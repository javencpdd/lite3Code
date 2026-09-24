"""UDP 旁路抓包接收器（Linux / AF_PACKET）。

为什么需要它
------------
103 感知导航主机的 UDP **43897 已经被 `transfer_ros2` 独占**。
UDP 单播端口被两个进程同时 bind 时，Linux 不会给两份拷贝：
后 bind 的会把报文抢走，导致 `transfer_ros2` 收不到状态，
进而 `leg_odom2` / `/imu/data` 断流、Nav2 失去本体里程计。

本模块改用 **AF_PACKET 旁路抓包**：直接从链路层"看"流经网卡的报文，
**不 bind 任何端口**，因此：

- 不影响 `transfer_ros2` 正常收包（零侵入、零端口竞争）；
- 不需要修改 120 的 `network.toml`，也不影响 103 现有服务；
- 拿到的是与 `transfer_ros2` 完全相同的原始 UDP 载荷。

代价：需要 root 或 `CAP_NET_RAW` 能力（systemd 里用 `AmbientCapabilities` 配置）。

平台：仅 Linux。Windows / macOS 请使用 `udp_receiver.UDPReceiver`（bind 模式）。
"""

from __future__ import annotations

import contextlib
import logging
import queue
import socket
import struct
import threading
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# (bytes, (ip, port)) 类型别名
Frame = Tuple[bytes, Tuple[str, int]]

# 以太网类型
ETH_P_ALL = 0x0003
ETH_TYPE_IPV4 = 0x0800
ETH_TYPE_VLAN = 0x8100      # 802.1Q
ETH_TYPE_VLAN_Q = 0x88A8    # 802.1ad（QinQ）

# 协议号
IPPROTO_UDP = 17


def parse_udp_from_ethernet(frame: bytes, target_port: int) -> Optional[Frame]:
    """从以太网帧中解析出目标端口匹配的 UDP 载荷。

    Args:
        frame:        AF_PACKET 收到的原始链路层帧
        target_port:  只保留目的端口等于该值的报文

    Returns:
        (payload, (src_ip, src_port))，不匹配时返回 None
    """
    if len(frame) < 14:
        return None

    # 以太网头：6 字节目的 MAC + 6 字节源 MAC + 2 字节类型
    eth_type = struct.unpack_from('!H', frame, 12)[0]
    offset = 14

    # 逐层剥掉 VLAN 标签
    for _ in range(2):
        if eth_type in (ETH_TYPE_VLAN, ETH_TYPE_VLAN_Q):
            if len(frame) < offset + 4:
                return None
            eth_type = struct.unpack_from('!H', frame, offset + 2)[0]
            offset += 4
        else:
            break

    if eth_type != ETH_TYPE_IPV4:
        return None

    # IPv4 头
    if len(frame) < offset + 20:
        return None
    version_ihl = frame[offset]
    if version_ihl >> 4 != 4:
        return None
    ihl = (version_ihl & 0x0F) * 4
    if ihl < 20 or frame[offset + 9] != IPPROTO_UDP:
        return None

    src_ip = '.'.join(str(b) for b in frame[offset + 12:offset + 16])

    udp_offset = offset + ihl
    if len(frame) < udp_offset + 8:
        return None
    src_port, dst_port, udp_len, _csum = struct.unpack_from('!HHHH', frame, udp_offset)
    if dst_port != target_port:
        return None

    # UDP 长度字段不可信时按剩余长度兜底
    payload_start = udp_offset + 8
    if udp_len >= 8:
        payload_end = min(len(frame), udp_offset + udp_len)
    else:
        payload_end = len(frame)
    if payload_end <= payload_start:
        return None

    return frame[payload_start:payload_end], (src_ip, int(src_port))


class UDPSniffer:
    """基于 AF_PACKET 的旁路 UDP 接收器。

    接口与 `udp_receiver.UDPReceiver` 保持一致，便于上层无感替换。
    """

    def __init__(
        self,
        port: int,
        interface: Optional[str] = None,
        timeout: float = 0.2,
        max_queue_size: int = 1024,
    ) -> None:
        self.port: int = port
        # 指定网卡时可显著减少无关流量（如 103 上拉取的 RTSP 视频流）
        self.interface: Optional[str] = interface
        self._timeout: float = timeout
        self._queue: "queue.Queue[Frame]" = queue.Queue(maxsize=max_queue_size)
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        # 统计
        self.captured_frames: int = 0
        self.matched_packets: int = 0
        self.dropped_packets: int = 0

    # ------------------------------------------------------------------
    @staticmethod
    def is_supported() -> bool:
        """当前平台是否支持 AF_PACKET（Linux）。"""
        return hasattr(socket, "AF_PACKET")

    def start(self) -> None:
        """创建原始套接字并启动抓包线程。

        Raises:
            RuntimeError: 平台不支持
            PermissionError: 权限不足（需 root 或 CAP_NET_RAW）
            OSError: 网卡不存在等
        """
        if not self.is_supported():
            raise RuntimeError("当前平台不支持 AF_PACKET，请改用 bind 模式（UDPReceiver）")

        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(ETH_P_ALL))
        try:
            sock.settimeout(self._timeout)
            # 指定网卡时 bind 到该网卡；未指定时不 bind，让内核把所有网卡
            # 的帧都投递到该 socket（等价于 "any"）。
            # —— 不用 bind("", 0)：Jetson 等定制内核对空接口名报
            #    OSError(Electron19, "No such device")，桌面 Linux 却正常，
            #    行为不一致；不 bind 反而更稳，且与 libpcap 的默认行为一致。
            if self.interface:
                sock.bind((self.interface, 0))
        except Exception:
            sock.close()
            raise

        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._sniff_loop, name="udp-sniffer", daemon=True)
        self._thread.start()
        logger.info("UDP 旁路抓包已启动: port=%s interface=%s", self.port, self.interface or "全部网卡(不bind)")

    def stop(self) -> None:
        """停止抓包线程并关闭套接字。"""
        if not self._running:
            return
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()
            self._sock = None
        logger.info("UDP 旁路抓包已停止")

    def is_running(self) -> bool:
        """返回抓包线程是否在运行。"""
        return self._running

    # ------------------------------------------------------------------
    def _sniff_loop(self) -> None:
        """抓包主循环：收帧 → 解析 → 过滤 → 入队。"""
        sock = self._sock
        if sock is None:
            return

        while self._running:
            try:
                frame = sock.recv(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    logger.error("抓包异常: %s", exc)
                break

            self.captured_frames += 1
            try:
                result = parse_udp_from_ethernet(frame, self.port)
            except Exception:  # noqa: BLE001 - 单个异常帧不能终止抓包
                continue

            if result is None:
                continue

            self.matched_packets += 1
            try:
                if self._queue.full():
                    with contextlib.suppress(queue.Empty):
                        self._queue.get_nowait()
                    self.dropped_packets += 1
                self._queue.put_nowait(result)
            except queue.Full:  # pragma: no cover - 极端并发兜底
                self.dropped_packets += 1

        logger.debug("抓包线程退出")

    # ------------------------------------------------------------------
    def get_packet(self, timeout: float = 0.1) -> Optional[Frame]:
        """取出一帧匹配的 UDP 载荷；队列为空时返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def pending_count(self) -> int:
        """队列中待消费的帧数。"""
        return self._queue.qsize()

    def stats(self) -> dict:
        """返回抓包侧统计信息。"""
        return {
            "mode": "sniff",
            "host": self.interface or "0.0.0.0",
            "port": self.port,
            "running": self._running,
            "received_packets": self.matched_packets,
            "dropped_packets": self.dropped_packets,
            "pending": self.pending_count(),
            "captured_frames": self.captured_frames,
        }


def self_test() -> None:
    """用合成以太网帧验证解析逻辑（不需要 root，任何平台都能跑）。"""
    src_ip, dst_ip = "192.168.1.120", "192.168.1.103"
    payload = bytes(range(220))
    udp = struct.pack('!HHHH', 43893, 43897, 8 + len(payload), 0) + payload
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 1, 0, 64, IPPROTO_UDP, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    frame = b'\x02' * 6 + b'\x03' * 6 + struct.pack('!H', ETH_TYPE_IPV4) + ip + udp

    ok = parse_udp_from_ethernet(frame, 43897)
    assert ok is not None, "目标端口匹配时应解析成功"
    data, addr = ok
    assert data == payload, "载荷应完整还原"
    assert addr == (src_ip, 43893), f"来源地址错误: {addr}"
    assert parse_udp_from_ethernet(frame, 12345) is None, "端口不匹配时应返回 None"

    # VLAN 标签帧
    vlan_frame = (b'\x02' * 6 + b'\x03' * 6 + struct.pack('!H', ETH_TYPE_VLAN)
                  + struct.pack('!HH', 0, ETH_TYPE_IPV4) + ip + udp)
    assert parse_udp_from_ethernet(vlan_frame, 43897) is not None, "VLAN 帧应能解析"

    print("udp_sniffer 自检通过：普通帧 / VLAN 帧 / 端口过滤 均正确")


if __name__ == "__main__":
    self_test()
