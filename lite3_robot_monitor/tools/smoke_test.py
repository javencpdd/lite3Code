"""端到端冒烟测试脚本（临时校验用）。

启动流程：
1. 直接在同一进程内初始化 MonitorService（不占用端口也可用）；
2. 用 mock_sender 向同一 UDP 端口发包；
3. 通过 WebSocket / REST 校验链路是否打通。

运行：python tools/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TOOLS = ROOT / "tools"
for p in (str(BACKEND), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import uvicorn  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

import main as backend_main  # noqa: E402
import mock_sender as ms  # noqa: E402
from config import udp_config  # noqa: E402


def run_mock_sender(seconds: float = 6.0) -> None:
    """后台线程：向本机发送模拟数据。"""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = ("127.0.0.1", udp_config.port)
    start = time.time()
    seq = 0
    while time.time() - start < seconds:
        t = time.time() - start
        seq += 1
        sock.sendto(ms.build_robot_state(t, seq), target)
        sock.sendto(ms.build_joint_values(0x0902, t, seq, 0.6), target)
        sock.sendto(ms.build_joint_values(0x0903, t, seq, 1.2, 0.4), target)
        time.sleep(0.1)
    sock.close()


async def wait_backend(url: str, timeout: float = 15.0) -> None:
    """等待后端接口可用。"""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.2)
    raise RuntimeError("后端启动超时")


def get_json(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def main() -> int:
    """执行一次完整冒烟测试，返回退出码。"""
    http = "http://127.0.0.1:8000"

    # 1) 启动 uvicorn（独立线程运行 Server）
    config = uvicorn.Config(backend_main.app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    try:
        await wait_backend(http)

        # 2) 未发数据时的状态
        status = get_json(f"{http}/api/status")
        print("[1] 初始状态 connected =", status["connected"], "| udp_running =", status["udp_running"])
        assert status["udp_running"], "UDP 接收线程应处于运行中"
        assert status["connected"] is False, "未收到数据时不应判定为在线"

        # 3) 开始发送模拟数据
        sender = threading.Thread(target=run_mock_sender, args=(6.0,), daemon=True)
        sender.start()

        # 4) WebSocket 订阅校验
        ws_url = "ws://127.0.0.1:8000/ws/state"
        async with connect(ws_url) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            print("[2] WS 首帧 keys =", sorted(first.keys()))

            await asyncio.sleep(2.0)
            second = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            rs = second.get("robot_state") or {}
            print("[3] WS 实时帧: 状态=%s 步态=%s 电量=%s Roll=%.3f" % (
                rs.get("basic_state"), rs.get("gait_state"), rs.get("battery"),
                (rs.get("imu") or {}).get("roll", float("nan")),
            ))
            assert rs.get("basic_state") == "力控状态", "0x0901 基本状态解析异常"
            assert len(second["joint_angle"]["joint"]) == 12, "关节角度数量应为 12"
            assert len(second["joint_velocity"]["velocity"]) == 12, "关节角速度数量应为 12"
            assert second["connected"] is True, "收到数据后应判定为在线"

        # 5) REST 校验
        state = get_json(f"{http}/api/state")
        print("[4] REST /api/state -> robot_state.basic_state =",
              (state.get("robot_state") or {}).get("basic_state"))
        raw = get_json(f"{http}/api/raw?limit=5")
        print("[5] REST /api/raw -> 缓存条数 =", raw["count"], "| 最新 =",
              raw["items"][0]["code"] if raw["items"] else "-")

        # 6) 等待模拟结束，验证离线判定
        sender.join(timeout=10)
        await asyncio.sleep(4.0)
        status = get_json(f"{http}/api/status")
        print("[6] 停止发送 4 秒后 connected =", status["connected"],
              "| 已接收包数 =", status["packets_received"])
        assert status["connected"] is False, "停止数据后应自动判定为离线"

        print("\n全部冒烟测试通过 ✅")
        return 0
    finally:
        server.should_exit = True
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
