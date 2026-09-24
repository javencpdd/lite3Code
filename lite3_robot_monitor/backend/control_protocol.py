"""Lite3 控制指令报文构造（依据厂商《运动主机通讯接口》文档）。

协议来源
--------
厂商文档《运动主机 UDP 通讯接口》摘录已存档于
`docs/protocol/lite3-udp-protocol.md`（1.1 协议格式 / 1.2 控制指令集 / 1.3 接收指令集）。

报文格式（文档 1.1 节）
----------------------
简单指令（12 字节，type=0）::

    struct CommandHead {
        uint32_t code;            // xxxx 指令码
        uint32_t paramters_size;  // yyyy 指令值；无有效指令值时为 0
        uint32_t type;            // zzzz = 0
    };

复杂指令（12 + N 字节，type=1）::

    struct Command {
        CommandHead head;         // yyyy = 数据长度，zzzz = 1
        uint32_t data[kDataSize]; // bbbb… 数据内容
    };

- 字节序：**小端**（文档附录实例 `0209 0000` 即 0x0902）
- 发送长度：`sizeof(head) + head.paramters_size`
- 校验：简单/复杂指令**均无校验字段**（手柄帧才有）

关键约束（文档原文）
--------------------
- 心跳（`0x21040001`）：**下发频率不低于 2Hz**
- 轴指令：下发频率**不低于 20Hz**，**超时 250ms** 未收到则机器人自动停止运动
- 速度指令（`0x0140/0x0145/0x0141`）：**需在自主模式下发送**
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 目标地址
# ----------------------------------------------------------------------
# 运动主机：WiFi 网段 1 / 背部网口为 192.168.1.120，网段 2 为 192.168.2.1
DEFAULT_TARGET_IP: str = "192.168.1.120"
# 运动主机接收控制指令的端口
DEFAULT_TARGET_PORT: int = 43893

# 报文长度
SIMPLE_CMD_SIZE: int = 12      # 3 × uint32
COMPLEX_CMD_SIZE: int = 20     # 3 × uint32 + 1 × double

# ----------------------------------------------------------------------
# 指令码（文档 1.2 节，均为 32 位）
# ----------------------------------------------------------------------
# 1.2.1 心跳
CMD_HEARTBEAT: int = 0x21040001

# 1.2.2 机器人基本状态转换
CMD_STAND_LIE_TOGGLE: int = 0x21010202   # 起立/趴下（在趴下与初始站立间轮流切换）
CMD_SOFT_ESTOP: int = 0x21020C0E         # 软急停
CMD_ZERO_POSITION: int = 0x21010C05      # 回零（初始化关节）
CMD_ENTER_AI: int = 0x21010528           # 进入 AI 状态
CMD_EXIT_AI: int = 0x2101052B            # 退出 AI 状态

# 1.2.3 轴指令（type=0，指令值为 int32 原始值，非物理单位）
CMD_AXIS_FORWARD: int = 0x21010130       # 前后：移动模式/AI 下 [-6553, 6553]，正值向前
CMD_AXIS_SIDE: int = 0x21010131          # 左右：移动模式/AI 下 [-12553, 12553]，正值向右
CMD_AXIS_TURN: int = 0x21010135          # 转向：移动模式/AI 下 [-9553, 9553]，正值向右转
CMD_AXIS_PITCH: int = 0x21010130         # 原地模式下为俯仰角，正值低头
CMD_AXIS_ROLL: int = 0x21010131          # 原地模式下为横滚角，正值向右翻滚
CMD_AXIS_YAW: int = 0x21010135           # 原地模式下为偏航角，正值向右旋转
CMD_AXIS_HEIGHT: int = 0x21010102        # 身体高度 [-20000, 20000]，正值抬高

# 轴指令取值范围（文档死区定义）
AXIS_RANGE_FORWARD: int = 6553
AXIS_RANGE_SIDE: int = 12553
AXIS_RANGE_TURN: int = 9553
AXIS_RANGE_HEIGHT: int = 20000

# 1.2.4 运动模式切换
CMD_MODE_STANDSTILL: int = 0x21010D05    # 原地模式
CMD_MODE_MOVE: int = 0x21010D06          # 移动模式

# 1.2.5 步态切换
CMD_GAIT_LOW: int = 0x21010300           # 平地低速
CMD_GAIT_MID: int = 0x21010307           # 平地中速
CMD_GAIT_HIGH: int = 0x21010303          # 平地高速
CMD_GAIT_CRAWL_TOGGLE: int = 0x21010406  # 正常/匍匐（轮流切换）
CMD_GAIT_GRIP: int = 0x21010402          # 抓地越障
CMD_GAIT_GENERAL: int = 0x21010401       # 通用越障
CMD_GAIT_HIGH_STEP: int = 0x21010407     # 高踏步越障

# 1.2.6 动作指令
CMD_ACTION_TWIST: int = 0x21010204       # 扭身体（需力控状态）
CMD_ACTION_FLIP: int = 0x21010205        # 翻身（需趴下状态）
CMD_ACTION_MOONWALK: int = 0x2101030C    # 太空步（需力控状态）
CMD_ACTION_BACKFLIP: int = 0x21010502    # 后空翻（需趴下状态）
CMD_ACTION_GREET: int = 0x21010507       # 打招呼（需趴下状态）
CMD_ACTION_FORWARD_JUMP: int = 0x2101050B  # 向前跳（需趴下状态）
CMD_ACTION_TWIST_JUMP: int = 0x2101020D  # 扭身跳（需力控状态）
CMD_ACTION_STOP: int = 0x21010C0B        # 停止动作（需同时发指令值 0 和 1）

# 1.2.7 控制模式切换（决定响应哪一路速度指令）
CMD_CONTROL_AUTO: int = 0x21010C03       # 自主模式：响应感知主机下发的速度指令
CMD_CONTROL_MANUAL: int = 0x21010C02     # 手动模式：响应手柄下发的速度指令

# 1.2.8 保存数据（保存前 100 秒数据，清零力矩并关闭运动程序）
CMD_SAVE_DATA: int = 0x21010C01

# 1.2.9 持续运动（未进入 AI 且无轴指令时也保持原地踏步）
CMD_CONTINUOUS_MOVE: int = 0x21010C06
CONTINUOUS_MOVE_ON: int = -1
CONTINUOUS_MOVE_OFF: int = 2

# 1.2.10 语音指令与扬声器
CMD_VOICE: int = 0x21010C0A
CMD_SPEAKER: int = 0x2101030D            # 0=关 1=开 2=查询

# 1.2.11 感知设置类指令（发往运动主机）
CMD_SENSE_AI_OPTION: int = 0x21012109
SENSE_OPTION_OFF: int = 0x00             # 关闭所有 AI 选项
SENSE_OPTION_STOP: int = 0x20            # 开启停障
SENSE_OPTION_FOLLOW: int = 0xC0          # 开启跟随

# 1.2.12 速度指令（复杂指令，double，需自主模式）
CMD_VEL_X: int = 0x0140                  # 前后平移 m/s，[-1.0, 1.0]
CMD_VEL_Y: int = 0x0145                  # 左右平移 m/s，[-0.5, 0.5]
CMD_VEL_YAW: int = 0x0141                # 旋转角速度 rad/s，[-1.5, 1.5]

# 1.2.13 AI 步态切换
CMD_AI_GAIT_BASE: int = 0x2101052A
CMD_AI_GAIT_JUMP: int = 0x21010529
CMD_AI_GAIT_STAND: int = 0x2101052C
CMD_AI_GAIT_FAST: int = 0x2101052E

# 1.2.14 AI 动作
CMD_AI_ACTION: int = 0x2101030E
AI_ACTION_HANDSTAND: int = 4             # 倒立（需 AI 站立步态，最长 10s）
AI_ACTION_FLIP_INPLACE: int = 64         # 原地空翻（需 AI 跳跃步态）
AI_ACTION_JUMP_INPLACE: int = 128        # 原地跳跃（需 AI 跳跃步态）
AI_ACTION_STOP: int = 0                  # 停止 AI 动作

# ----------------------------------------------------------------------
# 安全限幅（取值来自文档 1.2.12 的取值范围）
# ----------------------------------------------------------------------
MAX_LINEAR_X: float = 1.0     # m/s
MAX_LINEAR_Y: float = 0.5     # m/s（文档中左右平移仅为 ±0.5）
MAX_ANGULAR: float = 1.5      # rad/s

# 心跳/轴指令的时间约束（文档 1.2.1 / 1.2.3）
MIN_HEARTBEAT_HZ: float = 2.0
AXIS_TIMEOUT_MS: int = 250

# ----------------------------------------------------------------------
# 手柄帧常量（NX2APPProtocol，文档未涉及；用于兼容手柄模拟通道）
# ----------------------------------------------------------------------
JOYSTICK_STX: Tuple[int, int] = (0x55, 0x66)
JOYSTICK_CTRL: int = 0
JOYSTICK_ID_RETROID: int = 1
JOYSTICK_RANGE: int = 1000
JOYSTICK_HEAD_SIZE: int = 10
JOYSTICK_CHANNEL_SIZE: int = 32
JOYSTICK_FRAME_SIZE: int = JOYSTICK_HEAD_SIZE + JOYSTICK_CHANNEL_SIZE


# ----------------------------------------------------------------------
# 语音指令值（文档 1.2.10）
# ----------------------------------------------------------------------
VOICE_COMMANDS: List[Tuple[int, str]] = [
    (0, "停止语音指令"), (1, "起立"), (2, "坐下"), (3, "前进"), (4, "后退"),
    (5, "向左平移"), (6, "向右平移"), (7, "停止"), (8, "低头"), (9, "抬头"),
    (11, "向左看"), (12, "向右看"), (13, "向左转90°"), (14, "向右转90°"),
    (15, "向后转180°"), (22, "打招呼"),
]


# ----------------------------------------------------------------------
# 预置指令表
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class PresetCommand:
    """一条预置控制指令。

    Attributes:
        name:     按钮显示名
        code:     指令码（32 位）
        value:    指令值 / paramters_size；简单指令无有效值时填 0
        cmd_type: 指令类型：0=简单指令，1=复杂指令
        data:     复杂指令携带的 double 数据（仅 type=1 时使用）
        group:    分组，用于前端归类展示
        note:     使用条件说明（文档中的「执行动作的条件」）
        danger:   是否为高风险指令（界面需二次确认/醒目标注）
    """
    name: str
    code: int
    value: int = 0
    cmd_type: int = 0
    data: Optional[float] = None
    group: str = "基础"
    note: str = ""
    danger: bool = False


# 全部指令均来自厂商文档，已核对指令码
PRESET_COMMANDS: List[PresetCommand] = [
    # ---- 1.2.2 基本状态转换 ----
    PresetCommand("起立/趴下", CMD_STAND_LIE_TOGGLE, 0, 0, None, "状态",
                  "在趴下状态与初始站立状态之间轮流切换"),
    PresetCommand("回零", CMD_ZERO_POSITION, 0, 0, None, "状态",
                  "初始化机器人关节，需先摆到准备姿势"),
    PresetCommand("进入AI", CMD_ENTER_AI, 0, 0, None, "状态", ""),
    PresetCommand("退出AI", CMD_EXIT_AI, 0, 0, None, "状态", ""),
    PresetCommand("软急停", CMD_SOFT_ESTOP, 0, 0, None, "状态",
                  "使机器人软急停", True),

    # ---- 1.2.4 运动模式 ----
    PresetCommand("原地模式", CMD_MODE_STANDSTILL, 0, 0, None, "模式",
                  "AI 状态下无效"),
    PresetCommand("移动模式", CMD_MODE_MOVE, 0, 0, None, "模式",
                  "AI 状态下无效"),

    # ---- 1.2.7 控制模式（速度指令的前提）----
    PresetCommand("自主模式", CMD_CONTROL_AUTO, 0, 0, None, "模式",
                  "★ 发送速度指令前必须先切到自主模式"),
    PresetCommand("手动模式", CMD_CONTROL_MANUAL, 0, 0, None, "模式",
                  "切回手柄控制"),

    # ---- 1.2.5 步态切换 ----
    PresetCommand("平地低速", CMD_GAIT_LOW, 0, 0, None, "步态", ""),
    PresetCommand("平地中速", CMD_GAIT_MID, 0, 0, None, "步态", ""),
    PresetCommand("平地高速", CMD_GAIT_HIGH, 0, 0, None, "步态", ""),
    PresetCommand("正常/匍匐", CMD_GAIT_CRAWL_TOGGLE, 0, 0, None, "步态",
                  "两个步态之间轮流切换"),
    PresetCommand("通用越障", CMD_GAIT_GENERAL, 0, 0, None, "步态", ""),
    PresetCommand("抓地越障", CMD_GAIT_GRIP, 0, 0, None, "步态", ""),
    PresetCommand("高踏步越障", CMD_GAIT_HIGH_STEP, 0, 0, None, "步态", ""),

    # ---- 1.2.6 动作（按文档标注的使用条件）----
    PresetCommand("扭身体", CMD_ACTION_TWIST, 0, 0, None, "动作", "需处于力控状态（静止站立）"),
    PresetCommand("太空步", CMD_ACTION_MOONWALK, 0, 0, None, "动作", "需处于力控状态"),
    PresetCommand("扭身跳", CMD_ACTION_TWIST_JUMP, 0, 0, None, "动作", "需处于力控状态", True),
    PresetCommand("翻身", CMD_ACTION_FLIP, 0, 0, None, "动作", "需处于趴下状态", True),
    PresetCommand("向前跳", CMD_ACTION_FORWARD_JUMP, 0, 0, None, "动作", "需处于趴下状态，前方 2m 内无障碍", True),
    PresetCommand("后空翻", CMD_ACTION_BACKFLIP, 0, 0, None, "动作", "需处于趴下状态", True),
    PresetCommand("打招呼", CMD_ACTION_GREET, 0, 0, None, "动作", "需处于趴下状态"),
    PresetCommand("停止动作", CMD_ACTION_STOP, 0, 0, None, "动作",
                  "需同时发送指令值 0 和 1，本实现会自动连发两条"),

    # ---- 1.2.13 AI 步态 ----
    PresetCommand("AI基础步态", CMD_AI_GAIT_BASE, 0, 0, None, "AI", "需处于 AI 状态"),
    PresetCommand("AI跳跃步态", CMD_AI_GAIT_JUMP, 0, 0, None, "AI", "仅用于位姿调整，勿控制运动", True),
    PresetCommand("AI站立步态", CMD_AI_GAIT_STAND, 0, 0, None, "AI", "仅用于位姿调整，勿控制运动", True),
    PresetCommand("AI极速步态", CMD_AI_GAIT_FAST, 0, 0, None, "AI", "需处于 AI 状态"),

    # ---- 1.2.8 / 1.2.9 其他 ----
    PresetCommand("持续运动开", CMD_CONTINUOUS_MOVE, CONTINUOUS_MOVE_ON, 0, None, "其他",
                  "无轴指令时保持原地踏步"),
    PresetCommand("持续运动关", CMD_CONTINUOUS_MOVE, CONTINUOUS_MOVE_OFF, 0, None, "其他", ""),
    PresetCommand("保存数据", CMD_SAVE_DATA, 0, 0, None, "其他",
                  "保存前 100 秒数据，清力矩并关闭运动程序", True),
]


# ----------------------------------------------------------------------
# 报文构造
# ----------------------------------------------------------------------
def build_simple_cmd(code: int, value: int = 0) -> bytes:
    """构造简单指令（12 字节）。

    Args:
        code:  指令码（xxxx）
        value: 指令值（yyyy）；无有效指令值时传 0
    """
    return struct.pack('<III', int(code) & 0xFFFFFFFF, int(value) & 0xFFFFFFFF, 0)


def build_complex_cmd(code: int, data: Any, param_size: Optional[int] = None) -> bytes:
    """构造复杂指令（12 + N 字节，type=1）。

    Args:
        code:       指令码（xxxx）
        data:       数据内容；标量按 double(8B) 打包，bytes 按原样拼装
        param_size: 数据长度（yyyy）；缺省时自动推断
    """
    if isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        payload = struct.pack('<d', float(data))
    size = len(payload) if param_size is None else int(param_size)
    return struct.pack('<III', int(code) & 0xFFFFFFFF, size, 1) + payload[:size]


def build_velocity_packets(linear_x: float, linear_y: float,
                           angular_z: float) -> List[bytes]:
    """构造速度指令组（文档 1.2.12，三条复杂指令）。

    Args:
        linear_x: 前后平移 m/s，正=前进，范围 [-1.0, 1.0]
        linear_y: 左右平移 m/s，**文档定义正方向为向右**，范围 [-0.5, 0.5]
        angular_z: 旋转角速度 rad/s，**文档定义正方向为向右转**，范围 [-1.5, 1.5]

    注意：本函数按文档语义直接下发（正值=向右），
          UI 若以「左」为正需在调用前自行取负。
    """
    lx, ly, az = clip_velocity(linear_x, linear_y, angular_z)
    return [
        build_complex_cmd(CMD_VEL_X, lx),
        build_complex_cmd(CMD_VEL_Y, ly),
        build_complex_cmd(CMD_VEL_YAW, az),
    ]


def clip_velocity(linear_x: float, linear_y: float, angular_z: float,
                  max_x: float = MAX_LINEAR_X, max_y: float = MAX_LINEAR_Y,
                  max_angular: float = MAX_ANGULAR) -> Tuple[float, float, float]:
    """按文档取值范围做硬限幅（后端执行，不信任前端）。"""
    def _clip(v: float, limit: float) -> float:
        return max(-limit, min(limit, float(v)))

    return _clip(linear_x, max_x), _clip(linear_y, max_y), _clip(angular_z, max_angular)


def clip_axis(value: int, limit: int) -> int:
    """轴指令限幅（原始值范围）。"""
    return max(-limit, min(limit, int(value)))


def build_axis_packet(code: int, value: int) -> bytes:
    """构造轴指令（简单指令，值为 int32 原始值）。"""
    return build_simple_cmd(code, clip_axis(value, AXIS_RANGE_HEIGHT))


def build_from_preset(preset: PresetCommand) -> List[bytes]:
    """按预置指令构造报文组。

    Returns:
        报文列表。多数指令为 1 条；「停止动作」按文档需同时发送指令值 0 和 1，返回 2 条。
    """
    if preset.name == "停止动作":
        return [build_simple_cmd(CMD_ACTION_STOP, 0), build_simple_cmd(CMD_ACTION_STOP, 1)]
    if preset.data is None:
        return [build_simple_cmd(preset.code, preset.value)]
    return [build_complex_cmd(preset.code, preset.data)]


# ----------------------------------------------------------------------
# 手柄帧（保留：文档未涉及，来源为官方 Lite3_ROS nx2app.cpp）
# ----------------------------------------------------------------------
def joystick_checksum(data: bytes) -> int:
    """手柄帧校验：data 区按字节累加到 uint16（与 nx2app.cpp 实现一致）。"""
    checksum = 0
    for byte in data:
        checksum = (checksum + byte) & 0xFFFF
    return checksum


def build_joystick_frame(buttons: Optional[List[int]] = None, left_x: int = 0,
                         left_y: int = 0, right_x: int = 0, right_y: int = 0,
                         axis_buttons: Optional[List[int]] = None, seq: int = 0,
                         ctrl: int = JOYSTICK_CTRL,
                         controller_id: int = JOYSTICK_ID_RETROID) -> bytes:
    """构造手柄通道帧（42 字节，pack(1)）。摇杆量程 ±1000。"""
    buttons = buttons or [0] * 10
    axis_buttons = axis_buttons or [0] * 2
    if len(buttons) != 10:
        raise ValueError("buttons 必须为 10 个元素")
    if len(axis_buttons) != 2:
        raise ValueError("axis_buttons 必须为 2 个元素")

    def _i16(v: int) -> int:
        return max(-32768, min(32767, int(v)))

    channels = struct.pack(
        '<10H4h2H',
        *[int(b) & 0xFFFF for b in buttons],
        _i16(left_x), _i16(left_y), _i16(right_x), _i16(right_y),
        *[int(b) & 0xFFFF for b in axis_buttons],
    )
    checksum = joystick_checksum(channels)
    head = struct.pack('<BBBHHBH', JOYSTICK_STX[0], JOYSTICK_STX[1],
                       int(ctrl) & 0xFF, len(channels), int(seq) & 0xFFFF,
                       int(controller_id) & 0xFF, checksum)
    if len(head) != JOYSTICK_HEAD_SIZE:
        raise RuntimeError("手柄帧头长度异常: %d" % len(head))
    return head + channels


# ----------------------------------------------------------------------
# 自定义报文
# ----------------------------------------------------------------------
def build_custom_packet(code: int, value: int = 0, cmd_type: int = 0,
                        data: Optional[float] = None) -> bytes:
    """构造自定义报文：传 data 走复杂指令(20B)，否则简单指令(12B)。"""
    if cmd_type == 1 and data is not None:
        return build_complex_cmd(code, data)
    return build_simple_cmd(code, value)


def build_hex_packet(hex_str: str) -> bytes:
    """十六进制串 → 报文字节（支持空格/逗号/0x 前缀）。"""
    cleaned = ''.join(hex_str.split()).replace('0x', '').replace(',', '').replace(':', '')
    if not cleaned:
        raise ValueError("十六进制内容为空")
    if len(cleaned) % 2 != 0:
        raise ValueError("十六进制长度必须为偶数（当前 %d 个字符）" % len(cleaned))
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError("十六进制解析失败: %s" % exc) from exc


# ----------------------------------------------------------------------
# 指令码 -> 名称（审计 / 界面可读化；同码多名取最常用者）
# ----------------------------------------------------------------------
CODE_NAMES: Dict[int, str] = {
    CMD_HEARTBEAT: "心跳包",
    CMD_STAND_LIE_TOGGLE: "起立/趴下",
    CMD_SOFT_ESTOP: "软急停",
    CMD_ZERO_POSITION: "回零",
    CMD_ENTER_AI: "进入 AI",
    CMD_EXIT_AI: "退出 AI",
    CMD_AXIS_FORWARD: "轴·前后/俯仰",
    CMD_AXIS_SIDE: "轴·左右/横滚",
    CMD_AXIS_TURN: "轴·转向/偏航",
    CMD_AXIS_HEIGHT: "轴·身体高度",
    CMD_MODE_STANDSTILL: "原地模式",
    CMD_MODE_MOVE: "移动模式",
    CMD_GAIT_LOW: "平地低速",
    CMD_GAIT_MID: "平地中速",
    CMD_GAIT_HIGH: "平地高速",
    CMD_GAIT_CRAWL_TOGGLE: "正常/匍匐",
    CMD_GAIT_GRIP: "抓地越障",
    CMD_GAIT_GENERAL: "通用越障",
    CMD_GAIT_HIGH_STEP: "高踏步越障",
    CMD_ACTION_TWIST: "扭身体",
    CMD_ACTION_FLIP: "翻身",
    CMD_ACTION_MOONWALK: "太空步",
    CMD_ACTION_BACKFLIP: "后空翻",
    CMD_ACTION_GREET: "打招呼",
    CMD_ACTION_FORWARD_JUMP: "向前跳",
    CMD_ACTION_TWIST_JUMP: "扭身跳",
    CMD_ACTION_STOP: "停止动作",
    CMD_CONTROL_AUTO: "自主模式",
    CMD_CONTROL_MANUAL: "手动模式",
    CMD_SAVE_DATA: "保存数据",
    CMD_CONTINUOUS_MOVE: "持续运动",
    CMD_VOICE: "语音指令",
    CMD_SPEAKER: "扬声器",
    CMD_SENSE_AI_OPTION: "感知 AI 选项",
    CMD_VEL_X: "速度·前后",
    CMD_VEL_Y: "速度·左右",
    CMD_VEL_YAW: "速度·旋转",
    CMD_AI_GAIT_BASE: "AI 基础步态",
    CMD_AI_GAIT_JUMP: "AI 跳跃步态",
    CMD_AI_GAIT_STAND: "AI 站立步态",
    CMD_AI_GAIT_FAST: "AI 极速步态",
    CMD_AI_ACTION: "AI 动作",
}


def code_name(code: int) -> str:
    """指令码 -> 中文名；未知码返回空串（界面回退到十六进制显示）。"""
    return CODE_NAMES.get(int(code) & 0xFFFFFFFF, "")


def data_views(body: bytes) -> Dict[str, Any]:
    """对数据区做多视角解码，支撑界面的「自定义解析格式」。

    协议只规定速度指令的 data 为 double，其余自定义报文究竟是整数还是
    浮点要靠人工判断；这里把常见解释一并给出，由界面切换显示。
    """
    views: Dict[str, Any] = {"length": len(body), "hex": body.hex()}
    if len(body) >= 8:
        views["f64"] = struct.unpack_from("<d", body, 0)[0]
        views["f32x2"] = [float(v) for v in struct.unpack_from("<2f", body, 0)]
    if len(body) >= 4:
        n = len(body) // 4
        views["u32"] = [int(v) for v in struct.unpack_from("<%dI" % n, body, 0)]
        views["i32"] = [int(v) for v in struct.unpack_from("<%di" % n, body, 0)]
    if len(body) >= 2:
        n = len(body) // 2
        views["u16"] = [int(v) for v in struct.unpack_from("<%dH" % n, body, 0)]
        views["i16"] = [int(v) for v in struct.unpack_from("<%dh" % n, body, 0)]
    if body:
        views["u8"] = list(body)
        views["i8"] = [b - 256 if b > 127 else b for b in body]
    return views


def describe_packet(payload: bytes) -> Dict[str, Any]:
    """把报文翻译成结构化字段，用于发送反馈、审计与界面解码展示。

    返回结构（新增字段均为向后兼容的增量）：
        length / hex / kind / code / code_hex / paramters_size / type / data
        name           指令码对应的中文名（未知为空串）
        param_meaning  'value'（简单指令：该字段就是指令值）
                       'size' （复杂指令：该字段是数据长度）
        fields         逐字段结构化列表：name / label / offset / size / hex / value
        data_hex       数据区十六进制
        data_views     数据区多视角解码（double / float / int32 / uint32 …）
    """
    info: Dict[str, Any] = {
        "length": len(payload),
        "hex": payload.hex(),
    }

    # 手柄帧与命令帧互斥：手柄帧前两字节是 0x5566，若按 <III 强行解会解出
    # 假的 code / 指令值（例如 code=0x20006655），所以先判定手柄帧。
    is_joystick = (
        len(payload) == JOYSTICK_FRAME_SIZE
        and payload[0] == JOYSTICK_STX[0]
        and payload[1] == JOYSTICK_STX[1]
    )

    if len(payload) >= SIMPLE_CMD_SIZE and not is_joystick:
        code, size, ctype = struct.unpack_from("<III", payload, 0)
        # 文档 1.2.3：轴指令值为 int32（如 -1、±6553），按无符号解会得到 4294967295 这类值
        signed = struct.unpack_from("<i", payload, 4)[0]
        body = payload[SIMPLE_CMD_SIZE:]
        is_complex = ctype == 1
        info.update(
            kind="ComplexCMD" if is_complex else "SimpleCMD",
            code=code,
            code_hex="0x%08X" % code,
            name=code_name(code),
            paramters_size=size,
            param_i32=signed,
            type=ctype,
            param_meaning="size" if is_complex else "value",
            data_hex=body.hex(),
            data_views=data_views(body),
        )
        if is_complex and len(payload) >= COMPLEX_CMD_SIZE:
            info["data"] = struct.unpack_from("<d", payload, SIMPLE_CMD_SIZE)[0]

        # 逐字段结构化：界面据此渲染字段表，并给十六进制预览分段着色。
        # 值一律以十六进制呈现（与报文原文一致，不做十进制换算）。
        info["fields"] = [
            {"name": "code", "label": "指令码", "offset": 0, "size": 4,
             "hex": payload[0:4].hex(), "value": "0x%08X" % code,
             "note": info.get("name") or "未知指令"},
            {"name": "paramters_size", "label": "数据长度" if is_complex else "指令值",
             "offset": 4, "size": 4, "hex": payload[4:8].hex(),
             "value": "0x%08X" % size,
             "note": ("数据长度 %d 字节" % size) if is_complex
                     else ("有符号解读 %d" % signed)},
            {"name": "type", "label": "类型", "offset": 8, "size": 4,
             "hex": payload[8:12].hex(), "value": "0x%08X" % ctype,
             "note": "1 复杂指令" if is_complex else "0 简单指令"},
        ]
        if body:
            info["fields"].append({
                "name": "data", "label": "数据区", "offset": SIMPLE_CMD_SIZE,
                "size": len(body), "hex": body.hex(),
                "value": "0x" + body.hex().upper(),
                "note": ("%d 字节" % len(body)),
            })

    if is_joystick:
        ctrl, data_len, seq, cid, checksum = struct.unpack("<BHHBH", payload[2:10])
        channels = payload[JOYSTICK_HEAD_SIZE:]
        info.update(kind="JoystickFrame", ctrl=ctrl, data_len=data_len, seq=seq,
                    id=cid, checksum=checksum,
                    name="手柄帧",
                    param_meaning="joystick",
                    data_hex=channels.hex(),
                    data_views=data_views(channels),
                    checksum_ok=(joystick_checksum(channels) == checksum))
        info["fields"] = [
            {"name": "stx", "label": "帧头", "offset": 0, "size": 2,
             "hex": payload[0:2].hex(), "value": "0x5566"},
            {"name": "ctrl", "label": "控制字", "offset": 2, "size": 1,
             "hex": payload[2:3].hex(), "value": str(ctrl)},
            {"name": "len", "label": "数据长度", "offset": 3, "size": 2,
             "hex": payload[3:5].hex(), "value": str(data_len)},
            {"name": "seq", "label": "序号", "offset": 5, "size": 2,
             "hex": payload[5:7].hex(), "value": str(seq)},
            {"name": "id", "label": "手柄 ID", "offset": 7, "size": 1,
             "hex": payload[7:8].hex(), "value": str(cid)},
            {"name": "checksum", "label": "校验", "offset": 8, "size": 2,
             "hex": payload[8:10].hex(),
             "value": "%d %s" % (checksum, "OK" if info.get("checksum_ok") else "BAD")},
            {"name": "channels", "label": "通道数据", "offset": JOYSTICK_HEAD_SIZE,
             "size": JOYSTICK_CHANNEL_SIZE, "hex": payload[JOYSTICK_HEAD_SIZE:].hex(),
             "value": ""},
        ]
    return info


def list_presets() -> List[Dict[str, Any]]:
    """返回预置指令表（供前端渲染按钮，按 group 归类）。"""
    return [
        {
            "name": p.name,
            "code": p.code,
            "code_hex": "0x%08X" % p.code,
            "value": p.value,
            "type": p.cmd_type,
            "group": p.group,
            "note": p.note,
            "danger": p.danger,
        }
        for p in PRESET_COMMANDS
    ]
