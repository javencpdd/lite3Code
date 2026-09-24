"""Lite3 控制服务：UDP 发送、心跳保活、安全策略与审计。

设计要点
--------
1. **默认禁用**：必须显式调用 :meth:`enable` 才会真正发包，避免误操作；
2. **心跳保活**：官方 SDK 说明「1s 无指令下发则底层收回控制权并进入阻尼保护」，
   因此控制期间需要以固定周期（默认 250ms）持续下发当前速度；
3. **租约机制**：浏览器崩溃、断网、直接关闭页面时，前端不会有机会通知后端停止。
   为此引入心跳租约——前端每次操作都会续约，租约超时（默认 5s）
   后端自动停止心跳并下发零速，**杜绝后台持续发送**；
4. **硬性限幅**：速度上限在后端二次夹取，不信任前端传入值；
5. **急停**：独立通道，立即停心跳、下发零速并锁死，需显式解除；
6. **审计**：所有下发指令带时间戳落内存环形队列，便于事后复盘。

资源清理：:meth:`close` 会停止心跳线程、发送零速、关闭 socket，
由 FastAPI 的 lifespan 在关闭时调用。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import control_protocol as cp
from config import control_config

logger = logging.getLogger(__name__)

# 审计缓冲里最多保留多少条心跳记录。
# 背景：心跳按 0.25s（4Hz）发送，而审计 deque 上限 200 条 —— 不限制的话约 50 秒
# 就会被心跳占满，把业务指令挤出历史记录，界面上看不到之前下发过什么。
AUDIT_HEARTBEAT_KEEP = 20


class ControlError(Exception):
    """控制指令下发失败（参数非法、未启用、被急停锁定等）。"""


class ControlService:
    """控制指令发送与心跳管理。"""

    def __init__(
        self,
        target_ip: str = cp.DEFAULT_TARGET_IP,
        target_port: int = cp.DEFAULT_TARGET_PORT,
        heartbeat_interval: float = 0.25,
        heartbeat_lease: float = 5.0,
        max_linear: float = cp.MAX_LINEAR_X,
        max_linear_y: float = cp.MAX_LINEAR_Y,
        max_angular: float = cp.MAX_ANGULAR,
        audit_size: int = 200,
        enabled_by_default: bool = False,
    ) -> None:
        # ---- 目标与参数 ----
        self.target_ip: str = target_ip
        self.target_port: int = target_port
        self.heartbeat_interval: float = heartbeat_interval
        self.heartbeat_lease: float = heartbeat_lease
        # 限幅取值来自文档 1.2.12：前后 ±1.0、左右 ±0.5、旋转 ±1.5
        self.max_linear: float = max_linear
        self.max_linear_y: float = max_linear_y
        self.max_angular: float = max_angular

        # ---- 运行状态 ----
        self.enabled: bool = False
        self.estop: bool = False
        self.heartbeat_running: bool = False
        self.current_velocity: Tuple[float, float, float] = (0.0, 0.0, 0.0)

        # ---- 统计与审计 ----
        self.sent_packets: int = 0
        self.failed_packets: int = 0
        # 心跳与业务分开累计：心跳 4Hz 会把合并计数淹没，
        # 单独统计才能看出"业务实际下发了几条"。
        self.heartbeat_packets: int = 0
        self.business_packets: int = 0
        # 最近一次下发的模式类指令。
        # 协议 0x0901 只上报 basic_state / gait_state / policy_state，**没有**
        # 运动模式、控制模式、持续运动的反馈字段，只能靠"我方最后下发了什么"来推断。
        self.last_modes: Dict[str, Any] = {}
        self.last_sent_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self._audit: "Deque[Dict[str, Any]]" = deque(maxlen=audit_size)

        # ---- 线程与锁 ----
        self._lock: threading.RLock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop: threading.Event = threading.Event()
        self._lease_deadline: float = 0.0

        if enabled_by_default:
            self.enable()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def enable(self) -> None:
        """启用控制（默认关闭，需显式调用）。"""
        with self._lock:
            if self.enabled:
                return
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            except OSError as exc:
                self.last_error = "创建 UDP socket 失败: %s" % exc
                logger.error(self.last_error)
                raise ControlError(self.last_error) from exc
            self.enabled = True
            self.estop = False
            logger.warning(
                "控制通道已启用 → %s:%s（直连闭源运动控制，会绕过 VOA 安全层）",
                self.target_ip, self.target_port,
            )

    def disable(self) -> None:
        """停用控制：停止心跳、下发零速、释放 socket。"""
        self.stop_heartbeat(send_stop=True)
        with self._lock:
            self.enabled = False
            self.current_velocity = (0.0, 0.0, 0.0)
            if self._sock:
                try:
                    self._sock.close()
                except OSError:  # pragma: no cover
                    pass
                self._sock = None
        logger.info("控制通道已停用")

    def close(self) -> None:
        """进程退出时调用，确保资源释放。"""
        self.disable()

    # ------------------------------------------------------------------
    # 底层发送
    # ------------------------------------------------------------------
    def _ensure_ready(self) -> None:
        """发送前的通用前置检查。"""
        if not self.enabled:
            raise ControlError("控制通道未启用，请先启用")
        if self.estop:
            raise ControlError("处于急停锁定状态，请先解除急停")
        if self._sock is None:
            raise ControlError("UDP socket 未就绪")

    def _send(self, payload: bytes, note: str = "") -> bool:
        """发送单个报文，失败时记录错误但不抛出（心跳线程不应因单包失败退出）。"""
        try:
            self._sock.sendto(payload, (self.target_ip, self.target_port))
        except OSError as exc:
            self.failed_packets += 1
            self.last_error = "发送失败: %s" % exc
            logger.error("控制报文发送失败: %s", exc)
            return False

        self.sent_packets += 1
        # 按 note 归类累加：heartbeat* 归心跳，其余归业务
        if note.startswith("heartbeat"):
            self.heartbeat_packets += 1
        else:
            self.business_packets += 1
        self.last_sent_at = time.time()
        self._audit_append(payload, note)
        return True

    def _send_all(self, packets: List[bytes], note: str = "") -> int:
        """批量发送，返回成功条数。"""
        ok = 0
        for payload in packets:
            if self._send(payload, note):
                ok += 1
        return ok

    def _audit_append(self, payload: bytes, note: str) -> None:
        """写入审计日志。

        心跳报文先触发一次裁剪：审计缓冲里只保留最近 AUDIT_HEARTBEAT_KEEP 条心跳，
        避免高频心跳把业务指令挤出历史记录（详见 AUDIT_HEARTBEAT_KEEP 注释）。
        """
        record = cp.describe_packet(payload)
        record["ts"] = time.time()
        record["note"] = note
        record["target"] = "%s:%s" % (self.target_ip, self.target_port)

        self._audit.append(record)

        # 裁剪放在 append 之后，保证稳态下心跳条数严格不超过 AUDIT_HEARTBEAT_KEEP
        if note.startswith("heartbeat"):
            self._trim_heartbeat_audit()

    def _trim_heartbeat_audit(self) -> None:
        """把审计里的心跳记录裁剪到最近 AUDIT_HEARTBEAT_KEEP 条。"""
        items = list(self._audit)
        hb = [r for r in items if str(r.get("note", "")).startswith("heartbeat")]
        excess = len(hb) - AUDIT_HEARTBEAT_KEEP
        if excess <= 0:
            return
        # 淘汰最早的那 excess 条心跳；业务记录一概不动
        drop = {id(r) for r in hb[:excess]}
        self._audit = deque(
            (r for r in items if id(r) not in drop),
            maxlen=self._audit.maxlen,
        )

    def renew_lease(self) -> None:
        """续约心跳租约；心跳开启期间需由前端定期调用。"""
        with self._lock:
            self._lease_deadline = time.time() + self.heartbeat_lease

    # ------------------------------------------------------------------
    # 指令接口
    # ------------------------------------------------------------------
    def send_velocity(self, linear_x: float, linear_y: float,
                      angular_z: float) -> Dict[str, Any]:
        """下发速度指令（会同步更新心跳内容）。

        方向语义（文档 1.2.12）：x 正值前进、y 正值向右、yaw 正值向右转。
        若实机方向与之相反，可用 LITE3_CTRL_INVERT_VEL_{X,Y,YAW} 逐轴取反纠正。
        ⚠️ 速度指令要求机器人已处于**自主模式**（`0x21010C03`），否则不生效。
        """
        # 方向取反配置（默认关闭 = 遵循文档）
        sx = -1.0 if control_config.invert_vel_x else 1.0
        sy = -1.0 if control_config.invert_vel_y else 1.0
        syaw = -1.0 if control_config.invert_vel_yaw else 1.0
        rx, ry, raz = linear_x * sx, linear_y * sy, angular_z * syaw

        with self._lock:
            self._ensure_ready()
            lx, ly, az = cp.clip_velocity(rx, ry, raz,
                                          self.max_linear, self.max_linear_y,
                                          self.max_angular)
            self.current_velocity = (lx, ly, az)
            packets = cp.build_velocity_packets(lx, ly, az)
            self.renew_lease()

        ok = self._send_all(packets, note="velocity")
        if ok == 0:
            raise ControlError("速度指令发送失败: %s" % (self.last_error or "未知原因"))
        return {
            "ok": True,
            "sent": ok,
            "velocity": {"x": lx, "y": ly, "yaw": az},
            # 与"取反后、限幅前"的输入比较，避免取反时误报 clipped
            "clipped": (lx != rx) or (ly != ry) or (az != raz),
            "invert": {"x": sx < 0, "y": sy < 0, "yaw": syaw < 0},
        }

    # 模式类指令 -> 类别名。用于回显"当前处于什么模式"。
    MODE_CATEGORY: Dict[str, str] = {
        "自主模式": "控制模式",
        "手动模式": "控制模式",
        "原地模式": "运动模式",
        "移动模式": "运动模式",
        "持续运动开": "持续运动",
        "持续运动关": "持续运动",
        "进入AI": "AI 状态",
        "退出AI": "AI 状态",
    }

    def _record_mode(self, name: str, preset) -> None:
        """记下这条模式类指令，供前端回显当前模式。"""
        cat = self.MODE_CATEGORY.get(name)
        if not cat:
            return
        self.last_modes[cat] = {
            "name": name,
            "code_hex": "0x%08X" % preset.code,
            "ts": time.time(),
        }

    def send_preset(self, name: str) -> Dict[str, Any]:
        """按下发预置指令（前进/后退/转向/站立…）。"""
        preset = next((p for p in cp.PRESET_COMMANDS if p.name == name), None)
        if preset is None:
            raise ControlError("未知指令: %s" % name)

        with self._lock:
            self._ensure_ready()
            self.renew_lease()

        # 部分指令需连发多条（如「停止动作」要求同时发送指令值 0 和 1）
        payloads = cp.build_from_preset(preset)
        sent = self._send_all(payloads, note="preset:%s" % name)
        if sent == 0:
            raise ControlError("指令「%s」发送失败: %s" % (name, self.last_error))

        # 记录模式类指令，供前端回显当前模式（协议无这些状态的上报字段）
        self._record_mode(name, preset)

        # 下发速度类指令后清空当前速度，避免心跳把刚发的动作冲掉
        if preset.code in (cp.CMD_VEL_X, cp.CMD_VEL_Y, cp.CMD_VEL_YAW):
            with self._lock:
                self.current_velocity = (0.0, 0.0, 0.0)

        return {
            "ok": True,
            "name": name,
            "group": preset.group,
            "danger": preset.danger,
            "sent": sent,
            "packets": [cp.describe_packet(p) for p in payloads],
            "packet": cp.describe_packet(payloads[-1]),
        }

    def send_custom(self, cmd_code: int, cmd_value: int = 0, cmd_type: int = 0,
                    data: Optional[float] = None) -> Dict[str, Any]:
        """下发自定义指令报文。"""
        with self._lock:
            self._ensure_ready()
            self.renew_lease()

        payload = cp.build_custom_packet(cmd_code, cmd_value, cmd_type, data)
        if not self._send(payload, note="custom"):
            raise ControlError("自定义指令发送失败: %s" % self.last_error)
        return {"ok": True, "packet": cp.describe_packet(payload)}

    def send_raw_hex(self, hex_str: str) -> Dict[str, Any]:
        """下发完全自定义的原始十六进制报文。"""
        payload = cp.build_hex_packet(hex_str)   # 解析失败会抛 ValueError
        with self._lock:
            self._ensure_ready()
            self.renew_lease()

        if not self._send(payload, note="raw"):
            raise ControlError("原始报文发送失败: %s" % self.last_error)
        return {"ok": True, "packet": cp.describe_packet(payload)}

    def stop_motion(self) -> Dict[str, Any]:
        """下发零速指令（停止移动，但保持心跳与连接）。"""
        return self.send_velocity(0.0, 0.0, 0.0)

    def emergency_stop(self) -> Dict[str, Any]:
        """急停：停止心跳 → 下发零速 → 发送厂商软急停指令 → 锁定后续指令。

        软急停指令码 0x21020C0E 来自文档 1.2.2，是厂家提供的急停语义；
        零速指令用于立即消除速度，两者同时发以尽快使机器人停止。
        """
        logger.warning("执行急停")
        self.stop_heartbeat(send_stop=False)
        with self._lock:
            self.estop = True
            self.current_velocity = (0.0, 0.0, 0.0)
            sock_ready = self._sock is not None

        sent = 0
        if sock_ready:
            # 1) 先连发零速，尽快消除当前速度
            for _ in range(3):
                for payload in cp.build_velocity_packets(0.0, 0.0, 0.0):
                    if self._send(payload, note="estop-velocity"):
                        sent += 1
            # 2) 再发厂商软急停指令
            for _ in range(2):
                if self._send(cp.build_simple_cmd(cp.CMD_SOFT_ESTOP, 0), note="estop-soft"):
                    sent += 1
        return {"ok": True, "estop": True, "sent": sent,
                "soft_estop_code": "0x%08X" % cp.CMD_SOFT_ESTOP}

    def clear_estop(self) -> Dict[str, Any]:
        """解除急停锁定。"""
        with self._lock:
            self.estop = False
        logger.info("急停已解除")
        return {"ok": True, "estop": False}

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    def start_heartbeat(self) -> Dict[str, Any]:
        """开启心跳（周期下发当前速度，默认零速）。

        心跳线程会在以下任一情况停止：
        - 调用 :meth:`stop_heartbeat`；
        - 控制通道被禁用 / 急停；
        - **租约超时**（前端长时间未续约，如浏览器被直接关闭）。
        """
        with self._lock:
            if not self.enabled:
                raise ControlError("控制通道未启用，无法开启心跳")
            if self.estop:
                raise ControlError("处于急停锁定状态，无法开启心跳")
            if self.heartbeat_running:
                self.renew_lease()
                return {"ok": True, "running": True, "note": "心跳已在运行"}

            self._hb_stop.clear()
            self._lease_deadline = time.time() + self.heartbeat_lease
            self.heartbeat_running = True
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name="lite3-heartbeat", daemon=True
            )
            self._hb_thread.start()

        logger.info("心跳已开启，周期 %.0f ms，租约 %.1f s",
                    self.heartbeat_interval * 1000, self.heartbeat_lease)
        return {"ok": True, "running": True}

    def stop_heartbeat(self, send_stop: bool = True) -> Dict[str, Any]:
        """停止心跳；默认会补发一次零速，避免机器人保持最后速度。"""
        with self._lock:
            if not self.heartbeat_running:
                return {"ok": True, "running": False}
            self.heartbeat_running = False
            self._hb_stop.set()
            thread = self._hb_thread
            self._hb_thread = None

        if thread and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():  # pragma: no cover - 兜底
                logger.warning("心跳线程未能在 2 秒内退出")

        # 心跳停止后补发零速：防止底层仍在执行最后一次速度指令
        if send_stop and self.enabled and self._sock is not None and not self.estop:
            for payload in cp.build_velocity_packets(0.0, 0.0, 0.0):
                self._send(payload, note="heartbeat-stop")

        logger.info("心跳已停止")
        return {"ok": True, "running": False}

    def _heartbeat_loop(self) -> None:
        """心跳主循环：周期下发当前速度。"""
        logger.debug("心跳线程启动")
        while not self._hb_stop.wait(self.heartbeat_interval):
            with self._lock:
                if not self.heartbeat_running or not self.enabled or self.estop:
                    break
                # 租约检查：前端消失（关闭页面/断网）时自动停止，避免后台持续发送
                if time.time() > self._lease_deadline:
                    logger.warning("心跳租约超时（前端未续约），自动停止心跳")
                    self.heartbeat_running = False
                    break
                lx, ly, az = self.current_velocity

            # 1) 官方心跳包（0x21040001），文档要求频率 ≥ 2Hz
            if not self._send(cp.build_simple_cmd(cp.CMD_HEARTBEAT, 0), note="heartbeat"):
                logger.error("心跳包发送失败，停止心跳")
                with self._lock:
                    self.heartbeat_running = False
                break

            # 2) 同时重发当前速度指令，避免底层因超时收回控制权
            if any(abs(v) > 1e-9 for v in (lx, ly, az)):
                for payload in cp.build_velocity_packets(lx, ly, az):
                    if not self._send(payload, note="heartbeat-velocity"):
                        logger.error("心跳速度重发失败，停止心跳")
                        with self._lock:
                            self.heartbeat_running = False
                        break

        # 退出前补发零速，确保机器人不会保持最后速度
        if self.enabled and self._sock is not None and not self.estop:
            for payload in cp.build_velocity_packets(0.0, 0.0, 0.0):
                self._send(payload, note="heartbeat-exit")

        logger.debug("心跳线程退出")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """返回控制服务状态。"""
        with self._lock:
            lease_left = max(0.0, self._lease_deadline - time.time()) \
                if self.heartbeat_running else 0.0
            return {
                "enabled": self.enabled,
                "estop": self.estop,
                "heartbeat_running": self.heartbeat_running,
                "heartbeat_interval": self.heartbeat_interval,
                "heartbeat_lease": self.heartbeat_lease,
                "lease_remaining": round(lease_left, 2),
                "target": "%s:%s" % (self.target_ip, self.target_port),
                "current_velocity": {
                    "x": self.current_velocity[0],
                    "y": self.current_velocity[1],
                    "yaw": self.current_velocity[2],
                },
                "limits": {"max_linear": self.max_linear, "max_angular": self.max_angular},
                "sent_packets": self.sent_packets,
                "failed_packets": self.failed_packets,
                "heartbeat_packets": self.heartbeat_packets,
                "business_packets": self.business_packets,
                "last_sent_at": self.last_sent_at,
                "last_error": self.last_error,
                "last_modes": dict(self.last_modes),
                "velocity_invert": {
                    "x": control_config.invert_vel_x,
                    "y": control_config.invert_vel_y,
                    "yaw": control_config.invert_vel_yaw,
                },
            }

    def audit(self, limit: int = 50) -> List[Dict[str, Any]]:
        """返回最近的指令审计记录（倒序）。"""
        items = list(self._audit)
        items.reverse()
        return items[:limit] if limit and limit > 0 else items
