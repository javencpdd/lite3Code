"""UDP 接收模块。

职责单一：只负责把 Lite3 发来的 UDP 报文收下来，放进线程安全队列。
本模块不做协议解析，也不访问任何 Web 相关对象。

接口：
    start()       启动后台接收线程
    stop()        停止线程并关闭 socket
    get_packet()  从队列取出一个 (data, addr) 数据帧
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
from typing import Optional, Tuple

from config import udp_config

# (bytes, (ip, port)) 类型别名
Frame = Tuple[bytes, Tuple[str, int]]

logger = logging.getLogger(__name__)


class UDPReceiver:
    """Lite3 状态数据 UDP 接收器（后台线程 + 线程安全队列）。"""

    def __init__(
        self,
        host: str = udp_config.host,
        port: int = udp_config.port,
        buffer_size: int = udp_config.buffer_size,
        timeout: float = udp_config.timeout,
        max_queue_size: int = udp_config.max_queue_size,
    ) -> None:
        # 本地监听地址与端口
        self.host: str = host
        self.port: int = port
        # 单次接收缓冲区大小
        self._buffer_size: int = buffer_size
        # socket 超时时长，保证 stop() 时线程能尽快退出
        self._timeout: float = timeout
        # 接收队列：后台线程写入，消费线程（FastAPI 事件循环）读取
        self._queue: "queue.Queue[Frame]" = queue.Queue(maxsize=max_queue_size)
        # 线程运行标志
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        # socket 在 start() 中创建，stop() 中关闭
        self._sock: Optional[socket.socket] = None
        # 统计信息，便于 /api/status 展示与排障
        self.received_packets: int = 0
        self.dropped_packets: int = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """创建 UDP socket 并启动后台接收线程。"""
        if self._running:
            logger.warning("UDPReceiver 已经在运行，忽略重复启动")
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 允许端口复用，避免程序重启时端口处于 TIME_WAIT 导致绑定失败
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((self.host, self.port))
        except OSError as exc:
            self._sock.close()
            self._sock = None
            logger.error("绑定 UDP 端口 %s:%s 失败: %s", self.host, self.port, exc)
            raise
        self._sock.settimeout(self._timeout)

        self._running = True
        self._thread = threading.Thread(
            target=self._receive_loop, name="udp-receiver", daemon=True
        )
        self._thread.start()
        logger.info("UDP 接收已启动: %s:%s", self.host, self.port)

    def stop(self) -> None:
        """停止接收线程并释放 socket 资源。"""
        if not self._running:
            return
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()
            self._sock = None
        logger.info("UDP 接收已停止")

    def is_running(self) -> bool:
        """返回接收线程是否处于运行状态。"""
        return self._running

    # ------------------------------------------------------------------
    # 后台线程主循环
    # ------------------------------------------------------------------
    def _receive_loop(self) -> None:
        """持续调用 recvfrom()，把数据帧写入队列。"""
        sock = self._sock
        if sock is None:
            return

        while self._running:
            try:
                data, addr = sock.recvfrom(self._buffer_size)
            except socket.timeout:
                # 超时属于正常现象，用于让线程有机会检查退出标志
                continue
            except OSError as exc:
                if self._running:
                    logger.error("UDP 接收异常: %s", exc)
                break

            self.received_packets += 1
            frame: Frame = (data, addr)
            try:
                # 队列满时直接丢弃最旧的一帧，保证前端永远拿到的是最新数据
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:  # pragma: no cover - 并发竞态兜底
                        pass
                    self.dropped_packets += 1
                self._queue.put_nowait(frame)
            except queue.Full:  # pragma: no cover - 极端并发兜底
                self.dropped_packets += 1

        logger.debug("UDP 接收线程退出")

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------
    def get_packet(self, timeout: float = 0.1) -> Optional[Frame]:
        """从队列取出一帧数据；队列为空时返回 None。

        该方法线程安全，可被消费侧（含 asyncio 线程池）调用。
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def pending_count(self) -> int:
        """返回队列中尚未消费的数据帧数量，用于健康检查。"""
        return self._queue.qsize()

    def stats(self) -> dict:
        """返回接收侧的统计信息。"""
        return {
            "host": self.host,
            "port": self.port,
            "running": self._running,
            "received_packets": self.received_packets,
            "dropped_packets": self.dropped_packets,
            "pending": self.pending_count(),
        }
