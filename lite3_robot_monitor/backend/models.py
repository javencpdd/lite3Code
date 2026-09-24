"""Pydantic 数据模型。

用途：
1. 约束 REST / WebSocket 对外输出的数据结构；
2. 为前端 TypeScript 类型定义提供唯一事实来源；
3. 在 FastAPI 层做响应序列化，避免把内部可变对象直接暴露出去。

注意： Parsing 模块（parser.py）输出的是普通 dict，
      只有在 HTTP 响应时才用这里的模型做校验与序列化。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Permissive(BaseModel):
    """允许额外字段的基类。

    协议字段可能随 SDK 版本变化，宽松地保留未知字段，
    既能保证不丢数据，也不会因为新增字段导致响应失败。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class SourceInfo(BaseModel):
    """UDP 数据来源。"""
    ip: Optional[str] = Field(None, description="来源 IP 地址")
    port: Optional[int] = Field(None, description="来源端口")


class Vector3(BaseModel):
    """平面位姿 / 速度：x、y、yaw（或 yaw_rate）。"""
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0


class ImuData(BaseModel):
    """IMU 姿态、角速度与加速度。"""
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    roll_vel: float = 0.0
    pitch_vel: float = 0.0
    yaw_vel: float = 0.0
    x_acc: float = 0.0
    y_acc: float = 0.0
    z_acc: float = 0.0


class UltrasoundData(BaseModel):
    """前后超声波测距结果，单位 m。"""
    forward: float = 0.0
    backward: float = 0.0


class RobotStateModel(_Permissive):
    """0x0901 机器人综合状态。"""
    type: str = "robot_state"
    code: str = "0x0901"
    length: Optional[int] = None
    source: SourceInfo = Field(default_factory=SourceInfo)
    timestamp: float = 0.0

    # 状态机（附原始数值，便于排障）
    basic_state: str = "未知"
    basic_state_code: int = 0
    gait_state: str = "未知"
    gait_state_code: int = 0
    policy_state: str = "未知"
    policy_state_code: int = 0
    motion_state: str = "未知"
    motion_state_code: int = 0

    imu: ImuData = Field(default_factory=ImuData)
    position: Vector3 = Field(default_factory=Vector3)
    velocity: Vector3 = Field(default_factory=Vector3)
    velocity_body: Vector3 = Field(default_factory=Vector3)

    battery: float = Field(0.0, ge=0.0, le=100.0, description="电池电量百分比 0-100")
    error_state: int = 0
    task_state: int = 0
    touch_down_and_stair_trot: int = 0
    is_charging: bool = False

    is_robot_need_move: bool = False
    zero_position_flag: bool = False
    is_after_first_start: bool = False
    is_voice_ctrl_enable: bool = False

    ultrasound: UltrasoundData = Field(default_factory=UltrasoundData)


class JointAngleModel(_Permissive):
    """0x0902 关节角度。"""
    type: str = "joint_angle"
    code: str = "0x0902"
    length: Optional[int] = None
    source: SourceInfo = Field(default_factory=SourceInfo)
    timestamp: float = 0.0
    joint: List[float] = Field(default_factory=list, description="12 个关节角度，单位 rad")
    joint_names: List[str] = Field(default_factory=list)
    unit: str = "rad"


class JointVelocityModel(_Permissive):
    """0x0903 关节角速度。"""
    type: str = "joint_velocity"
    code: str = "0x0903"
    length: Optional[int] = None
    source: SourceInfo = Field(default_factory=SourceInfo)
    timestamp: float = 0.0
    velocity: List[float] = Field(default_factory=list, description="12 个关节角速度，单位 rad/s")
    joint_names: List[str] = Field(default_factory=list)
    unit: str = "rad/s"


class UnknownPacketModel(_Permissive):
    """未在本工程支持的消息码，仅做透传展示。"""
    type: str = "unknown"
    code: str = ""
    length: Optional[int] = None
    source: SourceInfo = Field(default_factory=SourceInfo)
    timestamp: float = 0.0


class MonitorState(BaseModel):
    """监控快照：一次推送给前端的完整机器人状态。"""
    type: str = "snapshot"
    server_time: float = Field(0.0, description="服务端时间戳（秒）")
    robot_state: Optional[RobotStateModel] = None
    joint_angle: Optional[JointAngleModel] = None
    joint_velocity: Optional[JointVelocityModel] = None
    others: List[UnknownPacketModel] = Field(default_factory=list)
    connected: bool = Field(False, description="机器人链路是否在线")

    model_config = ConfigDict(extra="allow")


class ServiceStatus(BaseModel):
    """服务自身运行状态，用于健康检查与页面顶部状态灯。"""
    connected: bool = False
    last_update: Optional[float] = Field(None, description="最后一次收到 UDP 数据的时间戳（秒）")
    since_last_update: Optional[float] = Field(None, description="距最后一次收到数据的间隔（秒）")
    udp_running: bool = False
    udp_mode: str = Field("bind", description="接收模式：ros / sniff（旁路抓包） / bind（绑定端口）")
    data_source: str = Field("none", description="当前生效的数据源模式：ros / sniff / bind / none")
    udp_host: str = ""
    udp_port: int = 0
    packets_received: int = 0
    packets_dropped: int = 0
    packets_parsed: int = 0
    packets_error: int = 0
    ws_clients: int = 0
    push_hz: int = 0


class RawPacketItem(BaseModel):
    """原始报文记录，供前端 RawPacket 组件展示。"""
    code: str = ""
    code_value: int = 0
    length: int = 0
    source_ip: Optional[str] = None
    source_port: Optional[int] = None
    received_at: float = 0.0
    hex_preview: str = ""


class RawPacketResponse(BaseModel):
    """GET /api/raw 响应体。"""
    count: int = 0
    items: List[RawPacketItem] = Field(default_factory=list)


# ----------------------------------------------------------------------
# 控制通道（写方向）相关模型
# ----------------------------------------------------------------------
class VelocityCommand(BaseModel):
    """速度指令：与 /cmd_vel 语义一致。"""
    x: float = Field(0.0, description="前后线速度，正值前进（m/s）")
    y: float = Field(0.0, description="左右线速度，正值左移（m/s）")
    yaw: float = Field(0.0, description="转向角速度，正值左转（rad/s）")


class PresetCommandRequest(BaseModel):
    """按名称下发预置指令。"""
    name: str = Field(..., description="预置指令名，如 前进 / 后退 / 左转")


class CustomCommandRequest(BaseModel):
    """自定义指令报文。

    传 `data` 时按 ComplexCMD(20B) 发送，否则按 SimpleCMD(12B) 发送。
    """
    cmd_code: int = Field(..., description="命令码，如 320/325/321/503")
    cmd_value: int = Field(0, description="命令值，速度类固定为 8")
    type: int = Field(0, description="类型，速度类为 1，简单指令为 0")
    data: Optional[float] = Field(None, description="ComplexCMD 的 double 数据")


class RawHexRequest(BaseModel):
    """完全自定义的原始十六进制报文。"""
    hex: str = Field(..., description="十六进制串，如 '55 66 00 20' 或 '55660020'")


class ControlStatus(BaseModel):
    """控制服务状态。"""
    enabled: bool = False
    estop: bool = False
    heartbeat_running: bool = False
    heartbeat_interval: float = 0.25
    heartbeat_lease: float = 5.0
    lease_remaining: float = 0.0
    target: str = ""
    current_velocity: VelocityCommand = Field(default_factory=VelocityCommand)
    limits: Dict[str, float] = Field(default_factory=dict)
    sent_packets: int = 0
    failed_packets: int = 0
    # 心跳与业务分开累计（心跳 4Hz 会淹没合并计数）
    heartbeat_packets: int = 0
    business_packets: int = 0
    last_sent_at: Optional[float] = None
    last_error: Optional[str] = None
    last_modes: Dict[str, Any] = {}
    velocity_invert: Dict[str, bool] = {}

    model_config = ConfigDict(extra="allow")


__all__ = [
    "SourceInfo",
    "Vector3",
    "ImuData",
    "UltrasoundData",
    "RobotStateModel",
    "JointAngleModel",
    "JointVelocityModel",
    "UnknownPacketModel",
    "MonitorState",
    "ServiceStatus",
    "RawPacketItem",
    "RawPacketResponse",
    "VelocityCommand",
    "PresetCommandRequest",
    "CustomCommandRequest",
    "RawHexRequest",
    "ControlStatus",
]
