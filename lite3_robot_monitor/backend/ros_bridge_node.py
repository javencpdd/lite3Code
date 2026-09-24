"""ROS2 → 中控后端 桥接节点。

作用
----
把 Lite3 在 ROS2 侧已经发布的 topic（/leg_odom2、/imu/data、/joint_states）
转换成中控后端 `data_source.RosBridgeSource` 能消费的 JSON，通过**本地 UDP**
转发到后端监听的桥接端口（默认 127.0.0.1:43900）。

这样中控后端（uvicorn）完全不需要依赖 rclpy / ROS2 环境：
  - 本节点在 ROS2 环境里跑（source /opt/ros/<distro>/setup.bash）；
  - 后端只在一个本地端口收 JSON，做到彻底解耦。

与 sniff 模式的差异（重要）
--------------------------
sniff 收的是厂商私有 0x0901 原始 UDP，能拿到全部状态机字段
（basic_state / gait_state / 电池 / 超声波 …）。而 ROS topic 只暴露
odom / imu / joint，因此本桥接发出的 robot_state 中，状态机类字段为默认值
（"未知" / 0）。需要完整保真时，前端切回 sniff 即可。

运行方式（在 103 的 ROS2 终端）
------------------------------
    source /opt/ros/humble/setup.bash
    # 若使用了 colcon 工作区，还需 source install/setup.bash
    python3 ros_bridge_node.py \
        --imu /imu/data --odom /leg_odom2 --joints /joint_states \
        --host 127.0.0.1 --port 43900

注意：topic 名若与 103 上实际发布的名字不同，请用参数覆盖。
"""

from __future__ import annotations

import argparse
import json
import socket
import time

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist, Quaternion
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu, JointState

# 转换逻辑与内嵌订阅（ros_direct_source）共用同一份实现，避免两边漂移
from ros_topic_adapter import apply_imu, apply_odom, joint_frames, new_state

# 桥接来源标记：与内嵌订阅（ros_topic）区分，便于界面判断数据走的是哪条路
SOURCE_TAG = {"ip": "ros_bridge", "port": 0}


class RosBridge(Node):
    def __init__(self, args):
        # 节点名必须可由外部指定：并行部署多套实例时，若两个桥接节点同名，
        # 会在 ROS 图里冲突（后启动的可能把先注册的挤掉），进而影响已在运行的实例。
        super().__init__(args.name)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._host = args.host
        self._port = args.port

        # 合并后的 robot_state 缓存：imu / odom 任一更新就重发整帧
        self._state = new_state()
        self._state["source"] = dict(SOURCE_TAG)
        self._dirty = False

        self._imu_sub = self.create_subscription(
            Imu, args.imu, self._on_imu, 10)
        self._odom_sub = self.create_subscription(
            Odometry, args.odom, self._on_odom, 10)
        self._joint_sub = self.create_subscription(
            JointState, args.joints, self._on_joints, 10)
        self.get_logger().info(
            "ROS 桥接已启动，转发 %s / %s / %s -> %s:%s",
            args.imu, args.odom, args.joints, self._host, self._port,
        )

    # ------------------------------------------------------------------
    def _send(self, payload: dict) -> None:
        try:
            self._sock.sendto(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                (self._host, self._port),
            )
        except OSError as exc:
            self.get_logger().error("转发失败: %s", exc)

    def _on_imu(self, msg: Imu) -> None:
        apply_imu(self._state, msg)
        self._dirty = True

    def _on_odom(self, msg: Odometry) -> None:
        apply_odom(self._state, msg)
        self._dirty = True

    def _flush_state(self) -> None:
        if not self._dirty:
            return
        self._state["timestamp"] = time.time()
        self._send(self._state)
        self._dirty = False

    def _on_joints(self, msg: JointState) -> None:
        # 先 flush 上一帧 robot_state（imu/odom 可能已更新）
        self._flush_state()

        for frame in joint_frames(msg):
            frame["source"] = dict(SOURCE_TAG)
            self._send(frame)


def main():
    parser = argparse.ArgumentParser(description="Lite3 ROS topic -> 中控后端 桥接节点")
    parser.add_argument("--imu", default="/imu/data")
    parser.add_argument("--odom", default="/leg_odom2")
    parser.add_argument("--joints", default="/joint_states")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=43900)
    parser.add_argument(
        "--name", default="lite3_ros_bridge",
        help="ROS2 节点名；并行部署多套实例时必须各不相同，例如 lite3_ros_bridge2",
    )
    args = parser.parse_args()

    rclpy.init()
    node = RosBridge(args)
    try:
        # 周期性把缓存的 robot_state 刷出去，保证即使 imu/odom 静默也有心跳
        timer_period = 0.1  # 10Hz
        def timer_cb():
            node._flush_state()
        node.create_timer(timer_period, timer_cb)
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node._sock.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
