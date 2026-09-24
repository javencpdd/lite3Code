"""Lite3 数据包模拟发送器（仅用于本地联调）。

用途：在没有机器人本体时，向本机 UDP 43897 端口发送符合协议的
0x0901 / 0x0902 / 0x0903 数据包，用于验证后端解析与前端展示链路。

用法：
    python tools/mock_sender.py                  # 默认发往 127.0.0.1:43897
    python tools/mock_sender.py --ip 192.168.1.10 --hz 20
    python tools/mock_sender.py --once           # 只发一轮
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Tuple

# 允许直接以脚本方式运行：把 backend 目录加入模块搜索路径
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from parser import ROBOT_STATE_FORMAT  # noqa: E402  （迁移来源：协议格式常量）

HEADER_SIZE = 12
MSG_ROBOT_STATE = 0x0901
MSG_JOINT_ANGLE = 0x0902
MSG_JOINT_VELOCITY = 0x0903


def build_header(code: int, payload_size: int, sequence: int) -> bytes:
    """构造 12 字节通用消息头：code + payload 长度 + 序号。"""
    return struct.pack('<3I', code, HEADER_SIZE + payload_size, sequence)


def build_robot_state(t: float, sequence: int) -> bytes:
    """构造 0x0901 机器人综合状态报文（负载 208 字节）。"""
    basic_state = 6                      # 力控状态
    gait_state = 5                       # 平地高速
    policy_state = 0                     # AI基础步态
    reserved = 0

    # IMU：用正弦波模拟缓慢晃动的机身
    roll = 3.0 * math.sin(t * 0.6)
    pitch = 2.0 * math.sin(t * 0.4 + 0.8)
    yaw = 10.0 * math.sin(t * 0.2)
    roll_vel = 3.0 * 0.6 * math.cos(t * 0.6)
    pitch_vel = 2.0 * 0.4 * math.cos(t * 0.4 + 0.8)
    yaw_vel = 10.0 * 0.2 * math.cos(t * 0.2)
    x_acc = 0.4 * math.sin(t)
    y_acc = 0.3 * math.cos(t)
    z_acc = 9.81

    # 世界系位置 / 速度：绕圆周运动
    pos = [2.0 * math.cos(t * 0.3), 2.0 * math.sin(t * 0.3), yaw * math.pi / 180]
    vel_world = [-0.6 * math.sin(t * 0.3), 0.6 * math.cos(t * 0.3), yaw_vel]
    vel_body = [0.5, 0.0, yaw_vel]

    touch_down = 0
    is_charging = 0
    error_state = 0
    motion_state = 1                     # 踏步
    battery = 0.78                       # 小数形式，后端会换算成 78%
    task_state = 0
    flags = (1, 1, 1, 0)                 # 四个布尔标志位
    ultrasound = [1.25, 0.86]

    payload = struct.pack(
        ROBOT_STATE_FORMAT,
        basic_state, gait_state, policy_state, reserved,
        roll, pitch, yaw,
        roll_vel, pitch_vel, yaw_vel,
        x_acc, y_acc, z_acc,
        pos[0], pos[1], pos[2],
        vel_world[0], vel_world[1], vel_world[2],
        vel_body[0], vel_body[1], vel_body[2],
        touch_down, is_charging,
        error_state, motion_state, battery, task_state,
        flags[0], flags[1], flags[2], flags[3],
        ultrasound[0], ultrasound[1],
    )
    return build_header(MSG_ROBOT_STATE, len(payload), sequence) + payload


def build_joint_values(code: int, t: float, sequence: int, scale: float,
                       phase: float = 0.0) -> bytes:
    """构造 0x0902 / 0x0903 关节报文（负载 96 字节 = 12 个 double）。"""
    values = [scale * math.sin(t * 2.0 + i * 0.5 + phase) for i in range(12)]
    payload = struct.pack('<' + 'd' * 12, *values)
    return build_header(code, len(payload), sequence) + payload


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="Lite3 UDP 数据包模拟器")
    parser.add_argument("--ip", default="127.0.0.1", help="目标 IP，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=43897, help="目标端口，默认 43897")
    parser.add_argument("--hz", type=float, default=10.0, help="发送频率，默认 10Hz")
    parser.add_argument("--once", action="store_true", help="只发送一轮后退出")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target: Tuple[str, int] = (args.ip, args.port)
    interval = 1.0 / max(0.1, args.hz)
    print(f"开始向 {target} 发送模拟数据，频率 {args.hz}Hz，Ctrl+C 停止")

    sequence = 0
    start = time.time()
    try:
        while True:
            t = time.time() - start
            sequence += 1
            sock.sendto(build_robot_state(t, sequence), target)
            sock.sendto(build_joint_values(MSG_JOINT_ANGLE, t, sequence, 0.6), target)
            sock.sendto(build_joint_values(MSG_JOINT_VELOCITY, t, sequence, 1.2, 0.4), target)
            if args.once:
                print("已发送一轮：0x0901 / 0x0902 / 0x0903")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
