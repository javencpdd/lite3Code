"""机器人状态管理器。

职责：
1. 保存「最新」的机器人状态，而不是历史队列（历史曲线交给前端缓存）；
2. 维护原始报文的环形缓存，用于协议调试；
3. 负责链路在线判定（超过 link_timeout 未收到数据即离线）；
4. 所有对外读接口先加锁拷贝，保证线程安全。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from config import service_config
from models import MonitorState, RawPacketItem, RawPacketResponse, ServiceStatus
from parser import to_hex_preview

logger = logging.getLogger(__name__)


class StateManager:
    """线程安全的最新状态仓库。"""

    def __init__(
        self,
        link_timeout: float = service_config.link_timeout,
        raw_history_size: int = service_config.raw_history_size,
        raw_preview_bytes: int = service_config.raw_preview_bytes,
    ) -> None:
        # 离线判定阈值（秒）
        self._link_timeout: float = link_timeout
        # 原始报文环形缓存容量
        self._raw_history_size: int = raw_history_size
        self._raw_preview_bytes: int = raw_preview_bytes

        # 数据锁：UDP 消费线程与 HTTP/WS 读取之间互斥
        self._lock: threading.Lock = threading.RLock()

        # 最新状态：按消息类型分类保存
        self._robot_state: Optional[Dict[str, Any]] = None
        self._joint_angle: Optional[Dict[str, Any]] = None
        self._joint_velocity: Optional[Dict[str, Any]] = None
        # 未支持消息码的最新一条
        self._others: Dict[str, Dict[str, Any]] = {}

        # 原始报文环形缓存
        self._raw_packets: Deque[Dict[str, Any]] = deque(maxlen=raw_history_size)

        # 时间与计数统计
        self._last_update: Optional[float] = None
        self._started_at: float = time.time()
        self._parsed_count: int = 0
        self._error_count: int = 0
        self._ws_clients: int = 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def update(self, payload: Dict[str, Any], raw: Optional[bytes] = None) -> None:
        """写入一帧解析后的数据。

        Args:
            payload: parser.parse_packet() 返回的结构化 dict
            raw:     原始 UDP 报文字节，用于 raw 面板展示（可选）
        """
        if not isinstance(payload, dict):
            logger.warning("StateManager.update 收到非法数据: %r", type(payload))
            return

        packet_type: str = payload.get("type", "unknown")
        with self._lock:
            if packet_type == "robot_state":
                self._robot_state = payload
                self._parsed_count += 1
            elif packet_type == "joint_angle":
                self._joint_angle = payload
                self._parsed_count += 1
            elif packet_type == "joint_velocity":
                self._joint_velocity = payload
                self._parsed_count += 1
            elif packet_type == "unknown":
                # 未支持消息码按 code 去重保存最新一条
                self._others[str(payload.get("code", "unknown"))] = payload
            elif packet_type == "error":
                self._error_count += 1
                logger.debug("解析错误帧: %s", payload.get("error"))
            else:
                logger.debug("未知 payload 类型: %s", packet_type)

            if packet_type != "error":
                self._last_update = payload.get("timestamp", time.time())

            if raw is not None:
                self._append_raw_locked(payload, raw)

    def _append_raw_locked(self, payload: Dict[str, Any], raw: bytes) -> None:
        """把原始报文写入环形缓存（调用方需先持锁）。"""
        source = payload.get("source") or {}
        self._raw_packets.append(
            {
                "code": str(payload.get("code", "")),
                "code_value": int(payload.get("code_value", 0) or 0),
                "length": int(payload.get("length", len(raw)) or len(raw)),
                "source_ip": source.get("ip"),
                "source_port": source.get("port"),
                "received_at": float(payload.get("timestamp", time.time())),
                "hex_preview": to_hex_preview(raw, self._raw_preview_bytes),
            }
        )

    def set_ws_clients(self, count: int) -> None:
        """更新当前 WebSocket 客户端数量（由 main.py 维护）。"""
        with self._lock:
            self._ws_clients = int(count)

    def clear_raw(self) -> None:
        """清空原始报文缓存。"""
        with self._lock:
            self._raw_packets.clear()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """返回一次完整快照 dict（可直接 JSON 序列化）。"""
        with self._lock:
            return {
                "type": "snapshot",
                "server_time": time.time(),
                "robot_state": self._robot_state,
                "joint_angle": self._joint_angle,
                "joint_velocity": self._joint_velocity,
                "others": list(self._others.values()),
                "connected": self.is_connected_locked(),
            }

    def get_state(self) -> MonitorState:
        """返回校验后的 Pydantic 快照模型，用于 REST 响应。"""
        return MonitorState.model_validate(self.snapshot())

    def get_raw_packets(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """返回原始报文列表（倒序：最新在前）。"""
        with self._lock:
            items = list(self._raw_packets)
        items.reverse()
        if limit is not None and limit > 0:
            items = items[:limit]
        return items

    def get_raw_response(self, limit: Optional[int] = None) -> RawPacketResponse:
        """返回 /api/raw 的响应模型。"""
        items = [RawPacketItem.model_validate(item) for item in self.get_raw_packets(limit)]
        return RawPacketResponse(count=len(items), items=items)

    def is_connected_locked(self) -> bool:
        """链路在线判定（调用方需持锁）。"""
        if self._last_update is None:
            return False
        return (time.time() - self._last_update) <= self._link_timeout

    def get_status(self, udp_stats: Optional[Dict[str, Any]] = None,
                   push_hz: int = service_config.push_hz) -> ServiceStatus:
        """返回服务运行状态。

        Args:
            udp_stats: UDPReceiver.stats() 的输出
            push_hz:   当前 WebSocket 推送频率
        """
        stats = udp_stats or {}
        with self._lock:
            last_update = self._last_update
            connected = self.is_connected_locked()
            ws_clients = self._ws_clients
            parsed = self._parsed_count
            errors = self._error_count

        since: Optional[float] = None
        if last_update is not None:
            since = round(time.time() - last_update, 3)

        return ServiceStatus(
            connected=connected,
            last_update=last_update,
            since_last_update=since,
            udp_running=bool(stats.get("running", False)),
            udp_mode=str(stats.get("mode", "bind")),
            udp_host=str(stats.get("host", "")),
            udp_port=int(stats.get("port", 0) or 0),
            packets_received=int(stats.get("received_packets", 0)),
            packets_dropped=int(stats.get("dropped_packets", 0)),
            packets_parsed=parsed,
            packets_error=errors,
            ws_clients=ws_clients,
            push_hz=int(push_hz),
        )

    def counters(self) -> Dict[str, Any]:
        """返回内部计数器，便于调试。"""
        with self._lock:
            return {
                "parsed": self._parsed_count,
                "error": self._error_count,
                "last_update": self._last_update,
                "uptime": round(time.time() - self._started_at, 3),
            }
