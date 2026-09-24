"""ROS topic -> 中控状态字典 的纯转换函数。

为什么单独成模块
----------------
`ros_bridge_node.py`（独立桥接进程）与 `ros_direct_source.py`（内嵌订阅）
需要做**完全相同**的消息转换。若各写一份，两边迟早会漂移（比如某个 topic
的换算改了，另一边没改）。

因此把转换逻辑抽到这里，并且**刻意不 import rclpy**——这样：
  - 内嵌模式可以在没有 ROS 环境的机器上被 import（延迟导入 rclpy 才需要它）；
  - 单元测试可以用任意鸭子类型对象喂进来，不必真的起 ROS。

所有 apply_* 函数只要求入参具备对应属性（orientation / angular_velocity /
position / name ...），不校验具体类型。
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

#: 关节顺序：左前 → 右前 → 左后 → 右后（与 parser.py 的 LF/RF/LB/RB 一致）
LEG_ORDER: List[str] = ["LF", "RF", "LB", "RB"]

#: 数据来源标记（写入 payload.source，便于前端区分数据是 sniff 还是 ROS 侧来的）
SOURCE_TAG: Dict[str, Any] = {"ip": "ros_topic", "port": 0}


def quat_to_rpy(x: float, y: float, z: float, w: float):
    """四元数 -> (roll, pitch, yaw)，顺序与 parser.parse_robot_state 保持一致。"""
    sinr = 2.0 * (w * x + y * z)
    cosr = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr, cosr)

    sinp = 2.0 * (w * y - z * x)
    pitch = math.copysign(math.pi / 2.0, sinp) if abs(sinp) >= 1.0 else math.asin(sinp)

    siny = 2.0 * (w * z + x * y)
    cosy = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny, cosy)
    return roll, pitch, yaw


def joint_index(name: str) -> Optional[int]:
    """把 Unitree JointState 的关节名映射到 12 关节序号。

    命名形如 ``LF_hip_joint`` / ``RF_thigh_joint`` / ``LB_calf_joint``：
    腿序 LF/RF/LB/RB 各占 3 位，部件序 hip=侧摆、thigh=髋、calf=膝。
    无法识别时返回 None（调用方跳过该关节）。
    """
    leg = None
    for i, prefix in enumerate(LEG_ORDER):
        if name.startswith(prefix):
            leg = i
            break
    if leg is None:
        return None
    if "calf" in name:
        part = 2
    elif "thigh" in name:
        part = 1
    elif "hip" in name:
        part = 0
    else:
        return None
    return leg * 3 + part


def new_state() -> Dict[str, Any]:
    """新建一帧 robot_state 骨架。

    注意：ROS topic 只暴露 odom / imu / joint，**没有**状态机、电池、超声波，
    这些字段只能留默认（"未知" / 0）。需要完整保真请切回 sniff 模式。
    """
    return {
        "type": "robot_state",
        "code": "0x0901",
        "source": dict(SOURCE_TAG),
        "timestamp": time.time(),
        "imu": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0,
                "roll_vel": 0.0, "pitch_vel": 0.0, "yaw_vel": 0.0,
                "x_acc": 0.0, "y_acc": 0.0, "z_acc": 0.0},
        "position": {"x": 0.0, "y": 0.0, "yaw": 0.0},
        "velocity": {"x": 0.0, "y": 0.0, "yaw": 0.0},
        "velocity_body": {"x": 0.0, "y": 0.0, "yaw": 0.0},
    }


def apply_imu(state: Dict[str, Any], msg: Any) -> None:
    """把 sensor_msgs/Imu 写入 robot_state 的 imu 段。"""
    q = msg.orientation
    roll, pitch, yaw = quat_to_rpy(q.x, q.y, q.z, q.w)
    av = msg.angular_velocity
    la = msg.linear_acceleration
    state["imu"] = {
        "roll": roll, "pitch": pitch, "yaw": yaw,
        "roll_vel": av.x, "pitch_vel": av.y, "yaw_vel": av.z,
        "x_acc": la.x, "y_acc": la.y, "z_acc": la.z,
    }


def apply_odom(state: Dict[str, Any], msg: Any) -> None:
    """把 nav_msgs/Odometry 写入 robot_state 的 position / velocity 段。"""
    p = msg.pose.pose.position
    q = msg.pose.pose.orientation
    _, _, yaw = quat_to_rpy(q.x, q.y, q.z, q.w)
    tw = msg.twist.twist
    state["position"] = {"x": p.x, "y": p.y, "yaw": yaw}
    state["velocity"] = {"x": tw.linear.x, "y": tw.linear.y, "yaw": tw.angular.z}


def joint_frames(msg: Any) -> List[Dict[str, Any]]:
    """把 sensor_msgs/JointState 转成 joint_angle（+ 可选 joint_velocity）帧列表。"""
    joints = [0.0] * 12
    velocities = [0.0] * 12
    have_vel = False

    names = list(msg.name)
    for i, name in enumerate(names):
        idx = joint_index(name)
        if idx is None:
            continue
        joints[idx] = float(msg.position[i])
        if i < len(msg.velocity) and msg.velocity[i] != 0.0:
            velocities[idx] = float(msg.velocity[i])
            have_vel = True

    frames: List[Dict[str, Any]] = [{
        "type": "joint_angle",
        "code": "0x0902",
        "source": dict(SOURCE_TAG),
        "timestamp": time.time(),
        "joint": joints,
        "unit": "rad",
    }]
    if have_vel:
        frames.append({
            "type": "joint_velocity",
            "code": "0x0903",
            "source": dict(SOURCE_TAG),
            "timestamp": time.time(),
            "velocity": velocities,
            "unit": "rad/s",
        })
    return frames
