"""ROS2 图像发布自检（L2）：本进程内同时发布与订阅，单终端即可闭环验证。

用法：
    cd /home/test/yolo8/src/test
    source /opt/ros/foxy/setup.bash
    VERIFY_SECONDS=15 python3 -u verify_ros2_publish.py

另开终端交叉验证 CLI 兼容性（改默认 QoS 后应能出速率）：
    source /opt/ros/foxy/setup.bash
    ros2 topic hz /yolo/image_annotated
"""
import os
import sys
import threading
import time

import numpy as np
import rclpy
from sensor_msgs.msg import CompressedImage

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 命名空间包下 `ROS2Transfer` 是模块名，类需 `模块.类` 调用
from RobotController.ROSTransfer import ROS2Transfer

SECONDS = float(os.environ.get("VERIFY_SECONDS", "15"))
EVERY = float(os.environ.get("VERIFY_EVERY", "0.1"))
TOPIC = os.environ.get("YOLO8_IMAGE_TOPIC", "/yolo/image_annotated")

transfer = ROS2Transfer.ROS2Transfer()

# ---- 订阅端：与发布端同 QoS，确认消息真的落到 subscriber ----
recv = {"n": 0, "bytes": 0, "t0": None, "t1": None}


def _cb(msg):
    recv["n"] += 1
    recv["bytes"] += len(msg.data)
    now = time.time()
    if recv["t0"] is None:
        recv["t0"] = now
    recv["t1"] = now


sub_qos = transfer.image_qos if transfer.image_qos is not None else 10
transfer.create_subscription(CompressedImage, TOPIC, _cb, sub_qos)

stop = threading.Event()


def _spin():
    while not stop.is_set():
        rclpy.spin_once(transfer, timeout_sec=0.05)


th = threading.Thread(target=_spin, daemon=True)
th.start()

print("topic    :", TOPIC)
print("publishing for %.0fs ..." % SECONDS)

t_end = time.time() + SECONDS
sent = 0
while time.time() < t_end:
    frame = np.random.randint(0, 255, (360, 640, 3), dtype=np.uint8)
    if transfer.SendImage(frame):
        sent += 1
    time.sleep(EVERY)

# 留一点时间收尾
time.sleep(0.5)
stop.set()
th.join(timeout=2)

n = recv["n"]
dt = (recv["t1"] - recv["t0"]) if (recv["t1"] and recv["t0"]) else 0.0
hz = (n - 1) / dt if (dt > 0 and n > 1) else 0.0
avg_kb = recv["bytes"] / n / 1024.0 if n else 0.0

print("sent     : %d messages" % sent)
print("received : %d messages" % n)
print("rate     : %.2f Hz" % hz)
print("avg size : %.1f KB/frame" % avg_kb)
print("RESULT   : %s" % ("PASS" if n > 0 else "FAIL"))

transfer.destroy_node()
rclpy.shutdown()
