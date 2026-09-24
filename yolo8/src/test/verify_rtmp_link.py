"""RTMP 编码链路自检。

默认用 fakesink：跑通 appsrc→nvv4l2h264enc→flvmux 但不对外推流（安全自检）。
需要真实推流时：
    YOLO8_RTMP_SINK=rtmpsink YOLO8_RTMP_URL=rtmp://... VERIFY_FPS=30 \
    python3 -u verify_rtmp_link.py

运行：cd /home/test/yolo8/src/test && python3 verify_rtmp_link.py
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 注意：RtmpPublisher 是命名空间包，`from RtmpPublisher import RtmpPublisher`
# 拿到的是**模块**而不是类，需按原项目风格 `模块.类` 调用。
from RtmpPublisher import RtmpPublisher

MODE = os.environ.get("YOLO8_RTMP_SINK", "fakesink")
N = int(os.environ.get("VERIFY_FRAMES", "60"))
FPS = float(os.environ.get("VERIFY_FPS", "0"))  # 0=尽快推；真实推流请设 30

pub = RtmpPublisher.RtmpPublisher(sink=MODE)
print("sink     :", MODE)
print("url      :", pub.url)
print("pipeline :", pub.pipeline_str)
print("frames   : %d, fps=%.0f" % (N, FPS))

interval = 1.0 / FPS if FPS > 0 else 0.0
t0 = time.time()
ok = True
bad = 0
for i in range(N):
    frame = np.random.randint(0, 255, (360, 640, 3), dtype=np.uint8)
    if not pub.Push(frame):
        ok = False
    if pub.CheckError():
        print("[ABORT] 总线报错，第 %d 帧中止" % i)
        break
    if interval > 0:
        time.sleep(interval)

print("pushed   : %d frames, last_ok=%s, elapsed=%.2fs" % (N, ok, time.time() - t0))
err = pub.CheckError()
if err:
    print("gst error:", err)
pub.Stop()
print("RESULT   : %s" % ("FAIL" if err else "PASS"))
