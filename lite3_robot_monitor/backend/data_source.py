"""数据源抽象：把不同的「机器人状态来源」统一成同一种接口。

为什么需要它
------------
中控后端获取机器人状态有三条路径（详见 config.DataSourceConfig）：

  sniff : AF_PACKET 旁路抓包，直接收 43897 原始 UDP（不占端口，与 transfer_ros2 共存）
  bind  : 直接绑定 43897（开发机 / 无权限兜底）
  ros   : 经本地桥接端口，接收 ros_bridge_node 转发的 ROS topic（已是结构化 dict）

三条路径对外暴露完全一致的接口，便于 MonitorService 无感替换，
也便于前端通过 /api/source 接口在运行时切换。

归一化帧（get_frame 返回值）
----------------------------
  {"kind": "raw",    "data": bytes, "addr": (ip, port)}   # sniff / bind：还需 parse_packet
  {"kind": "parsed", "payload": dict}                     # ros：已经是结构化 dict，直接喂 StateManager

消费循环据此决定是直接喂 StateManager，还是先走 parser.parse_packet。
"""

from __future__ import annotations

import abc
import contextlib
import json
import logging
import queue
import socket
import threading
from typing import Any, Dict, Optional

from config import data_source_config, udp_config
from udp_receiver import UDPReceiver
from udp_sniffer import UDPSniffer

logger = logging.getLogger(__name__)


class DataSource(abc.ABC):
    """所有数据源的基类。"""

    #: 对外展示的模式名：sniff / bind / ros
    mode: str = "abstract"
    #: 产出帧的类型：raw（需 parse_packet）或 parsed（已是 dict）
    kind: str = "raw"

    def __init__(self) -> None:
        self._running: bool = False

    @abc.abstractmethod
    def start(self) -> None:
        """启动数据源（创建 socket / 线程等）。"""

    @abc.abstractmethod
    def stop(self) -> None:
        """停止数据源并释放资源。"""

    @abc.abstractmethod
    def get_frame(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        """取一帧归一化数据；无数据时返回 None。"""

    def is_running(self) -> bool:
        return self._running

    @abc.abstractmethod
    def stats(self) -> Dict[str, Any]:
        """返回供 /api/status 展示的统计信息。"""


class _RawWrapperSource(DataSource):
    """把已有的 UDPSniffer / UDPReceiver 包成统一接口，产出 raw 帧。"""

    kind = "raw"

    def __init__(self, inner: Any, mode: str) -> None:
        super().__init__()
        self.inner = inner
        self.mode = mode

    def start(self) -> None:
        if not getattr(self.inner, "is_running", lambda: False)():
            self.inner.start()
        self._running = True

    def stop(self) -> None:
        try:
            self.inner.stop()
        finally:
            self._running = False

    def get_frame(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        frame = self.inner.get_packet(timeout)
        if frame is None:
            return None
        data, addr = frame
        return {"kind": "raw", "data": data, "addr": addr}

    def stats(self) -> Dict[str, Any]:
        return self.inner.stats()


class SniffSource(_RawWrapperSource):
    """AF_PACKET 旁路抓包数据源。"""

    def __init__(self) -> None:
        sniffer = UDPSniffer(
            port=udp_config.port,
            interface=udp_config.interface,
            max_queue_size=udp_config.max_queue_size,
        )
        super().__init__(sniffer, "sniff")


class BindSource(_RawWrapperSource):
    """直接绑定 UDP 端口的数据源（开发机兜底）。"""

    def __init__(self) -> None:
        super().__init__(UDPReceiver(), "bind")


class RosBridgeSource(DataSource):
    """本地桥接数据源：监听 ros_bridge_node 转发来的 JSON 状态帧。

    设计要点
    --------
    ros_bridge_node 在 ROS2 环境下运行（订阅 /leg_odom2 /imu/data /joint_states），
    把解析后的状态以 JSON 发给本机指定端口；这里只负责收 JSON 并喂给 StateManager。
    这样 uvicorn 进程可以完全不依赖 rclpy / ROS2 环境，二者通过本地 UDP 解耦。

    转发的 JSON 必须是已经过 parser 语义的 dict，至少包含 ``type`` 字段：
        robot_state | joint_angle | joint_velocity | unknown | error
    """

    kind = "parsed"
    mode = "ros"

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        max_queue_size: int = 1024,
        timeout: float = 0.2,
    ) -> None:
        super().__init__()
        self.host: str = host or data_source_config.ros_bridge_host
        self.port: int = int(port if port is not None else data_source_config.ros_bridge_port)
        self._timeout: float = timeout
        self._max_queue_size = max_queue_size
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue_size)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.received_packets: int = 0
        self.dropped_packets: int = 0

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(self._timeout)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="ros-bridge-rx", daemon=True)
        self._thread.start()
        logger.info("ROS 桥接数据源已启动: %s:%s", self.host, self.port)

    def _loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while self._running:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    logger.error("ROS 桥接接收异常: %s", exc)
                break

            try:
                payload = json.loads(data.decode("utf-8"))
            except Exception:  # noqa: BLE001 - 非 JSON 帧直接丢弃
                continue

            if not isinstance(payload, dict) or "type" not in payload:
                continue

            self.received_packets += 1
            try:
                if self._queue.full():
                    with contextlib.suppress(queue.Empty):
                        self._queue.get_nowait()
                    self.dropped_packets += 1
                self._queue.put_nowait(payload)
            except queue.Full:  # pragma: no cover - 极端并发兜底
                self.dropped_packets += 1

        logger.debug("ROS 桥接接收线程退出")

    def get_frame(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        try:
            payload = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        return {"kind": "parsed", "payload": payload}

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()
            self._sock = None
        logger.info("ROS 桥接数据源已停止")

    def stats(self) -> Dict[str, Any]:
        return {
            "mode": "ros",
            "host": self.host,
            "port": self.port,
            "running": self._running,
            "received_packets": self.received_packets,
            "dropped_packets": self.dropped_packets,
            "pending": self._queue.qsize(),
        }


def build_data_source(mode: str) -> DataSource:
    """按模式构造数据源。

    - ``bind`` / ``sniff``：直连 43897；
    - ``ros``：本地桥接端口，收 ros_bridge_node 转发来的 JSON；
    - ``ros_direct``：本进程内嵌订阅 ROS topic（需 ROS 环境）；
    - ``auto``：按 ``LITE3_ROS_IMPL`` 选 bridge（默认）或 direct。

    ros_direct 在**函数内**延迟导入：它的模块依赖 config，而 config 不依赖它，
    若放在模块顶层会形成 config → data_source → ros_direct_source → config 的循环。
    """
    mode = (mode or "auto").lower()
    if mode == "bind":
        return BindSource()
    if mode == "sniff":
        return SniffSource()
    if mode == "ros":
        return RosBridgeSource()
    if mode == "ros_direct":
        from ros_direct_source import RosDirectSource  # 延迟导入，见 docstring
        return RosDirectSource()
    # auto
    if data_source_config.ros_impl == "direct":
        from ros_direct_source import RosDirectSource  # 延迟导入，见 docstring
        return RosDirectSource()
    return RosBridgeSource()
