"""Lite3 Robot Monitor 后端服务入口。

提供：
    GET  /api/state     当前机器人状态快照
    GET  /api/status    服务与链路运行状态
    GET  /api/raw       最近若干条原始 UDP 报文
    WS   /ws/state      10Hz 实时推送机器人状态

设计约束：
1. UDP 接收在独立线程；解析在 asyncio 消费任务中完成，两者都不会阻塞事件循环；
2. 每个 WebSocket 连接独立心跳；断连自动清理；
3. 所有对外数据经由 models.py 中的 Pydantic 模型序列化。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Set

# 保证既支持 `uvicorn main:app`，也支持从其他工作目录脚本方式启动
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import control_protocol  # noqa: E402
from config import (  # noqa: E402
    control_config,
    data_source_config,
    service_config,
    udp_config,
)
from control_service import ControlError, ControlService  # noqa: E402
from models import (  # noqa: E402
    ControlStatus,
    CustomCommandRequest,
    MonitorState,
    PresetCommandRequest,
    RawHexRequest,
    RawPacketResponse,
    ServiceStatus,
    VelocityCommand,
)
from parser import parse_packet  # noqa: E402
from state_manager import StateManager  # noqa: E402
from data_source import build_data_source, DataSource  # noqa: E402
from udp_sniffer import UDPSniffer  # noqa: E402  (仅用于看门狗判断是否支持 sniff 回退)

# ----------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------
# uvicorn 会重置 root logger 的 handler，导致 app 层日志被吞掉
# （表现为 journalctl 里只有 access log，看不到 sniff/解析/错误日志）。
# 这里显式给 app logger 挂一个 StreamHandler，确保输出到 stderr → journal。
_log_handler = logging.StreamHandler(sys.stderr)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
))
logger = logging.getLogger("lite3.monitor")
logger.setLevel(os.getenv("LITE3_LOG_LEVEL", "INFO").upper())
logger.addHandler(_log_handler)
# 防止日志向 root 重复传播（uvicorn 的 handler 已在 root 上）
logger.propagate = False

# 探测 ros 桥接端口时，等待一个「新」数据报的窗口（秒）。
# ros_bridge_node 以 10Hz 发送，2 个周期足够判定，且不会拖慢主循环。
_ROS_PROBE_WINDOW = 1.0


class WebSocketManager:
    """WebSocket 连接池：负责注册、注销与广播。"""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """接受连接并加入广播列表。"""
        await websocket.accept()
        self._clients.add(websocket)
        logger.info("WebSocket 客户端接入，当前连接数 %d", len(self._clients))

    def disconnect(self, websocket: WebSocket) -> None:
        """移除连接（幂等）。"""
        if websocket in self._clients:
            self._clients.discard(websocket)
            logger.info("WebSocket 客户端断开，当前连接数 %d", len(self._clients))

    @property
    def count(self) -> int:
        """当前在线客户端数。"""
        return len(self._clients)

    async def broadcast(self, message: str) -> None:
        """向所有在线客户端广播文本消息，异常连接自动清理。"""
        dead: List[WebSocket] = []
        for client in list(self._clients):
            try:
                await client.send_text(message)
            except Exception as exc:  # noqa: BLE001 - 连接可能随时断开
                logger.debug("广播失败，移除客户端: %s", exc)
                dead.append(client)
        for client in dead:
            self.disconnect(client)


class MonitorService:
    """组合数据源、状态管理器与 WebSocket 广播的核心服务。

    数据源（DataSource）可以是 sniff / bind / ros 三种之一，
    三者接口一致，可经 /api/source 接口在运行时切换；默认（auto）优先
    用 ros 话题订阅，收不到数据时自动回退 sniff（见 _consume_loop 看门狗）。
    """

    def __init__(self) -> None:
        # 当前生效的数据源（DataSource 实例）
        self.data_source: Optional[DataSource] = None
        # 当前实际生效的接收模式：ros / sniff / bind
        self.data_source_mode: str = "none"
        # 状态仓库（线程安全）
        self.state = StateManager()
        # WebSocket 连接池
        self.ws = WebSocketManager()
        # 消费任务与广播任务句柄（Optional 写法以兼容 Python 3.8）
        self._consumer_task: Optional[asyncio.Task] = None
        self._broadcast_task: Optional[asyncio.Task] = None
        # 数据源启动时间戳（用于看门狗判定「多久没收到数据」）
        self._source_started_at: float = 0.0
        # 当前数据源累计收到的帧数
        self._frames_received: int = 0
        # 最近一次真正收到帧的时间（monotonic）。
        # 只看 _frames_received 无法识别「先正常、后断流」的场景。
        self._last_frame_at: float = 0.0
        # 是否正处于「因 ros 无数据而回退 sniff」的状态。
        # 注意这是双向的：ros 恢复后会重新置 False，允许下次再降级。
        self._auto_fell_back: bool = False
        # 上一次探测 ros 是否恢复的时间（monotonic）
        self._last_ros_probe_at: float = 0.0
        # UDP 是否已就绪
        self.udp_ready: bool = False

    # ------------------------------------------------------------------
    def _build_data_source(self) -> DataSource:
        """按配置构造数据源。

        auto / ros -> 本地桥接（ros_bridge_node 转发来的 ROS topic）；
        sniff / bind -> 直连 43897。
        """
        return build_data_source(data_source_config.mode)

    def _switch_source(self, mode: str) -> None:
        """停止当前数据源、构造并启动新的数据源。

        看门狗与 /api/source/set 共用此方法。任何一步失败都不会让服务崩溃：
        最坏情况回退到 sniff（若新源也起不来，则 data_source 置空，页面显示离线）。
        """
        logger.info(
            "切换数据源: %s -> %s",
            self.data_source_mode,
            mode,
        )
        old = self.data_source
        try:
            if old is not None:
                old.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止旧数据源失败: %s", exc)

        try:
            new = build_data_source(mode)
            new.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("启动 %s 数据源失败: %s", mode, exc)
            if mode != "sniff":
                try:
                    new = build_data_source("sniff")
                    new.start()
                    mode = "sniff"
                except Exception as exc2:  # noqa: BLE001
                    logger.error("sniff 兜底也失败: %s", exc2)
                    self.data_source = None
                    self.data_source_mode = "none"
                    return
            else:
                self.data_source = None
                self.data_source_mode = "none"
                return

        self.data_source = new
        self.data_source_mode = new.mode
        self._source_started_at = time.monotonic()
        self._frames_received = 0
        self._last_frame_at = 0.0
        logger.info("数据源已切换为: %s", new.mode)

    async def set_mode(self, mode: str) -> Dict[str, Any]:
        """运行时切换数据源模式（供 /api/source/set 调用）。"""
        allowed = {"ros", "ros_direct", "sniff", "bind", "auto"}
        if mode not in allowed:
            return {"ok": False, "detail": f"不支持的模式: {mode}，可选 {sorted(allowed)}"}
        self._auto_fell_back = False  # 手动切换重置回退标记
        self._switch_source(mode)
        return {"ok": True, "mode": self.data_source_mode}

    async def start(self) -> None:
        """启动数据源、消费任务与广播任务。"""
        try:
            self.data_source = self._build_data_source()
            self.data_source.start()
            self.data_source_mode = self.data_source.mode
            self._source_started_at = time.monotonic()
            self.udp_ready = True
            logger.info("数据源已启动: %s", self.data_source_mode)
        except OSError:
            logger.error(
                "数据源启动失败，服务继续启动以便查看状态页，请检查配置后重启",
            )
            self.udp_ready = False

        self._consumer_task = asyncio.create_task(self._consume_loop(), name="data-consumer")
        self._broadcast_task = asyncio.create_task(self._broadcast_loop(), name="ws-broadcast")

    async def stop(self) -> None:
        """停止所有后台任务与数据源。"""
        for task in (self._consumer_task, self._broadcast_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._consumer_task = None
        self._broadcast_task = None
        if self.data_source is not None:
            self.data_source.stop()

    # ------------------------------------------------------------------
    async def _consume_loop(self) -> None:
        """消费数据源队列：取帧 -> 解析/直填 -> 更新状态仓库。

        队列读取是阻塞调用，放入线程池执行，避免阻塞 asyncio 事件循环。
        ros 来源产出 parsed 帧（已结构化），sniff/bind 产出 raw 帧（需 parse_packet）。
        """
        loop = asyncio.get_running_loop()

        while True:
            if self.data_source is None:
                await asyncio.sleep(1.0)
                continue

            # 数据源看门狗：ros 失效则降级 sniff，ros 恢复则切回 ros。
            # 内部按时间间隔节流，绝大多数迭代会立即返回；
            # 注意放在取帧之前——这样才能在 ros 彻底静默（不返回帧）时也得到执行。
            await self._source_watchdog(loop)

            get_frame = functools.partial(self.data_source.get_frame, 0.1)
            try:
                frame = await loop.run_in_executor(None, get_frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 消费线程不应因异常退出
                logger.error("数据源读取异常: %s", exc)
                await asyncio.sleep(0.2)
                continue

            if frame is None:
                continue

            self._frames_received += 1
            self._last_frame_at = time.monotonic()
            try:
                if frame["kind"] == "raw":
                    data, addr = frame["data"], frame["addr"]
                    payload: Dict[str, Any] = parse_packet(data, addr)
                    self.state.update(payload, raw=data)
                else:  # parsed（ros 来源）
                    payload = frame["payload"]
                    self.state.update(payload, raw=None)
            except Exception as exc:  # noqa: BLE001 - 单包异常不能终止整个服务
                logger.exception("处理数据帧失败: %s", exc)

    def _maybe_fallback_to_sniff(self) -> None:
        """看门狗（降级方向）：ros 收不到数据时回退 sniff。

        必须覆盖两种场景，缺一不可：
          1) ros 从未产出数据——桥接没起来，或 topic 名与 transfer_ros2 实际发布的不一致，
             用较短的 link_timeout 快速判定；
          2) ros 正常一段时间后断流——transfer_ros2 重启、ROS master 抖动等。
             此场景下 _frames_received != 0，旧实现会直接跳过，导致页面停在陈旧数据上，
             这里改用「距最后一帧的时间」判定。
        """
        if data_source_config.mode != "auto":
            return
        # ros（桥接）与 ros_direct（内嵌订阅）都要纳入降级判定
        if self.data_source_mode not in ("ros", "ros_direct"):
            return
        if not UDPSniffer.is_supported():
            return

        now = time.monotonic()
        if self._frames_received == 0:
            # 场景 1：自切换以来一帧未收到
            waited = now - self._source_started_at
            if waited < service_config.link_timeout:
                return
            reason = "ros 在 %.1fs 内未产出任何数据" % service_config.link_timeout
        else:
            # 场景 2：曾经正常，之后静默
            silent = now - self._last_frame_at
            if silent < data_source_config.ros_stale_timeout:
                return
            reason = "ros 数据流已中断 %.1fs" % silent

        logger.warning("auto 模式：%s，自动回退 sniff", reason)
        self._auto_fell_back = True
        self._switch_source("sniff")

    def _probe_ros_available(self) -> bool:
        """探测 ros 桥接是否已恢复：临时绑定 ros 桥接端口，看短窗内能否收到数据报。

        可行性依据（ros_bridge_node.py）：桥接节点只在真正收到 ROS topic 时才发送
        （_flush_state 由 _dirty 门控，topic 一停就静默），所以「端口上有包」
        等价于「ROS topic 当前确实是活的」，不会把陈旧缓存误判为已恢复。

        为什么不直接切回 ros 去试：那必须先停掉正在正常工作的 sniff，
        探测期间必然丢数据。而 sniff 走 AF_PACKET 不占用端口，
        与临时 bind 该端口互不冲突，两者可以并行。
        """
        if not data_source_config.ros_bridge_host:
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as exc:
            logger.debug("创建探测 socket 失败: %s", exc)
            return False

        try:
            sock.settimeout(_ROS_PROBE_WINDOW)
            sock.bind((data_source_config.ros_bridge_host, data_source_config.ros_bridge_port))
            try:
                sock.recvfrom(65535)
                return True
            except socket.timeout:
                return False
        except OSError as exc:
            # 绑不上（例如端口被残留进程占用）时不算恢复，下一周期再试
            logger.debug("探测 ros 端口失败: %s", exc)
            return False
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    async def _maybe_recover_to_ros(self, loop) -> None:
        """看门狗（恢复方向）：sniff 兜底期间周期性探测 ros，恢复后优先切回 ros。

        这是「ros 为主」的关键——若只支持单向降级，ros 恢复后仍会一直停在 sniff 上。
        """
        if data_source_config.mode != "auto":
            return
        if not self._auto_fell_back:
            return
        if self.data_source_mode != "sniff":
            return

        now = time.monotonic()
        if (now - self._last_ros_probe_at) < data_source_config.ros_retry_interval:
            return
        self._last_ros_probe_at = now

        # auto 下到底该切回哪种 ros 实现，由 LITE3_ROS_IMPL 决定
        target = "ros_direct" if data_source_config.ros_impl == "direct" else "ros"
        probe = (self._probe_ros_direct_available if target == "ros_direct"
                 else self._probe_ros_available)

        # 阻塞式探测放进线程池，避免卡住 asyncio 事件循环
        recovered = await loop.run_in_executor(None, probe)
        if not recovered:
            return

        logger.info("auto 模式：检测到 ros 已恢复，优先切回 %s 数据源", target)
        self._switch_source(target)
        # 置回 False：允许 ros 再次失效时继续降级，形成双向自愈而非一次性降级
        self._auto_fell_back = False

    def _probe_ros_direct_available(self) -> bool:
        """探测内嵌订阅是否已恢复：读常驻节点的「最后消息时间」。

        节点在降级到 sniff 期间**并未销毁**（见 ros_direct_source 的 stop），
        回调仍在实时更新该时间戳，所以这里读到的就是 ROS 图的真实活跃度——
        既不用停掉正在工作的 sniff，也不用重建节点再等首帧。
        """
        try:
            from ros_direct_source import peek_last_message_age  # 延迟导入
        except Exception as exc:  # noqa: BLE001
            logger.debug("ros_direct 探测不可用: %s", exc)
            return False

        age = peek_last_message_age()
        if age is None:
            return False
        # 只要在陈旧阈值内还有消息，就认为已恢复
        return age <= max(1.0, data_source_config.ros_stale_timeout)

    async def _source_watchdog(self, loop) -> None:
        """数据源双向看门狗入口，每轮消费循环调用一次。"""
        self._maybe_fallback_to_sniff()
        await self._maybe_recover_to_ros(loop)

    async def _broadcast_loop(self) -> None:
        """按固定频率向所有 WebSocket 客户端推送最新状态快照。"""
        interval = 1.0 / max(1, service_config.push_hz)
        while True:
            await asyncio.sleep(interval)
            if self.ws.count == 0:
                # 没有客户端时跳过序列化开销，仅更新连接数
                self.state.set_ws_clients(0)
                continue

            try:
                snapshot = self.state.snapshot()
                self.state.set_ws_clients(self.ws.count)
                message = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("状态序列化失败: %s", exc)
                continue

            await self.ws.broadcast(message)

    # ------------------------------------------------------------------
    def status(self) -> ServiceStatus:
        """返回服务运行状态。"""
        stats = self.data_source.stats() if self.data_source is not None else {}
        # 以实际生效的数据源模式为准（ros / sniff / bind）
        stats["mode"] = self.data_source_mode
        stats["data_source"] = self.data_source_mode
        return self.state.get_status(stats, service_config.push_hz)


service = MonitorService()

# 控制服务（写方向）：默认禁用，需通过 /api/control/enable 显式开启
control = ControlService(
    target_ip=control_config.target_ip,
    target_port=control_config.target_port,
    heartbeat_interval=control_config.heartbeat_interval,
    heartbeat_lease=control_config.heartbeat_lease,
    max_linear=control_config.max_linear,
    max_linear_y=control_config.max_linear_y,
    max_angular=control_config.max_angular,
    enabled_by_default=control_config.enabled_by_default,
)


def _ctrl_error(exc: Exception, status: int = 400):
    """控制接口的统一错误返回。"""
    detail = str(exc)
    logger.warning("控制指令失败: %s", detail)
    return JSONResponse(status_code=status, content={"ok": False, "detail": detail})


# ----------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201 - FastAPI 生命周期钩子
    """应用生命周期：启动时拉起监控服务，关闭时释放资源。"""
    logger.info(
        "Lite3 Robot Monitor 启动: UDP %s:%s, HTTP %s:%s",
        udp_config.host, udp_config.port, service_config.host, service_config.port,
    )
    await service.start()
    logger.info(
        "数据源模式: %s（ros=订阅 ROS topic / sniff=旁路抓包 / bind=绑定端口）",
        service.data_source_mode,
    )
    try:
        yield
    finally:
        # 先停控制：停下心跳、补发零速、释放 socket，避免退出后机器人仍在动作
        control.close()
        await service.stop()
        logger.info("Lite3 Robot Monitor 已停止")


app = FastAPI(
    title="Lite3 Robot Monitor",
    description="Lite3 四足机器人 UDP 状态实时监控后端",
    version="1.0.0",
    lifespan=lifespan,
)

# 开发环境允许 Vite 直连后端，生产环境也保留以便跨域部署
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# REST 接口
# ----------------------------------------------------------------------
@app.get("/api/state", response_model=MonitorState, tags=["state"])
async def get_state() -> MonitorState:
    """获取当前机器人状态快照（HTTP 轮询备用接口）。"""
    return service.state.get_state()


@app.get("/api/status", response_model=ServiceStatus, tags=["state"])
async def get_status() -> ServiceStatus:
    """获取服务与链路运行状态。"""
    return service.status()


@app.get("/api/raw", response_model=RawPacketResponse, tags=["debug"])
async def get_raw(limit: int = 20) -> RawPacketResponse:
    """获取最近 `limit` 条原始 UDP 报文的摘要信息。"""
    limit = max(1, min(limit, 200))
    return service.state.get_raw_response(limit)


# ----------------------------------------------------------------------
# 数据源模式切换（中控页面「sniff / ROS 话题」开关）
#
# 默认 auto：优先用 ros 订阅 ROS topic（经 ros_bridge_node 转发），
# 收不到数据时自动回退 sniff；也可由前端手动切到 ros / sniff / bind。
# 见 config.DataSourceConfig 与 data_source.py。
# ----------------------------------------------------------------------
def _ros_diag() -> Dict[str, Any]:
    """ROS 数据源自检，产出可直接渲染到页面的排查结论。

    目的是把"ROS 模式失败了但不知道为什么"变成一份逐项勾选的清单：
    模式配置 / 当前生效 / 桥接端口是否在收数据 / sniff 兜底是否可用。
    """
    now = time.monotonic()
    active = service.data_source_mode
    configured = data_source_config.mode
    frames = service._frames_received
    since = round(now - service._last_frame_at, 1) if service._last_frame_at else None

    checks = []

    checks.append({
        "item": "模式配置",
        "ok": configured in ("ros", "auto"),
        "detail": f"LITE3_DATA_SOURCE={configured}"
                  + ("" if configured in ("ros", "auto") else "（该配置不会走 ROS）"),
    })

    checks.append({
        "item": "当前生效数据源",
        "ok": active == "ros",
        "detail": f"当前 {active}"
                  + ("" if active == "ros"
                     else ("（auto 已因 ROS 无数据回退）" if service._auto_fell_back else "")),
    })

    if active == "ros":
        if frames == 0:
            detail = f"已等待 {round(now - service._source_started_at, 1)}s 仍未收到任何帧"
        else:
            detail = f"最近一帧 {since}s 前（阈值 {data_source_config.ros_stale_timeout}s）"
        ok = frames > 0 and (since is None or since < data_source_config.ros_stale_timeout)
        checks.append({
            "item": f"桥接端口 {data_source_config.ros_bridge_port} 收包",
            "ok": bool(ok),
            "detail": detail,
        })
    else:
        checks.append({
            "item": f"桥接端口 {data_source_config.ros_bridge_port} 收包",
            "ok": False,
            "detail": "当前不在 ros 模式，未监听桥接端口",
        })

    checks.append({
        "item": "sniff 兜底能力",
        "ok": UDPSniffer.is_supported(),
        "detail": "支持 AF_PACKET 旁路抓包" if UDPSniffer.is_supported()
                  else "不支持（缺 CAP_NET_RAW 或非 Linux），ROS 失败后无兜底",
    })

    # 结论与建议
    if active == "ros" and frames > 0 and (since is None or since < data_source_config.ros_stale_timeout):
        verdict, hint = "ok", "ROS 话题订阅正常收数。"
    elif configured not in ("ros", "auto"):
        verdict, hint = "warn", "配置为 %s，不会使用 ROS。请切到 auto 或 ros。" % configured
    elif active == "bind":
        verdict, hint = "fail", "已降级到 bind（直接绑定 43897），可能与 transfer_ros2 抢端口。"
    elif service._auto_fell_back or active == "sniff":
        verdict, hint = (
            "fail",
            "ROS 未取到数据，已回退 sniff。请确认：① lite3-ros-bridge 服务在运行；"
            "② 它订阅的 topic 名与 transfer_ros2 实际发布的一致（默认 /imu/data、/leg_odom2、/joint_states）；"
            "③ 桥接端口与本机 LITE3_ROS_BRIDGE_PORT 一致。",
        )
    else:
        verdict, hint = "wait", "ROS 模式已启用，正在等待首帧数据（超过 link_timeout 会自动回退）。"

    return {
        "verdict": verdict,
        "hint": hint,
        "checks": checks,
        "frames_received": frames,
        "since_last_frame": since,
        "ros_bridge_port": data_source_config.ros_bridge_port,
        "ros_stale_timeout": data_source_config.ros_stale_timeout,
        "ros_retry_interval": data_source_config.ros_retry_interval,
        "link_timeout": service_config.link_timeout,
    }


@app.get("/api/source", tags=["source"])
async def get_source() -> Dict[str, Any]:
    """返回当前数据与配置的数据源模式，并附带 ROS 自检结论。"""
    return {
        "mode": service.data_source_mode,
        "configured": data_source_config.mode,
        "ros_bridge": f"{data_source_config.ros_bridge_host}:{data_source_config.ros_bridge_port}",
        "auto_fell_back": service._auto_fell_back,
        "diag": _ros_diag(),
    }


@app.post("/api/source/set", tags=["source"])
async def set_source(mode: str) -> Dict[str, Any]:
    """运行时切换数据源模式：ros / sniff / bind / auto。

    中控页面的「监听模式」开关调用本接口即可；切换过程不停服务。
    """
    return await service.set_mode(mode)


@app.get("/api/health", tags=["ops"])
async def health() -> Dict[str, Any]:
    """轻量健康检查，供容器 / supervisor 使用。"""
    status = service.status()
    counters = service.state.counters()
    return {
        "ok": True,
        "udp_running": status.udp_running,
        "connected": status.connected,
        "uptime_seconds": counters["uptime"],
    }


@app.post("/api/raw/clear", tags=["debug"])
async def clear_raw() -> Dict[str, Any]:
    """清空原始报文缓存。"""
    service.state.clear_raw()
    return {"ok": True}


# ----------------------------------------------------------------------
# 控制通道接口（写方向）
#
# ⚠️ 安全说明：43893 直连运动主机上的闭源 jy_exe，
#    不经过 103 侧的 VOA 安全层，因此：
#      · 控制通道默认关闭，必须显式 enable；
#      · 速度上限在后端二次夹取；
#      · 心跳带租约，前端消失后自动停止并下发零速。
# ----------------------------------------------------------------------
@app.get("/api/control/status", response_model=ControlStatus, tags=["control"])
async def control_status() -> ControlStatus:
    """获取控制服务状态（是否启用、急停、心跳、发包计数）。"""
    return ControlStatus.model_validate(control.status())


@app.get("/api/control/presets", tags=["control"])
async def control_presets() -> Dict[str, Any]:
    """返回预置指令列表（指令码均来自厂商《运动主机通讯接口》文档）。

    每条包含 name / code / code_hex / value / type / group / note / danger，
    前端按 group 归类渲染，danger=True 的需二次确认。
    """
    commands = control_protocol.list_presets()
    groups: List[str] = []
    for item in commands:
        if item["group"] not in groups:
            groups.append(item["group"])
    return {
        "commands": commands,
        "groups": groups,
        "heartbeat_code": "0x%08X" % control_protocol.CMD_HEARTBEAT,
        "velocity_codes": {
            "x": "0x%04X" % control_protocol.CMD_VEL_X,
            "y": "0x%04X" % control_protocol.CMD_VEL_Y,
            "yaw": "0x%04X" % control_protocol.CMD_VEL_YAW,
        },
        "axis_timeout_ms": control_protocol.AXIS_TIMEOUT_MS,
    }


@app.post("/api/control/enable", tags=["control"])
async def control_enable() -> Any:
    """启用控制通道（默认关闭）。"""
    try:
        control.enable()
    except ControlError as exc:
        return _ctrl_error(exc, 409)
    return {"ok": True, "enabled": True, "target": "%s:%s" % (control.target_ip, control.target_port)}


@app.post("/api/control/disable", tags=["control"])
async def control_disable() -> Dict[str, Any]:
    """停用控制通道：停止心跳、下发零速、释放 socket。"""
    control.disable()
    return {"ok": True, "enabled": False}


@app.post("/api/control/velocity", tags=["control"])
async def control_velocity(cmd: VelocityCommand) -> Any:
    """下发速度指令（x 前进 / y 左移 / yaw 左转）。"""
    try:
        return control.send_velocity(cmd.x, cmd.y, cmd.yaw)
    except ControlError as exc:
        return _ctrl_error(exc, 409)


@app.post("/api/control/preset", tags=["control"])
async def control_preset(req: PresetCommandRequest) -> Any:
    """按名称下发预置指令（前进 / 后退 / 左转 / 站立 …）。"""
    try:
        return control.send_preset(req.name)
    except ControlError as exc:
        return _ctrl_error(exc, 400)


@app.post("/api/control/custom", tags=["control"])
async def control_custom(req: CustomCommandRequest) -> Any:
    """下发自定义指令报文（cmd_code / cmd_value / type / data）。"""
    try:
        return control.send_custom(req.cmd_code, req.cmd_value, req.type, req.data)
    except ControlError as exc:
        return _ctrl_error(exc, 400)


@app.post("/api/control/raw", tags=["control"])
async def control_raw(req: RawHexRequest) -> Any:
    """下发完全自定义的原始十六进制报文。"""
    try:
        return control.send_raw_hex(req.hex)
    except ValueError as exc:
        return _ctrl_error(exc, 400)
    except ControlError as exc:
        return _ctrl_error(exc, 409)


@app.post("/api/control/stop", tags=["control"])
async def control_stop() -> Any:
    """停止移动（下发零速，保持心跳与连接）。"""
    try:
        return control.stop_motion()
    except ControlError as exc:
        return _ctrl_error(exc, 409)


@app.post("/api/control/estop", tags=["control"])
async def control_estop() -> Dict[str, Any]:
    """急停：停心跳 + 连发零速 + 锁定后续指令。"""
    return control.emergency_stop()


@app.post("/api/control/estop/clear", tags=["control"])
async def control_estop_clear() -> Dict[str, Any]:
    """解除急停锁定。"""
    return control.clear_estop()


@app.post("/api/control/heartbeat/start", tags=["control"])
async def control_heartbeat_start() -> Any:
    """开启心跳（周期下发当前速度，带租约保护）。"""
    try:
        return control.start_heartbeat()
    except ControlError as exc:
        return _ctrl_error(exc, 409)


@app.post("/api/control/heartbeat/stop", tags=["control"])
async def control_heartbeat_stop() -> Dict[str, Any]:
    """停止心跳，并补发一次零速。"""
    return control.stop_heartbeat()


@app.post("/api/control/heartbeat/renew", tags=["control"])
async def control_heartbeat_renew() -> Dict[str, Any]:
    """续约心跳租约（心跳开启时由前端定期调用）。"""
    control.renew_lease()
    return {"ok": True}


@app.get("/api/control/audit", tags=["control"])
async def control_audit(limit: int = 50) -> Dict[str, Any]:
    """返回最近的指令下发审计记录。"""
    return {"items": control.audit(limit)}


# ----------------------------------------------------------------------
# WebSocket 接口
# ----------------------------------------------------------------------
@app.websocket("/ws/state")
async def websocket_state(websocket: WebSocket) -> None:
    """实时推送机器人状态。

    服务端以 10Hz 主动推送；客户端发送任意文本均可触发一次即时快照；
    连接断开自动清理。
    """
    await service.ws.connect(websocket)
    service.state.set_ws_clients(service.ws.count)
    try:
        # 建立连接后立即推送一次，避免页面空白等待
        snapshot = service.state.snapshot()
        await websocket.send_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))

        while True:
            # 接收客户端消息以感知断连；收到任意消息立即回推一帧
            await websocket.receive_text()
            snapshot = service.state.snapshot()
            await websocket.send_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    except WebSocketDisconnect:
        service.ws.disconnect(websocket)
    except Exception as exc:  # noqa: BLE001 - 未知异常同样需要清理连接
        logger.debug("WebSocket 异常: %s", exc)
        service.ws.disconnect(websocket)
    finally:
        service.ws.disconnect(websocket)
        service.state.set_ws_clients(service.ws.count)


# ----------------------------------------------------------------------
# 静态资源（可选）：若前端已构建，则由本服务直接托管
# ----------------------------------------------------------------------
def _mount_frontend() -> None:
    """把 frontend/dist 挂载到根路径，实现单端口部署。"""
    if not service_config.serve_frontend:
        return
    dist = os.path.abspath(os.path.join(_BACKEND_DIR, service_config.frontend_dist))
    index = os.path.join(dist, "index.html")
    if os.path.isfile(index):
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
        logger.info("已挂载前端静态资源: %s", dist)
    else:
        logger.info("未发现前端构建产物，跳过静态挂载（开发模式请使用 npm run dev）")


_mount_frontend()


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):  # noqa: ANN001, ANN201
    """统一异常处理，避免内部错误直接暴露堆栈。"""
    logger.exception("未处理异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误", "error": str(exc)})

def main() -> None:
    """命令行入口：python main.py。

    等价于 `uvicorn main:app --host ... --port ...`
    """
    import uvicorn

    uvicorn.run(
        "main:app",
        host=service_config.host,
        port=service_config.port,
        reload=os.getenv("LITE3_RELOAD", "false").lower() == "true",
        log_level=os.getenv("LITE3_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
