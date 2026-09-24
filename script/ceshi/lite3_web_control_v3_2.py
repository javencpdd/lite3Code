#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Lite3 Web Control v3

基于 ROS2 的 Lite3 浏览器控制台，不再直接 bind UDP 43897。

订阅：
  /joint_states                       sensor_msgs/msg/JointState
  /imu/data                           sensor_msgs/msg/Imu
  /leg_odom2                          nav_msgs/msg/Odometry
  /us_publisher/ultrasound_distance   std_msgs/msg/Float64

发布：
  /simple_cmd                         transfer_interfaces/msg/MotionSimpleCMD

移动控制后端：
  使用 Lite3 简单轴指令（虚拟摇杆），不再依赖旧的 /cmd_vel -> 320/325/321 复杂速度路径。
  控制启用后自动发送：
    手动模式 0x21010C02
    移动模式 0x21010D06
    平地低速步态 0x21010300
  并以 4 Hz 发送心跳 0x21040001。

Web:
  http://<Lite3-IP>:8080

键盘：
  W/S 前进/后退
  A/D 左移/右移
  Q/E 左转/右转
  Space 停止

注意：
  - 默认只监控；--enable-control 才允许虚拟摇杆运动控制。
  - --enable-actions 才允许初始化/起立/趴下。
  - /joint_states 当前实机 velocity/effort 为空，本程序显示的是 position 差分估算速度。
  - 起立/趴下默认使用 cmd_code=0x21010C0A，value 1/2；固件若不同可通过 CLI 覆盖。
  - 初始化/回零默认使用 cmd_code=0x21010C05，value 0。
"""

import argparse
import json
import math
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    QoSProfile,
    ReliabilityPolicy,
    DurabilityPolicy,
    HistoryPolicy,
)

from geometry_msgs.msg import Twist
from sensor_msgs.msg import JointState, Imu
from nav_msgs.msg import Odometry
from std_msgs.msg import Float64
from transfer_interfaces.msg import MotionSimpleCMD


CMD_INIT_ZERO = 0x21010C05
CMD_STAND_LIE = 0x21010C0A
STAND_VALUE = 1
LIE_VALUE = 2

# Lite3 高层控制模式 / 虚拟摇杆轴指令
CMD_MANUAL_MODE = 0x21010C02

# 运动模式
CMD_SPOT_MODE = 0x21010D05
CMD_MOVE_MODE = 0x21010D06

# 平地步态
CMD_GAIT_SLOW = 0x21010300
CMD_GAIT_MEDIUM = 0x21010307
CMD_GAIT_FAST = 0x21010303

CMD_HEARTBEAT = 0x21040001

CMD_AXIS_FORWARD = 0x21010130   # 正值前进
CMD_AXIS_LATERAL = 0x21010131   # 正值向右
CMD_AXIS_YAW = 0x21010135       # 正值向右转


def get_host_ipv4_addresses():
    addresses = []
    try:
        output = subprocess.check_output(
            ["hostname", "-I"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        for item in output.split():
            try:
                socket.inet_aton(item)
                if not item.startswith("127.") and item not in addresses:
                    addresses.append(item)
            except OSError:
                pass
    except Exception:
        pass

    if not addresses:
        try:
            _, _, ips = socket.gethostbyname_ex(socket.gethostname())
            for ip in ips:
                try:
                    socket.inet_aton(ip)
                    if not ip.startswith("127.") and ip not in addresses:
                        addresses.append(ip)
                except OSError:
                    pass
        except Exception:
            pass

    return addresses


def quaternion_to_rpy(x, y, z, w):
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    sinp = 2.0 * (w * y - z * x)
    if abs(sinp) >= 1.0:
        pitch = math.copysign(math.pi / 2.0, sinp)
    else:
        pitch = math.asin(sinp)

    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return roll, pitch, yaw


class Lite3WebNode(Node):
    def __init__(self, args):
        super().__init__("lite3_web_control")
        self.args = args

        self.state_lock = threading.Lock()
        self.control_lock = threading.Lock()

        self.joint_names = []
        self.joint_positions = []
        self.joint_vel_est = []
        self.prev_joint_positions = None
        self.prev_joint_rx_time = None

        self.imu = None
        self.odom = None
        self.ultrasound = None

        self.last_joint_rx = 0.0
        self.last_imu_rx = 0.0
        self.last_odom_rx = 0.0
        self.last_ultrasound_rx = 0.0

        self.pressed_keys = set()
        self.last_browser_heartbeat = 0.0

        self.action_lock_until = 0.0
        self.last_action = ""
        self.last_action_time = 0.0
        self.last_action_result = ""

        self.control_enabled = bool(args.enable_control)
        self.actions_enabled = bool(args.enable_actions)

        state_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )

        self.simple_cmd_pub = self.create_publisher(
            MotionSimpleCMD, args.simple_cmd_topic, 10
        )

        self.create_subscription(
            JointState, args.joint_topic, self.on_joint_state, state_qos
        )
        self.create_subscription(
            Imu, args.imu_topic, self.on_imu, state_qos
        )
        self.create_subscription(
            Odometry, args.odom_topic, self.on_odom, state_qos
        )
        self.create_subscription(
            Float64, args.ultrasound_topic, self.on_ultrasound, state_qos
        )

        self.create_timer(
            1.0 / max(args.rate, 1.0),
            self.control_tick
        )
        self.create_timer(
            1.0 / max(args.heartbeat_rate, 2.0),
            self.heartbeat_tick
        )

        self.control_prepared = False
        self.prepare_attempted = False

        # Web 显示用的“最后一次由本程序下发的模式/步态”。
        # 注意：Lite3 当前公开 ROS2 状态并不直接回传这两个模式，
        # 因此这里不是 firmware 的权威反馈，而是本程序的命令状态。
        self.motion_mode = "unknown"   # unknown / move / spot
        self.gait_mode = "unknown"     # unknown / slow / medium / fast

        self.get_logger().info("Lite3 Web ROS2 node initialized")
        self.get_logger().info("movement backend: Lite3 simple axis commands")
        self.get_logger().info("simple_cmd: %s" % args.simple_cmd_topic)
        self.get_logger().info(
            "control=%s actions=%s" %
            (self.control_enabled, self.actions_enabled)
        )

    def on_joint_state(self, msg):
        now = time.monotonic()
        positions = list(msg.position)

        with self.state_lock:
            vel_est = []

            if (
                self.prev_joint_positions is not None
                and len(self.prev_joint_positions) == len(positions)
                and self.prev_joint_rx_time is not None
            ):
                dt = now - self.prev_joint_rx_time

                if 0.001 <= dt <= 0.2:
                    raw = [
                        (positions[i] - self.prev_joint_positions[i]) / dt
                        for i in range(len(positions))
                    ]

                    if len(self.joint_vel_est) == len(raw):
                        alpha = self.args.joint_velocity_alpha
                        vel_est = [
                            alpha * raw[i]
                            + (1.0 - alpha) * self.joint_vel_est[i]
                            for i in range(len(raw))
                        ]
                    else:
                        vel_est = raw

            if not vel_est:
                vel_est = [0.0] * len(positions)

            self.joint_names = list(msg.name)
            self.joint_positions = positions
            self.joint_vel_est = vel_est
            self.prev_joint_positions = positions[:]
            self.prev_joint_rx_time = now
            self.last_joint_rx = now

    def on_imu(self, msg):
        now = time.monotonic()
        q = msg.orientation
        roll, pitch, yaw = quaternion_to_rpy(q.x, q.y, q.z, q.w)

        with self.state_lock:
            self.imu = {
                "orientation_quaternion": {
                    "x": q.x, "y": q.y, "z": q.z, "w": q.w
                },
                "rpy_deg": {
                    "roll": math.degrees(roll),
                    "pitch": math.degrees(pitch),
                    "yaw": math.degrees(yaw),
                },
                "angular_velocity": {
                    "x": msg.angular_velocity.x,
                    "y": msg.angular_velocity.y,
                    "z": msg.angular_velocity.z,
                },
                "linear_acceleration": {
                    "x": msg.linear_acceleration.x,
                    "y": msg.linear_acceleration.y,
                    "z": msg.linear_acceleration.z,
                },
            }
            self.last_imu_rx = now

    def on_odom(self, msg):
        now = time.monotonic()
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        roll, pitch, yaw = quaternion_to_rpy(q.x, q.y, q.z, q.w)

        with self.state_lock:
            self.odom = {
                "position": {"x": p.x, "y": p.y, "z": p.z},
                "orientation_deg": {
                    "roll": math.degrees(roll),
                    "pitch": math.degrees(pitch),
                    "yaw": math.degrees(yaw),
                },
                "linear_velocity": {
                    "x": msg.twist.twist.linear.x,
                    "y": msg.twist.twist.linear.y,
                    "z": msg.twist.twist.linear.z,
                },
                "angular_velocity": {
                    "x": msg.twist.twist.angular.x,
                    "y": msg.twist.twist.angular.y,
                    "z": msg.twist.twist.angular.z,
                },
            }
            self.last_odom_rx = now

    def on_ultrasound(self, msg):
        with self.state_lock:
            self.ultrasound = float(msg.data)
            self.last_ultrasound_rx = time.monotonic()

    def update_keys(self, keys):
        normalized = set()
        for key in keys:
            key = str(key).lower()
            if key in ("w", "s", "a", "d", "q", "e"):
                normalized.add(key)

        with self.control_lock:
            self.pressed_keys = normalized
            self.last_browser_heartbeat = time.monotonic()

    def browser_heartbeat(self):
        with self.control_lock:
            self.last_browser_heartbeat = time.monotonic()

    def clear_keys(self):
        with self.control_lock:
            self.pressed_keys.clear()
            self.last_browser_heartbeat = time.monotonic()

    def is_action_locked(self):
        return time.monotonic() < self.action_lock_until

    def build_axis_values(self):
        """根据当前按键计算 Lite3 虚拟摇杆轴值。"""
        now = time.monotonic()

        with self.control_lock:
            if self.last_browser_heartbeat <= 0.0:
                heartbeat_age = float("inf")
            else:
                heartbeat_age = now - self.last_browser_heartbeat

            if heartbeat_age > self.args.watchdog:
                self.pressed_keys.clear()

            keys = set(self.pressed_keys)

        if self.is_action_locked():
            keys.clear()

        forward = 0
        lateral = 0
        yaw = 0

        # 前后：正值前进
        if "w" in keys and "s" not in keys:
            forward = self.args.axis_forward
        elif "s" in keys and "w" not in keys:
            forward = -self.args.axis_forward

        # 横移：协议定义正值向右，因此 A 为负、D 为正
        if "a" in keys and "d" not in keys:
            lateral = -self.args.axis_lateral
        elif "d" in keys and "a" not in keys:
            lateral = self.args.axis_lateral

        # 偏航：协议定义正值向右转，因此 Q 为负、E 为正
        if "q" in keys and "e" not in keys:
            yaw = -self.args.axis_yaw
        elif "e" in keys and "q" not in keys:
            yaw = self.args.axis_yaw

        return forward, lateral, yaw

    def prepare_movement_control(self):
        """
        切入已验证的虚拟摇杆控制链路：
          手动模式 -> 移动模式 -> 低速步态
        这些是模式切换，不包含非零移动轴值。
        """
        self.send_simple_cmd(CMD_MANUAL_MODE, 0)
        time.sleep(0.08)
        self.send_simple_cmd(CMD_MOVE_MODE, 0)
        time.sleep(0.08)
        self.send_simple_cmd(CMD_GAIT_SLOW, 0)

        self.motion_mode = "move"
        self.gait_mode = "slow"
        self.control_prepared = True
        self.prepare_attempted = True

        self.get_logger().info(
            "movement control prepared: MANUAL + MOVE + SLOW_GAIT"
        )

    def heartbeat_tick(self):
        if not self.control_enabled:
            return

        try:
            self.send_simple_cmd(CMD_HEARTBEAT, 0)
        except Exception as exc:
            self.get_logger().warning(
                "heartbeat publish failed: %s" % exc
            )

    def control_tick(self):
        if not self.control_enabled:
            return

        # 首次控制 tick 自动准备模式；只做一次。
        if not self.prepare_attempted:
            try:
                self.prepare_movement_control()
            except Exception as exc:
                self.prepare_attempted = True
                self.control_prepared = False
                self.get_logger().error(
                    "prepare movement control failed: %s" % exc
                )
                return

        # 原地模式下，同一组轴码有“调整身体姿态”的含义。
        # 因此 Web 的 WASD 只允许在 MOVE 模式下发送非零轴值。
        if self.motion_mode != "move" or not self.control_prepared:
            return

        forward, lateral, yaw = self.build_axis_values()

        # 轴指令要求持续发送；即使为 0 也持续发布以可靠清零。
        self.send_simple_cmd(CMD_AXIS_FORWARD, forward)
        self.send_simple_cmd(CMD_AXIS_LATERAL, lateral)
        self.send_simple_cmd(CMD_AXIS_YAW, yaw)

    def set_motion_mode(self, mode):
        """Web 切换移动模式 / 原地模式。"""
        if not self.control_enabled:
            return False, "运动控制未启用；启动时添加 --enable-control"

        if self.is_action_locked():
            return False, "姿态动作执行期间不能切换运动模式"

        self.clear_keys()
        self.publish_stop(repeat=5)

        try:
            if mode == "move":
                # Web 虚拟摇杆走厂商“手动控制源”通道。
                self.send_simple_cmd(CMD_MANUAL_MODE, 0)
                time.sleep(0.08)
                self.send_simple_cmd(CMD_MOVE_MODE, 0)

                # 若此前没有明确步态，则默认低速。
                if self.gait_mode not in ("slow", "medium", "fast"):
                    time.sleep(0.08)
                    self.send_simple_cmd(CMD_GAIT_SLOW, 0)
                    self.gait_mode = "slow"

                self.motion_mode = "move"
                self.control_prepared = True
                self.prepare_attempted = True
                return True, "已切换到移动模式；WASD 已允许发送移动轴指令"

            if mode == "spot":
                self.send_simple_cmd(CMD_SPOT_MODE, 0)
                self.motion_mode = "spot"
                self.control_prepared = False
                self.prepare_attempted = True
                return True, "已切换到原地模式；为防误操作，WASD 移动轴已禁用"

            return False, "未知运动模式"

        except Exception as exc:
            return False, "运动模式切换失败: %s" % exc

    def set_gait(self, gait):
        """Web 切换平地低速/中速/高速步态。"""
        if not self.control_enabled:
            return False, "运动控制未启用；启动时添加 --enable-control"

        if self.is_action_locked():
            return False, "姿态动作执行期间不能切换步态"

        if self.motion_mode != "move":
            return False, "请先切换到移动模式，再切换步态"

        gait_map = {
            "slow": (CMD_GAIT_SLOW, "低速"),
            "medium": (CMD_GAIT_MEDIUM, "中速"),
            "fast": (CMD_GAIT_FAST, "高速"),
        }

        if gait not in gait_map:
            return False, "未知步态"

        code, label = gait_map[gait]

        try:
            self.clear_keys()
            self.publish_stop(repeat=3)
            self.send_simple_cmd(code, 0)
            self.gait_mode = gait
            return True, "已切换到平地%s步态" % label
        except Exception as exc:
            return False, "步态切换失败: %s" % exc

    def publish_stop(self, repeat=3):
        if not self.control_enabled:
            return

        for _ in range(max(1, repeat)):
            self.send_simple_cmd(CMD_AXIS_FORWARD, 0)
            self.send_simple_cmd(CMD_AXIS_LATERAL, 0)
            self.send_simple_cmd(CMD_AXIS_YAW, 0)
            if repeat > 1:
                time.sleep(0.03)

    def send_simple_cmd(self, cmd_code, cmd_value=0):
        msg = MotionSimpleCMD()
        msg.cmd_code = int(cmd_code)
        # Lite3 ROS2 消息该字段名为 size，桥接端会原样写入 SimpleCMD 第二个 int32。
        msg.size = int(cmd_value)
        msg.type = 0
        self.simple_cmd_pub.publish(msg)

    def trigger_action(self, action):
        if not self.actions_enabled:
            return False, "动作控制未启用；启动时添加 --enable-actions"

        if self.is_action_locked():
            remaining = max(
                0.0,
                self.action_lock_until - time.monotonic()
            )
            return False, "上一动作仍在锁定期，剩余 %.1f 秒" % remaining

        self.clear_keys()
        self.publish_stop(repeat=5)

        if action == "init":
            cmd_code = self.args.init_cmd_code
            cmd_value = self.args.init_cmd_value
            lock_seconds = self.args.init_lock
            label = "初始化/回零"

        elif action == "stand":
            cmd_code = self.args.stand_lie_cmd_code
            cmd_value = self.args.stand_value
            lock_seconds = self.args.stand_lock
            label = "起立"

        elif action == "lie":
            cmd_code = self.args.stand_lie_cmd_code
            cmd_value = self.args.lie_value
            lock_seconds = self.args.lie_lock
            label = "趴下"

        else:
            return False, "未知动作"

        self.action_lock_until = time.monotonic() + lock_seconds

        try:
            self.send_simple_cmd(cmd_code, cmd_value)
            self.last_action = label
            self.last_action_time = time.time()
            self.last_action_result = "已发送"

            # 起立/趴下/回零后不假设 firmware 仍保持移动模式。
            # 用户可在 Web 中重新点击“移动模式”后继续 WASD。
            self.motion_mode = "unknown"
            self.control_prepared = False

            return True, "%s 指令已发送；如需继续 WASD，请重新点击“移动模式”" % label
        except Exception as exc:
            self.action_lock_until = 0.0
            self.last_action = label
            self.last_action_time = time.time()
            self.last_action_result = "发送失败: %s" % exc
            return False, self.last_action_result

    @staticmethod
    def _fresh(last_rx, threshold=1.0):
        return (
            last_rx > 0.0
            and (time.monotonic() - last_rx) <= threshold
        )

    def snapshot(self):
        now = time.monotonic()

        with self.state_lock:
            joints = {
                "names": list(self.joint_names),
                "position_rad": list(self.joint_positions),
                "position_deg": [
                    math.degrees(v) for v in self.joint_positions
                ],
                "velocity_est_rad_s": list(self.joint_vel_est),
                "velocity_source": "estimated_from_position_difference",
            }

            imu = dict(self.imu) if self.imu is not None else None
            odom = dict(self.odom) if self.odom is not None else None
            ultrasound = self.ultrasound

            lj = self.last_joint_rx
            li = self.last_imu_rx
            lo = self.last_odom_rx
            lu = self.last_ultrasound_rx

        with self.control_lock:
            keys = sorted(self.pressed_keys)
            hb_age = (
                None
                if self.last_browser_heartbeat <= 0.0
                else now - self.last_browser_heartbeat
            )

        remaining = max(0.0, self.action_lock_until - now)

        return {
            "time": time.time(),
            "ros": {
                "node": self.get_name(),
                "movement_backend": "lite3_simple_axis",
                "simple_cmd_topic": self.args.simple_cmd_topic,
                "simple_cmd_subscribers": self.simple_cmd_pub.get_subscription_count(),
            },
            "enabled": {
                "control": self.control_enabled,
                "actions": self.actions_enabled,
            },
            "topics": {
                "joint_states": {
                    "online": self._fresh(lj),
                    "age_s": None if lj <= 0.0 else now - lj,
                },
                "imu": {
                    "online": self._fresh(li),
                    "age_s": None if li <= 0.0 else now - li,
                },
                "odom": {
                    "online": self._fresh(lo),
                    "age_s": None if lo <= 0.0 else now - lo,
                },
                "ultrasound": {
                    "online": self._fresh(lu),
                    "age_s": None if lu <= 0.0 else now - lu,
                },
            },
            "control": {
                "pressed_keys": keys,
                "heartbeat_age_s": hb_age,
                "watchdog_s": self.args.watchdog,
                "axis_forward": self.args.axis_forward,
                "axis_lateral": self.args.axis_lateral,
                "axis_yaw": self.args.axis_yaw,
                "control_prepared": self.control_prepared,
                "motion_mode": self.motion_mode,
                "gait_mode": self.gait_mode,
                "heartbeat_rate_hz": self.args.heartbeat_rate,
                "action_locked": remaining > 0.0,
                "action_lock_remaining_s": remaining,
            },
            "last_action": {
                "name": self.last_action,
                "unix_time": self.last_action_time,
                "result": self.last_action_result,
            },
            "joints": joints,
            "imu": imu,
            "odom": odom,
            "ultrasound_m": ultrasound,
        }


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lite3 ROS2 Web Console v3.2</title>
<style>
:root{--bg:#101418;--panel:#171d22;--panel2:#20282f;--text:#e8edf2;--muted:#95a2ad;--ok:#4dd27c;--bad:#ff6b6b;--warn:#f0c75e;--line:#2d3841;--accent:#62a9ff}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{width:min(1200px,96vw);margin:20px auto 40px} h1{margin:0 0 6px;font-size:26px}.sub{color:var(--muted);margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}.full{grid-column:1/-1}
.title{font-size:16px;font-weight:700;margin-bottom:12px}.row{display:flex;justify-content:space-between;gap:16px;padding:5px 0;border-bottom:1px dashed rgba(255,255,255,.06)}
.row:last-child{border-bottom:0}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
kbd{display:inline-flex;width:56px;height:56px;align-items:center;justify-content:center;background:var(--panel2);border:1px solid #3b4852;border-bottom-width:3px;border-radius:8px;font-size:20px;user-select:none}
kbd.on{background:#24466d;border-color:var(--accent)}.keys{display:grid;grid-template-columns:repeat(3,56px);grid-template-rows:repeat(2,56px);gap:8px;justify-content:center;margin:10px 0 16px}
.actions{display:flex;gap:10px;flex-wrap:wrap} button{appearance:none;border:1px solid #40505d;background:var(--panel2);color:var(--text);padding:11px 16px;border-radius:8px;cursor:pointer;font-size:15px}
button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:not-allowed}.danger{border-color:#824646;background:#402020}.primary{border-color:#3e6da2;background:#203a57}.selected{border-color:var(--ok)!important;background:#173b28!important;color:#bff5d1!important}
.notice{margin-top:10px;color:var(--muted);font-size:13px;line-height:1.5}.table-wrap{overflow:auto;max-height:440px}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:right;padding:7px 9px;border-bottom:1px solid var(--line);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}th:first-child,td:first-child{text-align:left}
pre{background:#0c1013;padding:12px;border-radius:8px;overflow:auto;white-space:pre-wrap;margin:0;font-size:12px}.statusline{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.pill{padding:5px 9px;border-radius:999px;background:var(--panel2);font-size:12px}@media(max-width:820px){.grid{grid-template-columns:1fr}.full{grid-column:auto}}
</style>
</head>
<body>
<div class="wrap">
<h1>Lite3 ROS2 Web Console v3.2</h1>
<div class="sub">ROS2 状态监控 + 浏览器 WASD（Lite3 虚拟摇杆）+ 姿态动作控制</div>
<div class="grid">

<section class="card">
<div class="title">ROS2 状态</div>
<div id="topicStatus"></div>
<div class="notice">本版本不监听 UDP 43897，避免与 jetson2motion / motion_receiver 冲突。</div>
</section>

<section class="card">
<div class="title">运动控制</div>
<div class="statusline"><span class="pill" id="controlEnabled">控制：--</span><span class="pill" id="watchdog">Watchdog：--</span></div>
<div class="keys">
<kbd data-key="q">Q</kbd><kbd data-key="w">W</kbd><kbd data-key="e">E</kbd>
<kbd data-key="a">A</kbd><kbd data-key="s">S</kbd><kbd data-key="d">D</kbd>
</div>
<div class="actions"><button class="danger" onclick="stopMotion()">SPACE / STOP</button></div>
<div class="notice">W/S 前后，A/D 左右，Q/E 原地旋转。使用 Lite3 简单轴指令。页面失焦或网络中断后由 watchdog 自动归零。</div>
</section>

<section class="card">
<div class="title">运动模式 / 步态</div>

<div class="statusline">
<span class="pill" id="motionModeState">模式：--</span>
<span class="pill" id="gaitModeState">步态：--</span>
</div>

<div class="notice" style="margin:0 0 8px">运动模式</div>
<div class="actions">
<button id="btnMoveMode" class="primary" onclick="setMotionMode('move')">移动模式</button>
<button id="btnSpotMode" onclick="setMotionMode('spot')">原地模式</button>
</div>

<div class="notice" style="margin:14px 0 8px">平地步态（仅移动模式有效）</div>
<div class="actions">
<button id="btnGaitSlow" onclick="setGait('slow')">低速</button>
<button id="btnGaitMedium" onclick="setGait('medium')">中速</button>
<button id="btnGaitFast" onclick="setGait('fast')">高速</button>
</div>

<div class="notice" id="modeMsg">
原地模式下，同一组轴指令可能用于调整身体姿态，因此本页面会自动禁用 WASD 移动输出。
</div>
</section>

<section class="card">
<div class="title">姿态动作</div>
<div class="statusline"><span class="pill" id="actionsEnabled">动作：--</span><span class="pill" id="actionLock">锁定：--</span></div>
<div class="actions">
<button id="btnInit" onclick="runAction('init',true)">初始化 / 回零</button>
<button id="btnStand" class="primary" onclick="runAction('stand',false)">起立</button>
<button id="btnLie" onclick="runAction('lie',false)">趴下</button>
</div>
<div class="notice" id="actionMsg">动作执行前会清空键盘状态并发布零速度。初始化按钮需要二次确认。</div>
</section>

<section class="card">
<div class="title">IMU / 里程计 / 超声波</div>
<pre id="motionState">等待 ROS2 数据...</pre>
</section>

<section class="card full">
<div class="title">12 关节状态</div>
<div class="notice" style="margin:0 0 10px">position 为 ROS2 原始数据；velocity 为 position 差分并低通后的估计值。</div>
<div class="table-wrap"><table>
<thead><tr><th>Joint</th><th>Position(rad)</th><th>Position(deg)</th><th>Velocity est.(rad/s)</th></tr></thead>
<tbody id="jointBody"></tbody>
</table></div>
</section>

<section class="card full">
<div class="title">调试信息</div>
<pre id="debug">等待...</pre>
</section>

</div></div>

<script>
const pressed=new Set();
function fmt(v,n=3){if(v===null||v===undefined||Number.isNaN(v))return"--";return Number(v).toFixed(n)}
async function postJSON(url,data){
 const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data||{})});
 let p={};try{p=await r.json()}catch(_){}
 if(!r.ok)throw new Error(p.error||("HTTP "+r.status));return p
}
function syncKeys(){
 postJSON("/api/control",{keys:[...pressed]}).catch(()=>{});
 document.querySelectorAll("kbd[data-key]").forEach(el=>el.classList.toggle("on",pressed.has(el.dataset.key)))
}
function clearKeys(){pressed.clear();syncKeys()}
document.addEventListener("keydown",ev=>{
 if(ev.repeat)return;
 if(ev.code==="Space"){ev.preventDefault();stopMotion();return}
 const k=ev.key.toLowerCase();if("wsadqe".includes(k)){ev.preventDefault();pressed.add(k);syncKeys()}
});
document.addEventListener("keyup",ev=>{
 const k=ev.key.toLowerCase();if("wsadqe".includes(k)){ev.preventDefault();pressed.delete(k);syncKeys()}
});
window.addEventListener("blur",clearKeys);
document.addEventListener("visibilitychange",()=>{if(document.hidden)clearKeys()});

async function stopMotion(){pressed.clear();syncKeys();try{await postJSON("/api/stop",{})}catch(_){}}

async function setMotionMode(mode){
 clearKeys();
 try{
  const r=await postJSON("/api/mode/"+mode,{});
  document.getElementById("modeMsg").textContent=r.message||"模式已切换";
 }catch(e){
  document.getElementById("modeMsg").textContent="模式切换失败："+e.message;
 }
}

async function setGait(gait){
 clearKeys();
 try{
  const r=await postJSON("/api/gait/"+gait,{});
  document.getElementById("modeMsg").textContent=r.message||"步态已切换";
 }catch(e){
  document.getElementById("modeMsg").textContent="步态切换失败："+e.message;
 }
}

async function runAction(name,needConfirm){
 if(needConfirm&&!confirm("确认执行 Lite3 初始化 / 关节回零？\n\n请确保机器人周围无障碍物，并可随时使用物理急停。"))return;
 clearKeys();
 try{
  const r=await postJSON("/api/action/"+name,{});
  document.getElementById("actionMsg").textContent=r.message||"动作已发送";
 }catch(e){document.getElementById("actionMsg").textContent="动作失败："+e.message}
}
function topicLine(name,obj){
 const online=obj&&obj.online,age=obj&&obj.age_s;
 return `<div class="row"><span>${name}</span><span class="${online?'ok':'bad'}">${online?'ONLINE':'OFFLINE'}${age===null||age===undefined?'':' / '+fmt(age,2)+' s'}</span></div>`;
}
function updatePage(s){
 document.getElementById("topicStatus").innerHTML=
  topicLine("/joint_states",s.topics.joint_states)+topicLine("/imu/data",s.topics.imu)+
  topicLine("/leg_odom2",s.topics.odom)+topicLine("/us_publisher/ultrasound_distance",s.topics.ultrasound);

 const ce=document.getElementById("controlEnabled");ce.textContent="控制："+(s.enabled.control?"ENABLED":"DISABLED");ce.className="pill "+(s.enabled.control?"ok":"warn");
 document.getElementById("watchdog").textContent=
 "Watchdog："+fmt(s.control.watchdog_s,2)+" s / Axis准备："+(s.control.control_prepared?"YES":"NO");

 const motionMode=s.control.motion_mode||"unknown";
 const gaitMode=s.control.gait_mode||"unknown";

 const mm=document.getElementById("motionModeState");
 mm.textContent="模式："+({move:"移动",spot:"原地",unknown:"未知"}[motionMode]||motionMode);
 mm.className="pill "+(motionMode==="move"?"ok":(motionMode==="spot"?"warn":""));

 const gm=document.getElementById("gaitModeState");
 gm.textContent="步态："+({slow:"低速",medium:"中速",fast:"高速",unknown:"未知"}[gaitMode]||gaitMode);
 gm.className="pill "+(gaitMode!=="unknown"?"ok":"");

 const modeDisabled=!s.enabled.control||s.control.action_locked;
 document.getElementById("btnMoveMode").disabled=modeDisabled;
 document.getElementById("btnSpotMode").disabled=modeDisabled;
 document.getElementById("btnGaitSlow").disabled=modeDisabled||motionMode!=="move";
 document.getElementById("btnGaitMedium").disabled=modeDisabled||motionMode!=="move";
 document.getElementById("btnGaitFast").disabled=modeDisabled||motionMode!=="move";

 document.getElementById("btnMoveMode").classList.toggle("selected",motionMode==="move");
 document.getElementById("btnSpotMode").classList.toggle("selected",motionMode==="spot");
 document.getElementById("btnGaitSlow").classList.toggle("selected",gaitMode==="slow");
 document.getElementById("btnGaitMedium").classList.toggle("selected",gaitMode==="medium");
 document.getElementById("btnGaitFast").classList.toggle("selected",gaitMode==="fast");

 const ae=document.getElementById("actionsEnabled");ae.textContent="动作："+(s.enabled.actions?"ENABLED":"DISABLED");ae.className="pill "+(s.enabled.actions?"ok":"warn");

 const locked=s.control.action_locked,lock=document.getElementById("actionLock");
 lock.textContent=locked?"锁定："+fmt(s.control.action_lock_remaining_s,1)+" s":"锁定：NO";lock.className="pill "+(locked?"warn":"ok");
 for(const id of["btnInit","btnStand","btnLie"])document.getElementById(id).disabled=!s.enabled.actions||locked;

 const im=s.imu,od=s.odom;
 document.getElementById("motionState").textContent=JSON.stringify({
  imu_rpy_deg:im?im.rpy_deg:null,
  imu_angular_velocity:im?im.angular_velocity:null,
  imu_linear_acceleration:im?im.linear_acceleration:null,
  odom_position:od?od.position:null,
  odom_orientation_deg:od?od.orientation_deg:null,
  odom_linear_velocity:od?od.linear_velocity:null,
  odom_angular_velocity:od?od.angular_velocity:null,
  ultrasound_m:s.ultrasound_m
 },null,2);

 const jb=document.getElementById("jointBody");jb.innerHTML="";
 const names=s.joints.names||[],pr=s.joints.position_rad||[],pd=s.joints.position_deg||[],vv=s.joints.velocity_est_rad_s||[];
 names.forEach((name,i)=>{
  const tr=document.createElement("tr");
  tr.innerHTML=`<td>${name}</td><td>${fmt(pr[i],5)}</td><td>${fmt(pd[i],2)}</td><td>${fmt(vv[i],4)}</td>`;
  jb.appendChild(tr)
 });
 document.getElementById("debug").textContent=JSON.stringify({ros:s.ros,enabled:s.enabled,control:s.control,last_action:s.last_action},null,2);
}
async function refresh(){
 try{const r=await fetch("/api/status",{cache:"no-store"});updatePage(await r.json())}
 catch(e){document.getElementById("debug").textContent="Web/ROS 状态读取失败："+e.message}
}
setInterval(()=>{postJSON("/api/heartbeat",{}).catch(()=>{})},100);
setInterval(refresh,250);refresh();
</script>
</body>
</html>
"""


class WebContext:
    def __init__(self):
        self.node = None


CTX = WebContext()


class Lite3RequestHandler(BaseHTTPRequestHandler):
    server_version = "Lite3Web/3.2"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, obj, status=200):
        raw = json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":")
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html):
        raw = html.encode("utf-8")
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0

        if n <= 0:
            return {}

        try:
            return json.loads(
                self.rfile.read(n).decode("utf-8")
            )
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self._send_html(HTML)
            return

        if path == "/api/status":
            self._send_json(CTX.node.snapshot())
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()

        if path == "/api/heartbeat":
            CTX.node.browser_heartbeat()
            self._send_json({"ok": True})
            return

        if path == "/api/control":
            if not CTX.node.control_enabled:
                CTX.node.clear_keys()
                self._send_json(
                    {
                        "error": (
                            "运动控制未启用；"
                            "请使用 --enable-control 启动"
                        )
                    },
                    403
                )
                return

            CTX.node.update_keys(data.get("keys", []))
            self._send_json({"ok": True})
            return

        if path == "/api/stop":
            CTX.node.clear_keys()
            CTX.node.publish_stop(repeat=5)
            self._send_json(
                {"ok": True, "message": "已发送 STOP"}
            )
            return

        if path.startswith("/api/mode/"):
            mode = path.rsplit("/", 1)[-1]
            ok, message = CTX.node.set_motion_mode(mode)
            self._send_json(
                {"ok": ok, "message": message},
                200 if ok else 409
            )
            return

        if path.startswith("/api/gait/"):
            gait = path.rsplit("/", 1)[-1]
            ok, message = CTX.node.set_gait(gait)
            self._send_json(
                {"ok": ok, "message": message},
                200 if ok else 409
            )
            return

        if path.startswith("/api/action/"):
            action = path.rsplit("/", 1)[-1]
            ok, message = CTX.node.trigger_action(action)
            self._send_json(
                {"ok": ok, "message": message},
                200 if ok else 409
            )
            return

        self._send_json({"error": "not found"}, 404)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Lite3 ROS2 Web Console v3.2"
    )

    parser.add_argument("--web-host", default="0.0.0.0")
    parser.add_argument("--web-port", type=int, default=8080)

    parser.add_argument(
        "--enable-control",
        action="store_true",
        help="允许浏览器发布 /cmd_vel"
    )
    parser.add_argument(
        "--enable-actions",
        action="store_true",
        help="允许初始化/起立/趴下"
    )

    parser.add_argument(
        "--simple-cmd-topic",
        default="/simple_cmd"
    )
    parser.add_argument(
        "--joint-topic",
        default="/joint_states"
    )
    parser.add_argument("--imu-topic", default="/imu/data")
    parser.add_argument("--odom-topic", default="/leg_odom2")
    parser.add_argument(
        "--ultrasound-topic",
        default="/us_publisher/ultrasound_distance"
    )

    parser.add_argument(
        "--axis-forward", type=int, default=8000,
        help="前后轴幅值；协议死区约 +/-6553，默认 8000"
    )
    parser.add_argument(
        "--axis-lateral", type=int, default=14000,
        help="横移轴幅值；协议死区约 +/-12553，默认 14000"
    )
    parser.add_argument(
        "--axis-yaw", type=int, default=11000,
        help="转向轴幅值；协议死区约 +/-9553，默认 11000"
    )
    parser.add_argument(
        "--rate", type=float, default=20.0,
        help="轴指令发布频率 Hz，默认 20"
    )
    parser.add_argument(
        "--heartbeat-rate", type=float, default=4.0,
        help="Lite3 心跳频率 Hz，必须 >=2，默认 4"
    )
    parser.add_argument(
        "--watchdog", type=float, default=0.35
    )

    parser.add_argument(
        "--joint-velocity-alpha",
        type=float,
        default=0.20
    )

    parser.add_argument(
        "--init-cmd-code",
        type=lambda x: int(x, 0),
        default=CMD_INIT_ZERO
    )
    parser.add_argument(
        "--init-cmd-value",
        type=int,
        default=0
    )
    parser.add_argument(
        "--stand-lie-cmd-code",
        type=lambda x: int(x, 0),
        default=CMD_STAND_LIE
    )
    parser.add_argument(
        "--stand-value",
        type=int,
        default=STAND_VALUE
    )
    parser.add_argument(
        "--lie-value",
        type=int,
        default=LIE_VALUE
    )

    parser.add_argument(
        "--init-lock", type=float, default=4.0
    )
    parser.add_argument(
        "--stand-lock", type=float, default=3.0
    )
    parser.add_argument(
        "--lie-lock", type=float, default=3.0
    )

    return parser


def print_startup(args):
    ips = get_host_ipv4_addresses()

    print("")
    print("=" * 62)
    print(" Lite3 ROS2 Web Console v3.2")
    print("-" * 62)
    print(
        " Web bind       : %s:%d" %
        (args.web_host, args.web_port)
    )
    print(" Movement       : Lite3 simple-axis / virtual joystick")
    print(
        " /simple_cmd     : %s" %
        args.simple_cmd_topic
    )
    print(
        " Control         : %s" %
        (
            "ENABLED"
            if args.enable_control
            else "DISABLED (monitor only)"
        )
    )
    print(
        " Actions         : %s" %
        (
            "ENABLED"
            if args.enable_actions
            else "DISABLED"
        )
    )
    print(
        " Axis values     : forward=%d  lateral=%d  yaw=%d" %
        (
            args.axis_forward,
            args.axis_lateral,
            args.axis_yaw
        )
    )
    print(" Heartbeat       : %.1f Hz" % args.heartbeat_rate)
    print(" Watchdog        : %.2f s" % args.watchdog)
    print("-" * 62)

    if ips:
        print(" Browser URLs:")
        for ip in ips:
            print(
                "   http://%s:%d" %
                (ip, args.web_port)
            )
    else:
        print(
            " Browser URL: http://<Lite3-IP>:%d" %
            args.web_port
        )

    print("-" * 62)
    print(" IMPORTANT:")
    print("   1) 本程序不会占用 UDP 43897。")
    print("   2) 控制后端使用 MANUAL + MOVE + 简单轴指令，不使用旧复杂速度接口。")
    print("   3) 首次实机测试请准备物理急停，并先短按 W 验证。")
    print("   4) 起立/趴下动作需确认与你当前固件一致。")
    print("=" * 62)
    print("")


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if not (0.0 < args.joint_velocity_alpha <= 1.0):
        parser.error(
            "--joint-velocity-alpha 必须在 (0,1]"
        )

    if args.watchdog <= 0.0:
        parser.error("--watchdog 必须 > 0")

    if args.heartbeat_rate < 2.0:
        parser.error("--heartbeat-rate 必须 >= 2 Hz")

    for name, value in (
        ("--axis-forward", args.axis_forward),
        ("--axis-lateral", args.axis_lateral),
        ("--axis-yaw", args.axis_yaw),
    ):
        if not (0 <= value <= 32767):
            parser.error("%s 必须在 [0,32767]" % name)

    rclpy.init()

    node = None
    executor = None
    spin_thread = None
    httpd = None

    try:
        node = Lite3WebNode(args)
        CTX.node = node

        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(node)

        spin_thread = threading.Thread(
            target=executor.spin,
            name="ros2_executor",
            daemon=True
        )
        spin_thread.start()

        httpd = ThreadingHTTPServer(
            (args.web_host, args.web_port),
            Lite3RequestHandler
        )

        print_startup(args)

        def _signal_handler(signum, frame):
            raise KeyboardInterrupt

        signal.signal(
            signal.SIGTERM,
            _signal_handler
        )

        httpd.serve_forever(
            poll_interval=0.25
        )

    except KeyboardInterrupt:
        print("\n[SYS] stopping...")

    except OSError as exc:
        print(
            "[ERROR] Web server start failed: %s" %
            exc
        )
        raise

    finally:
        if node is not None:
            try:
                node.clear_keys()
                node.publish_stop(repeat=10)
            except Exception:
                pass

        if httpd is not None:
            try:
                httpd.server_close()
            except Exception:
                pass

        if executor is not None:
            try:
                executor.shutdown()
            except Exception:
                pass

        if (
            spin_thread is not None
            and spin_thread.is_alive()
        ):
            spin_thread.join(timeout=1.0)

        if node is not None:
            try:
                node.destroy_node()
            except Exception:
                pass

        try:
            if rclpy.ok():
                rclpy.shutdown()
        except Exception:
            pass

        print("[SYS] exited")


if __name__ == "__main__":
    main()
