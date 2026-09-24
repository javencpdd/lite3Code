"""内嵌 ROS2 话题订阅数据源（`ros_direct` 模式）。

与 `ros`（外部 `ros_bridge_node` 转发）模式的区别
------------------------------------------------
`ros` 模式靠一个独立进程订阅 topic、再经本地 UDP 转发给后端，
为的是让后端进程完全不依赖 ROS 环境（可在 Windows 开发机运行）。

若确认**只在 103 上运行**，这一层就没有必要了：本模块让后端进程自己
`import rclpy` 订阅 topic，省掉一个进程、一跳 UDP、一个 systemd 单元。

代价（接受前请确认）
--------------------
1. 后端进程必须运行在 source 过 ROS 的解释器里（服务单元需改 ExecStart）；
2. ROS 图异常（DDS 抖动、节点名冲突）与 Web 服务同进程，靠看门狗降级兜底；
3. 失去跨平台能力——Windows 开发机无法直接启动后端。

为什么要「节点常驻」而不是随数据源销毁重建
------------------------------------------
1. `rclpy.init()` / `shutdown()` 在一个进程内只能成对调用一次，反复 init 会抛异常；
2. 节点常驻后，即使看门狗已降级到 sniff，回调仍在更新"最后收到时间"，
   恢复判定只需读这个时间戳——比"重建节点再等首帧"快得多，且期间零丢数据。
   对应到 bridge 模式，这等价于"桥接端口上有包"。
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Set

from data_source import DataSource

logger = logging.getLogger(__name__)


class RosDirectError(RuntimeError):
    """ROS 环境不可用：未 source setup.bash、rclpy 缺失、init 失败等。"""


class _RosRuntime:
    """进程级 rclpy 运行时：一个节点 + 一个 spin 线程 + 若干消费队列。"""

    def __init__(self, topics: Dict[str, str], node_name: str,
                 queue_size: int = 1024) -> None:
        self.topics = topics
        self.node_name = node_name
        self._queue_size = queue_size
        self._sinks: Set["queue.Queue"] = set()
        self._sinks_lock = threading.Lock()

        import ros_topic_adapter as rta  # 本地导入：保持本模块可独立 import
        self._rta = rta
        self._state: Dict[str, Any] = rta.new_state()
        self._dirty = False
        self._last_msg_at = 0.0

        self._node = None
        self._rclpy = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    def bootstrap(self) -> None:
        """导入 rclpy、创建节点并启动 spin 线程。失败抛 RosDirectError。"""
        try:
            import rclpy  # type: ignore
            from rclpy.node import Node  # type: ignore
            from geometry_msgs.msg import Twist  # noqa: F401  类型由 topic 名决定
            from nav_msgs.msg import Odometry  # type: ignore
            from sensor_msgs.msg import Imu, JointState  # type: ignore
        except Exception as exc:  # noqa: BLE001 - 任何导入失败都归为环境不可用
            raise RosDirectError(
                "无法导入 rclpy / ROS 消息类型（%s）。"
                "请确认已在启动服务前 source /opt/ros/<distro>/setup.bash" % exc
            ) from exc

        try:
            if not rclpy.ok():
                rclpy.init(args=None)
        except Exception as exc:  # noqa: BLE001
            raise RosDirectError("rclpy.init() 失败: %s" % exc) from exc

        self._rclpy = rclpy
        node = Node(self.node_name)

        node.create_subscription(Imu, self.topics["imu"], self._on_imu, 10)
        node.create_subscription(Odometry, self.topics["odom"], self._on_odom, 10)
        node.create_subscription(JointState, self.topics["joints"], self._on_joints, 10)
        # 周期 flush：即使 imu/odom 静默也维持 10Hz 心跳帧
        node.create_timer(0.1, self._flush)

        self._node = node
        self._thread = threading.Thread(
            target=rclpy.spin, args=(node,), name="ros-direct-spin", daemon=True
        )
        self._thread.start()
        logger.info(
            "内嵌 ROS 订阅已启动：%s / %s / %s（节点 %s）",
            self.topics["imu"], self.topics["odom"], self.topics["joints"], self.node_name,
        )

    # ------------------------------------------------------------------
    # 回调（运行在 spin 线程）
    # ------------------------------------------------------------------
    def _mark(self) -> None:
        self._last_msg_at = time.time()

    def _on_imu(self, msg: Any) -> None:
        try:
            self._rta.apply_imu(self._state, msg)
            self._dirty = True
            self._mark()
        except Exception as exc:  # noqa: BLE001 - 单包异常不能打死 spin 线程
            logger.warning("处理 /imu 失败: %s", exc)

    def _on_odom(self, msg: Any) -> None:
        try:
            self._rta.apply_odom(self._state, msg)
            self._dirty = True
            self._mark()
        except Exception as exc:  # noqa: BLE001
            logger.warning("处理 /odom 失败: %s", exc)

    def _on_joints(self, msg: Any) -> None:
        try:
            self._flush()  # 先推出上一帧 robot_state，保持顺序
            for frame in self._rta.joint_frames(msg):
                self._push(frame)
            self._mark()
        except Exception as exc:  # noqa: BLE001
            logger.warning("处理 /joint_states 失败: %s", exc)

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False  # 先清：无消费者时不必重发
        self._state["timestamp"] = time.time()
        self._push(dict(self._state))

    # ------------------------------------------------------------------
    def _push(self, payload: Dict[str, Any]) -> None:
        with self._sinks_lock:
            sinks = list(self._sinks)
        for q in sinks:
            try:
                if q.full():
                    with contextlib.suppress(queue.Empty):
                        q.get_nowait()
                q.put_nowait(payload)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    def attach(self, q: "queue.Queue") -> None:
        with self._sinks_lock:
            self._sinks.add(q)

    def detach(self, q: "queue.Queue") -> None:
        with self._sinks_lock:
            self._sinks.discard(q)

    @property
    def last_message_at(self) -> float:
        return self._last_msg_at

    def shutdown(self) -> None:
        """进程退出时清理（正常路径由 systemd 直接回收，这里主要服务测试）。"""
        if self._rclpy is not None and self._node is not None:
            try:
                self._rclpy.spin  # noqa: B018 - 属性存在性检查，避免未初始化时误用
                self._node.destroy_node()
                self._rclpy.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug("rclpy 清理异常: %s", exc)


# ----------------------------------------------------------------------
# 进程级单例
# ----------------------------------------------------------------------
_RUNTIME: Optional[_RosRuntime] = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime(topics: Dict[str, str], node_name: str,
                queue_size: int = 1024) -> _RosRuntime:
    """获取（必要时创建）进程级 ROS 运行时。"""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            runtime = _RosRuntime(topics, node_name, queue_size)
            runtime.bootstrap()
            _RUNTIME = runtime
        return _RUNTIME


def peek_runtime() -> Optional[_RosRuntime]:
    """返回已存在的运行时；**不会**创建新的。

    供看门狗在降级到 sniff 期间探测"ROS 是否已恢复"——不触发 rclpy.init()，
    也就不会因为探测本身把 ROS 环境拉起来。
    """
    return _RUNTIME


def peek_last_message_age() -> Optional[float]:
    """距最近一次 topic 回调的秒数；运行时未创建或从未收到消息时返回 None。"""
    runtime = peek_runtime()
    if runtime is None or runtime.last_message_at <= 0:
        return None
    return max(0.0, time.time() - runtime.last_message_at)


class RosDirectSource(DataSource):
    """内嵌订阅 ROS topic 的数据源（产出 parsed 帧）。"""

    kind = "parsed"
    mode = "ros_direct"

    def __init__(self, topics: Optional[Dict[str, str]] = None,
                 node_name: Optional[str] = None,
                 max_queue_size: int = 1024,
                 timeout: float = 0.2) -> None:
        super().__init__()
        from config import data_source_config  # 延迟导入，避免循环依赖

        self.topics: Dict[str, str] = topics or {
            "imu": data_source_config.ros_topic_imu,
            "odom": data_source_config.ros_topic_odom,
            "joints": data_source_config.ros_topic_joints,
        }
        self.node_name: str = node_name or data_source_config.ros_node_name
        self._max_queue_size = max_queue_size
        self._timeout = timeout
        self._queue: "queue.Queue" = queue.Queue(maxsize=max_queue_size)
        self._runtime: Optional[_RosRuntime] = None
        self.received_packets: int = 0
        self.dropped_packets: int = 0

    # ------------------------------------------------------------------
    def start(self) -> None:
        # 首次调用时才真正 init rclpy；失败向上抛，由 MonitorService 兜底降级
        self._runtime = get_runtime(self.topics, self.node_name, self._max_queue_size)
        self._runtime.attach(self._queue)
        self._running = True
        logger.info("ros_direct 数据源已挂载：节点 %s", self.node_name)

    def stop(self) -> None:
        """只摘除消费队列，**不销毁 ROS 节点**（详见模块文档字符串）。

        节点继续在后台收 topic，看门狗据此判断 ROS 是否已恢复。
        """
        if self._runtime is not None:
            self._runtime.detach(self._queue)
        self._running = False
        logger.info("ros_direct 数据源已卸载（ROS 节点保持运行）")

    def get_frame(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        try:
            payload = self._queue.get(timeout=timeout or self._timeout)
        except queue.Empty:
            return None
        self.received_packets += 1
        return {"kind": "parsed", "payload": payload}

    # ------------------------------------------------------------------
    def last_message_age(self) -> Optional[float]:
        """距最近一次 topic 回调的秒数；从未收到过返回 None。"""
        if self._runtime is None or self._runtime.last_message_at <= 0:
            return None
        return max(0.0, time.time() - self._runtime.last_message_at)

    def stats(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "node": self.node_name,
            "topics": dict(self.topics),
            "running": self._running,
            "received_packets": self.received_packets,
            "dropped_packets": self.dropped_packets,
            "pending": self._queue.qsize(),
            # 供看门狗判定 ROS 是否恢复（等价于 bridge 模式"端口上有包"）
            "last_message_age": self.last_message_age(),
        }
