"""Lite3 UDP 协议解析模块。

本模块是从原 Tkinter 脚本 `script/lite3_robot_state_receiver.py` 中迁移过来的
纯协议解析实现，满足以下约束：

1. 不修改 Lite3 UDP 协议、不删减任何已解析字段；
2. 只做 bytes -> Python dict 的结构化转换，不涉及 GUI / Web；
3. 解析失败时返回结构化错误信息，而不是抛出异常中断消费线程。

消息码：
    0x0901  机器人综合状态（220 字节：12 字节头 + 208 字节负载）
    0x0902  12 个关节角度（108 字节：12 字节头 + 96 字节负载）
    0x0903  12 个关节角速度（108 字节：12 字节头 + 96 字节负载）
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Any, Dict, Final, List, Optional, Tuple

from config import get_joint_names

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 常量定义
# ----------------------------------------------------------------------
MSG_ROBOT_STATE: Final[int] = 0x0901      # 机器人综合状态
MSG_JOINT_ANGLE: Final[int] = 0x0902      # 关节角度
MSG_JOINT_VELOCITY: Final[int] = 0x0903   # 关节角速度

HEADER_SIZE: Final[int] = 12              # 通用消息头长度
JOINT_COUNT: Final[int] = 12              # Lite3 关节数量

# 0x0901 各段长度（保持与原脚本一致）
ROBOT_STATE_PAYLOAD_SIZE: Final[int] = 208
JOINT_PAYLOAD_SIZE: Final[int] = 96

# 状态码映射表（与原脚本完全一致，不做任何删减）
BASIC_STATE_MAP: Final[Dict[int, str]] = {
    1: "趴下状态",
    4: "准备起立",
    5: "正在起立",
    6: "力控状态",
    7: "正在趴下",
    8: "失控保护",
    9: "姿态调整",
    11: "执行翻身",
    16: "AI状态",
    17: "回零状态",
    18: "执行后空翻",
    20: "执行打招呼",
    98: "未初始化(关节未回零)",
}

GAIT_STATE_MAP: Final[Dict[int, str]] = {
    0: "平地低速",
    2: "通用越障",
    4: "平地中速",
    5: "平地高速",
    6: "抓地越障",
    12: "太空步",
    13: "高踏步",
}

POLICY_STATE_MAP: Final[Dict[int, str]] = {
    0: "AI基础步态",
    16: "AI跳跃步态",
    18: "AI站立步态",
    20: "AI极速步态",
}

MOTION_STATE_MAP: Final[Dict[int, str]] = {
    0: "无动作",
    1: "踏步",
    2: "扭身体",
    4: "扭身跳",
    11: "向前跳",
}

# 0x0901 负载 struct 格式（小端序）：
# iiiI       basic_state / gait_state / policy_state / 保留位
# d*18       rpy(3) rpy_vel(3) xyz_acc(3) pos_world(3) vel_world(3) vel_body(3)
# I          touch_down_and_stair_trot
# B 3x       is_charging + 3 字节填充
# I i d i    error_state / motion_state / battery / task_state
# 4B         四个布尔标志位
# 2d         前后超声波距离
ROBOT_STATE_FORMAT: Final[str] = '<iiiI' + 'd' * 18 + 'I B 3x I i d i 4B 2d'


# ----------------------------------------------------------------------
# 公共工具
# ----------------------------------------------------------------------
def get_packet_code(data: bytes) -> Optional[int]:
    """读取数据包的消息码；数据不足 12 字节时返回 None。

    消息头按 3 个 uint32 解析：code、length、sequence（后两者仅用于诊断透传）。
    """
    if len(data) < HEADER_SIZE:
        return None
    code, _, _ = struct.unpack('<3I', data[:HEADER_SIZE])
    return int(code)


def _header_info(data: bytes) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """解析 12 字节通用消息头，返回 (头部摘要, 头部原始字段)。

    依据厂商文档 1.1 节，头部结构为::

        struct CommandHead {
            uint32_t code;            // 指令码
            uint32_t paramters_size;  // 数据内容长度
            uint32_t type;            // 0=简单指令，1=复杂指令
        };

    文档附录实例 `0209 0000 | 6000 0000 | 0100 0000` 即
    code=0x0902、paramters_size=0x60(96)、type=1（复杂指令）。
    """
    code, paramters_size, cmd_type = struct.unpack('<3I', data[:HEADER_SIZE])
    summary = {
        "type": "",                       # 由各 parse 函数填充
        "code": f"0x{int(code):04X}",
        "code_value": int(code),
        "length": len(data),
    }
    header_fields = {
        # 头部第 2、3 个字段（此前误命名为 length/sequence，据文档更正）
        "paramters_size": int(paramters_size),
        "cmd_type": int(cmd_type),
    }
    return summary, header_fields


def to_hex_preview(data: bytes, limit: int = 64) -> str:
    """把报文前 `limit` 字节转换成十六进制字符串，用于前端原始数据显示。"""
    chunk = data[:limit]
    preview = ' '.join(f'{b:02x}' for b in chunk)
    if len(data) > limit:
        preview += f" ... (共 {len(data)} 字节)"
    return preview


def to_hex_dump(data: bytes, max_bytes: int = 256) -> List[str]:
    """生成完整的十六进制转储行（偏移 + hex + ascii），供调试面板使用。"""
    lines: List[str] = []
    limit = min(len(data), max_bytes)
    for offset in range(0, limit, 16):
        chunk = data[offset:offset + 16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk).ljust(16 * 3 - 1)
        ascii_part = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)
        lines.append(f"{offset:04x}: {hex_part}  {ascii_part}")
    if len(data) > max_bytes:
        lines.append(f"... (共 {len(data)} 字节，仅显示前 {max_bytes})")
    return lines


def _source_info(addr: Optional[Tuple[str, int]]) -> Dict[str, Any]:
    """把 (ip, port) 转换成结构化来源信息。"""
    if addr is None:
        return {"ip": None, "port": None}
    return {"ip": addr[0], "port": int(addr[1])}


def _map_or_unknown(value: int, mapping: Dict[int, str]) -> str:
    """状态码 -> 中文描述；未知码保留原始数值。"""
    return mapping.get(value, f"未知({value})")


# ----------------------------------------------------------------------
# 0x0901 机器人综合状态
# ----------------------------------------------------------------------
def parse_robot_state(data: bytes, addr: Optional[Tuple[str, int]] = None) -> Dict[str, Any]:
    """解析消息码 0x0901：机器人综合状态。

    返回结构化 dict，包含：基本状态、步态、AI 步态、动作状态、IMU、
    世界系位置/速度、机体系速度、电池、标志位、超声波等全部字段。
    """
    summary, header_fields = _header_info(data)
    if len(data) < HEADER_SIZE + ROBOT_STATE_PAYLOAD_SIZE:
        # 长度不足时返回结构化错误，交给上层记录日志，不抛异常
        return {
            **summary,
            "type": "error",
            "error": f"数据长度不足: 期望 {HEADER_SIZE + ROBOT_STATE_PAYLOAD_SIZE} 字节，实际 {len(data)} 字节",
            "source": _source_info(addr),
            "timestamp": time.time(),
        }

    values = struct.unpack_from(ROBOT_STATE_FORMAT, data, HEADER_SIZE)
    it = iter(values)

    basic_state = next(it)
    gait_state = next(it)
    policy_state = next(it)
    _reserved = next(it)                       # 原脚本中的保留填充位

    rpy = [next(it) for _ in range(3)]         # IMU 姿态：roll / pitch / yaw
    rpy_vel = [next(it) for _ in range(3)]     # IMU 角速度
    xyz_acc = [next(it) for _ in range(3)]     # IMU 加速度
    pos_world = [next(it) for _ in range(3)]   # 世界系位置 x / y / yaw
    vel_world = [next(it) for _ in range(3)]   # 世界系速度 vx / vy / yaw_rate
    vel_body = [next(it) for _ in range(3)]    # 机体系速度 vx / vy / yaw_rate

    touch_down = next(it)                      # touch_down_and_stair_trot
    is_charging_byte = next(it)                # 原协议为 1 字节
    error_state = next(it)
    motion_state = next(it)
    battery = next(it)
    task_state = next(it)
    flag_bytes = [next(it) for _ in range(4)]
    ultrasound = [next(it) for _ in range(2)]

    is_robot_need_move = bool(flag_bytes[0])
    zero_position_flag = bool(flag_bytes[1])
    is_after_first_start = bool(flag_bytes[2])
    is_voice_ctrl_enable = bool(flag_bytes[3])
    is_charging = bool(is_charging_byte & 0xFF)

    # 电池电量：<=1.0 视为百分比小数，否则视为 0-100 的整数百分比
    battery_percent = battery * 100 if battery <= 1.0 else battery

    return {
        **summary,
        **header_fields,
        "type": "robot_state",
        "source": _source_info(addr),
        "timestamp": time.time(),

        # ---- 状态机 ----
        "basic_state": _map_or_unknown(basic_state, BASIC_STATE_MAP),
        "basic_state_code": int(basic_state),
        "gait_state": _map_or_unknown(gait_state, GAIT_STATE_MAP),
        "gait_state_code": int(gait_state),
        "policy_state": _map_or_unknown(policy_state, POLICY_STATE_MAP),
        "policy_state_code": int(policy_state),
        "motion_state": _map_or_unknown(motion_state, MOTION_STATE_MAP),
        "motion_state_code": int(motion_state),

        # ---- IMU ----
        "imu": {
            "roll": rpy[0], "pitch": rpy[1], "yaw": rpy[2],
            "roll_vel": rpy_vel[0], "pitch_vel": rpy_vel[1], "yaw_vel": rpy_vel[2],
            "x_acc": xyz_acc[0], "y_acc": xyz_acc[1], "z_acc": xyz_acc[2],
        },

        # ---- 位姿与速度 ----
        "position": {"x": pos_world[0], "y": pos_world[1], "yaw": pos_world[2]},
        "velocity": {"x": vel_world[0], "y": vel_world[1], "yaw": vel_world[2]},
        "velocity_body": {"x": vel_body[0], "y": vel_body[1], "yaw": vel_body[2]},

        # ---- 电源与故障 ----
        "battery": float(battery_percent),
        "error_state": int(error_state),
        "task_state": int(task_state),
        "touch_down_and_stair_trot": int(touch_down),
        "is_charging": bool(is_charging),

        # ---- 标志位 ----
        "is_robot_need_move": is_robot_need_move,
        "zero_position_flag": zero_position_flag,
        "is_after_first_start": is_after_first_start,
        "is_voice_ctrl_enable": is_voice_ctrl_enable,

        # ---- 超声波 ----
        "ultrasound": {"forward": ultrasound[0], "backward": ultrasound[1]},
    }


# ----------------------------------------------------------------------
# 0x0902 / 0x0903 关节数据
# ----------------------------------------------------------------------
def _parse_joint_values(data: bytes) -> Optional[List[float]]:
    """解析 12 个 double 类型的关节数据；长度不足时返回 None。"""
    if len(data) < HEADER_SIZE + JOINT_PAYLOAD_SIZE:
        return None
    fmt = '<' + 'd' * JOINT_COUNT
    return list(struct.unpack_from(fmt, data, HEADER_SIZE))


def parse_joint_angle(data: bytes, addr: Optional[Tuple[str, int]] = None) -> Dict[str, Any]:
    """解析消息码 0x0902：12 个关节角度，单位 rad。"""
    summary, header_fields = _header_info(data)
    values = _parse_joint_values(data)
    if values is None:
        return {
            **summary,
            "type": "error",
            "error": f"数据长度不足: 期望 {HEADER_SIZE + JOINT_PAYLOAD_SIZE} 字节，实际 {len(data)} 字节",
            "source": _source_info(addr),
            "timestamp": time.time(),
        }
    return {
        **summary,
        **header_fields,
        "type": "joint_angle",
        "source": _source_info(addr),
        "timestamp": time.time(),
        "joint": values,
        "joint_names": get_joint_names(),
        "unit": "rad",
    }


def parse_joint_velocity(data: bytes, addr: Optional[Tuple[str, int]] = None) -> Dict[str, Any]:
    """解析消息码 0x0903：12 个关节角速度，单位 rad/s。"""
    summary, header_fields = _header_info(data)
    values = _parse_joint_values(data)
    if values is None:
        return {
            **summary,
            "type": "error",
            "error": f"数据长度不足: 期望 {HEADER_SIZE + JOINT_PAYLOAD_SIZE} 字节，实际 {len(data)} 字节",
            "source": _source_info(addr),
            "timestamp": time.time(),
        }
    return {
        **summary,
        **header_fields,
        "type": "joint_velocity",
        "source": _source_info(addr),
        "timestamp": time.time(),
        "velocity": values,
        "joint_names": get_joint_names(),
        "unit": "rad/s",
    }


# ----------------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------------
def parse_packet(data: bytes, addr: Optional[Tuple[str, int]] = None) -> Dict[str, Any]:
    """根据消息码自动分发到对应的解析函数，返回结构化 dict。

    返回值一定包含 `type` 字段：
        robot_state | joint_angle | joint_velocity | unknown | error
    """
    code = get_packet_code(data)

    if code is None:
        return {
            "type": "error",
            "error": f"数据包过短: 仅 {len(data)} 字节，不足 {HEADER_SIZE} 字节消息头",
            "length": len(data),
            "source": _source_info(addr),
            "timestamp": time.time(),
        }

    try:
        if code == MSG_ROBOT_STATE:
            return parse_robot_state(data, addr)
        if code == MSG_JOINT_ANGLE:
            return parse_joint_angle(data, addr)
        if code == MSG_JOINT_VELOCITY:
            return parse_joint_velocity(data, addr)
    except struct.error as exc:
        # struct 解析异常不向上抛出，避免中断消费线程
        logger.warning("解析 0x%04X 数据失败: %s", code, exc)
        return {
            "type": "error",
            "code": f"0x{code:04X}",
            "code_value": code,
            "error": f"解析失败: {exc}",
            "length": len(data),
            "source": _source_info(addr),
            "timestamp": time.time(),
        }

    # 未支持的消息码：保留数据长度信息，便于后续协议扩展
    return {
        "type": "unknown",
        "code": f"0x{code:04X}",
        "code_value": code,
        "length": len(data),
        "source": _source_info(addr),
        "timestamp": time.time(),
        "hex_preview": to_hex_preview(data),
    }


__all__ = [
    "MSG_ROBOT_STATE",
    "MSG_JOINT_ANGLE",
    "MSG_JOINT_VELOCITY",
    "get_packet_code",
    "parse_packet",
    "parse_robot_state",
    "parse_joint_angle",
    "parse_joint_velocity",
    "to_hex_preview",
    "to_hex_dump",
]
