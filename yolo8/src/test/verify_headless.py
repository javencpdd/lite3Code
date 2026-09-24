"""Headless 验证：在 Jetson(ARM) 上加载 TensorRT engine 并完成一次人体检测+跟踪。

用途：不依赖摄像头、不依赖 GUI、不依赖 ROS，单独验证 YOLO 链路是否可用。
运行：cd /home/test/yolo8/src/test && python3 verify_headless.py
注意：必须用 _arm.engine（Jetson），_amd.engine 是 x86 机器导出的，本机加载会失败。
"""
import os
import sys
import time

# 关键：本脚本位于 src/test/，而 ultralytics 包在 src/。
# Python 执行脚本时 sys.path[0] 是"脚本所在目录"(test)，不是 cwd，
# 因此必须把 src/ 显式加入搜索路径，否则 import ultralytics 失败。
# 原项目 test/yolov8.py 缺少这一步，换个目录运行就会报 ModuleNotFoundError。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
from ultralytics import YOLO

MODEL = "../../model/yolov8n_arm.engine"
IMG = "../ultralytics/assets/zidane.jpg"

print("cwd   :", os.getcwd())
print("model :", os.path.abspath(MODEL), "exists=", os.path.exists(MODEL))
print("image :", os.path.abspath(IMG), "exists=", os.path.exists(IMG))

t0 = time.time()
model = YOLO(MODEL)
print("load  : OK  %.2fs" % (time.time() - t0))

t1 = time.time()
results = model.track(IMG, persist=True, classes=[0], conf=0.5)
print("infer : OK  %.3fs" % (time.time() - t1))

r = results[0]
n = 0 if r.boxes is None else len(r.boxes)
print("persons:", n)

if n:
    for b in r.boxes:
        xyxy = [round(float(v), 1) for v in b.xyxy[0].tolist()]
        conf = float(b.conf[0])
        cls = int(b.cls[0])
        tid = None if b.id is None else int(b.id[0])
        print("  box xyxy=%s conf=%.3f cls=%d id=%s" % (xyxy, conf, cls, tid))

# 纯噪点帧：验证任意尺寸输入不会崩（不要求检出）
noise = np.random.randint(0, 255, (360, 640, 3), dtype=np.uint8)
r2 = model.track(noise, persist=True, classes=[0], conf=0.5)
print("noise frame infer: OK, results=%d" % len(r2))

print("RESULT:", "PASS" if n > 0 else "PASS(链路通，但该图未检出人)")
