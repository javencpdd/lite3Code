"""Lite3 Robot Monitor 后端全局配置。

设计要点：
1. 所有可调参数集中在本文件，避免散落到业务模块中；
2. 全部参数支持通过环境变量覆盖，方便在机器人本体 / 边缘主机上部署；
3. 使用冻结（frozen）的数据类，运行期不允许被意外修改；
4. 控制相关配置的默认值取自 control_protocol（协议常量单一来源）。

说明：本模块 import control_protocol 取默认值，
     为避免循环依赖，control_protocol **不得** import config。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import control_protocol as cp


def _get_str(name: str, default: str) -> str:
    """读取字符串型环境变量，未设置时返回默认值。"""
    value = os.getenv(name)
    return value if value else default


def _get_int(name: str, default: int) -> int:
    """读取整型环境变量，解析失败时回退到默认值。"""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    """读取浮点型环境变量，解析失败时回退到默认值。"""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "f", ""}


def _get_bool(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量。

    真：1 / true / yes / on / y / t；假：0 / false / no / off / n / f（均大小写不敏感）。

    之所以显式列出而不是只判 ``== "true"``：部署时很容易顺手写成 ``=1``，
    只判 "true" 会让这类配置**静默失效**（既没报错也没生效），排查成本很高。
    无法识别时回退默认值，不抛异常——配置错误不该让服务起不来。
    """
    value = os.getenv(name)
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    return default


@dataclass(frozen=True)
class UDPConfig:
    """UDP 接收相关配置。

    默认值与原 Tkinter 脚本保持一致：监听本机所有网卡的 43897 端口。
    """

    # 监听地址：0.0.0.0 表示接收所有网卡上的数据
    host: str = _get_str("LITE3_UDP_HOST", "0.0.0.0")
    # Lite3 状态数据默认目标端口
    port: int = _get_int("LITE3_UDP_PORT", 43897)
    # 单次 recvfrom 的最大字节数，需大于最长报文（0x0901 共 220 字节）
    buffer_size: int = _get_int("LITE3_UDP_BUFFER", 2048)
    # Socket 超时时间（秒），保证后台线程可以被及时停止
    timeout: float = _get_float("LITE3_UDP_TIMEOUT", 0.1)
    # 接收队列最大长度，超限时丢弃最旧的数据包，防止长时间断网后堆积
    max_queue_size: int = _get_int("LITE3_UDP_QUEUE", 1024)
    # 接收模式：
    #   auto  —— Linux 上优先用旁路抓包（不占用端口），失败则退回 bind（默认）
    #   sniff —— 强制旁路抓包；103 部署推荐，避免与 transfer_ros2 抢 43897
    #   bind  —— 强制绑定端口；Windows 开发机或没有 root 权限时使用
    mode: str = _get_str("LITE3_UDP_MODE", "auto").lower()
    # 旁路抓包监听的网卡名；为空表示所有网卡。
    # 指定网卡可显著减少无关流量（例如 103 上拉取的 RTSP 视频流）。
    interface: Optional[str] = _get_str("LITE3_UDP_IFACE", "") or None


@dataclass(frozen=True)
class ServiceConfig:
    """Web 服务相关配置。"""

    host: str = _get_str("LITE3_HTTP_HOST", "0.0.0.0")
    port: int = _get_int("LITE3_HTTP_PORT", 8000)
    # WebSocket 推送频率（Hz），计划书要求 10Hz
    push_hz: int = _get_int("LITE3_PUSH_HZ", 10)
    # 超过该时间（秒）没有收到任何 UDP 数据，即判定机器人离线
    link_timeout: float = _get_float("LITE3_LINK_TIMEOUT", 3.0)
    # 原始报文环形缓存条数，供前端 RawPacket 组件展示
    raw_history_size: int = _get_int("LITE3_RAW_HISTORY", 30)
    # 原始报文十六进制预览的最大字节数
    raw_preview_bytes: int = _get_int("LITE3_RAW_PREVIEW", 64)
    # 是否挂载已构建的前端静态资源（frontend/dist）
    serve_frontend: bool = _get_bool("LITE3_SERVE_FRONTEND", True)
    # 前端静态资源目录（相对 backend 目录）
    frontend_dist: str = _get_str("LITE3_FRONTEND_DIST", "../frontend/dist")


@dataclass(frozen=True)
class ControlConfig:
    """控制指令（写方向）相关配置。

    安全提醒：43893 直连闭源运动控制器，**不经过 VOA 安全层**，
    因此默认关闭，必须显式开启；速度上限在后端二次夹取。
    """

    # 运动主机地址：WiFi 网段 1 / 背部网口为 192.168.1.120，网段 2 为 192.168.2.1
    target_ip: str = _get_str("LITE3_CTRL_IP", cp.DEFAULT_TARGET_IP)
    # 运动主机接收指令的端口（与 120 侧 jy_exe 的 local_port 一致）
    target_port: int = _get_int("LITE3_CTRL_PORT", cp.DEFAULT_TARGET_PORT)
    # 心跳周期（秒）。官方 SDK：1s 无指令即收回控制权，故必须 < 1s
    heartbeat_interval: float = _get_float("LITE3_CTRL_HEARTBEAT", 0.25)
    # 心跳租约（秒）：超过该时间前端未续约则自动停心跳并发零速
    heartbeat_lease: float = _get_float("LITE3_CTRL_LEASE", 5.0)
    # 速度硬限幅（文档 1.2.12：前后 ±1.0、左右 ±0.5、旋转 ±1.5）
    max_linear: float = _get_float("LITE3_CTRL_MAX_LINEAR", cp.MAX_LINEAR_X)
    max_linear_y: float = _get_float("LITE3_CTRL_MAX_LINEAR_Y", cp.MAX_LINEAR_Y)
    max_angular: float = _get_float("LITE3_CTRL_MAX_ANGULAR", cp.MAX_ANGULAR)
    # 是否在服务启动时自动启用控制通道（默认关闭，强烈建议保持 False）
    enabled_by_default: bool = _get_bool("LITE3_CTRL_ENABLED", False)

    # 速度方向取反开关。
    # 文档 1.2.12 定义"x 正值前进、y 正值向右、yaw 正值向右转"，代码按此实现；
    # 若实机表现相反（固件与文档不一致，或对"右"的参考系理解不同），
    # 用这三个开关逐轴纠正即可，无需改代码。
    #
    # y 轴默认取反：103 实机实测「左右」方向与文档定义相反（下发正值向右，
    # 实机却向左），故此处默认 True 。x / yaw 仍遵循文档。
    invert_vel_x: bool = _get_bool("LITE3_CTRL_INVERT_VEL_X", False)
    # invert_vel_y: bool = _get_bool("LITE3_CTRL_INVERT_VEL_Y", False)
    invert_vel_y: bool = True
    invert_vel_yaw: bool = _get_bool("LITE3_CTRL_INVERT_VEL_YAW", False)


@dataclass(frozen=True)
class DataSourceConfig:
    """状态数据来源配置。

    机器人状态有三条获取路径，本配置决定中控后端默认走哪条：

      ros   —— 经本地桥接端口接收 `ros_bridge_node` 转发的 ROS topic
               （/leg_odom2 /imu/data /joint_states）。推荐：零端口冲突、
               拿到的已是结构化消息、与 transfer_ros2 完全解耦。
      sniff —— AF_PACKET 旁路抓包直接收 43897（不占端口，与 transfer_ros2 共存）。
      bind  —— 直接绑定 43897（开发机 / 无权限时的兜底）。
      auto  —— 优先 ros；ros 无数据时自动回退 sniff，**ros 恢复后再自动切回 ros**。
               即「ros 为主 + sniff 兜底」是双向自愈的，而非一次性降级
               （见 main.py 的 MonitorService 数据源看门狗）。

    注意：ros 模式依赖独立的 ros_bridge_node（在 ROS2 环境下运行），
    本后端只负责在本地端口收它转发来的 JSON，自身不依赖 rclpy。
    """

    mode: str = _get_str("LITE3_DATA_SOURCE", "auto").lower()
    # ros 模式：ros_bridge_node 转发来的本地接收地址
    ros_bridge_host: str = _get_str("LITE3_ROS_BRIDGE_HOST", "127.0.0.1")
    ros_bridge_port: int = _get_int("LITE3_ROS_BRIDGE_PORT", 43900)

    # ros 曾经正常、之后才断流时（例如 transfer_ros2 重启），静默多久判定失效并回退。
    # 比 link_timeout 更长：10Hz 的 topic 偶尔抖动不该触发降级。
    ros_stale_timeout: float = _get_float("LITE3_ROS_STALE_TIMEOUT", 10.0)

    # ---- 内嵌订阅（ros_direct）相关 ----
    # ros 话题订阅的实现方式：
    #   bridge —— 外部 ros_bridge_node 进程转发（默认，后端不依赖 ROS 环境）
    #   direct —— 后端进程自己 import rclpy 订阅（少一个进程，但需 source ROS）
    # 仅影响 ros / auto 模式；显式指定 ros_direct 时以它为准。
    ros_impl: str = _get_str("LITE3_ROS_IMPL", "bridge").lower()
    # 内嵌订阅的节点名：并行部署多套实例时必须各不相同（或用 ROS_DOMAIN_ID 隔离）
    ros_node_name: str = _get_str("LITE3_ROS_NODE_NAME", "lite3_monitor_ros")
    # 内嵌订阅的 topic 名（与 transfer_ros2 实际发布的一致）
    ros_topic_imu: str = _get_str("LITE3_ROS_TOPIC_IMU", "/imu/data")
    ros_topic_odom: str = _get_str("LITE3_ROS_TOPIC_ODOM", "/leg_odom2")
    ros_topic_joints: str = _get_str("LITE3_ROS_TOPIC_JOINTS", "/joint_states")
    # 回退 sniff 期间，每隔多久探测一次 ros 是否已恢复。
    # 探测不丢数据（见 main.py _probe_ros_available），故可以设置得比较积极。
    ros_retry_interval: float = _get_float("LITE3_ROS_RETRY_INTERVAL", 15.0)


@dataclass(frozen=True)
class DisplayConfig:
    """数值展示相关的辅助配置。"""

    # 12 个关节的显示名称。
    # 顺序依据厂商文档 1.3.2：左前侧摆、左前髋、左前膝 → 右前 → 左后 → 右后，
    # 即 LF / RF / LB / RB 四腿，每腿 hip_x（侧摆）/ hip_y（髋）/ knee（膝）。
    joint_legs: Tuple[str, ...] = ("LF", "RF", "LB", "RB")
    joint_parts: Tuple[str, ...] = ("hip_x", "hip_y", "knee")


# 全局单例配置对象
udp_config = UDPConfig()
service_config = ServiceConfig()
control_config = ControlConfig()
data_source_config = DataSourceConfig()
display_config = DisplayConfig()


def get_joint_names() -> List[str]:
    """生成 12 个关节的显示名称，例如 FR_hip、FR_thigh ...。"""
    names: List[str] = []
    for leg in display_config.joint_legs:
        for part in display_config.joint_parts:
            names.append(f"{leg}_{part}")
    # SDK 关节顺序若少于 12 个，使用通用名称兜底，保证下标不会越界。
    while len(names) < 12:
        names.append(f"joint_{len(names)}")
    return names[:12]
