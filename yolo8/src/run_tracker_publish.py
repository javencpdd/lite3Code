"""带双通路发布的主入口：ROS2(CompressedImage → Foxglove) + RTMP(H.264 硬件编码)。

用法（务必 cd 到 src，模型相对路径 ../model 依赖此目录）：
  cd /home/test/yolo8/src
  python3 run_tracker_publish.py                      # 双路 + 本地窗口
  HEADLESS=1 python3 run_tracker_publish.py           # 无 GUI，纯后台推流（推荐服务化）
  YOLO8_ENABLE_RTMP=0 python3 run_tracker_publish.py  # 只发 ROS2
  YOLO8_ENABLE_ROS2_IMAGE=0 python3 run_tracker_publish.py   # 只推 RTMP

环境变量：
  HEADLESS=1                    不调用 cv2.imshow（无桌面环境必开）
  YOLO8_ENABLE_RTMP=0/1         RTMP 开关（默认 1）
  YOLO8_ENABLE_ROS2_IMAGE=0/1   ROS2 图像开关（默认 1）
  YOLO8_ROS2_EVERY=N            每 N 帧发一次 ROS2（默认 3，约 10fps）
  YOLO8_RTMP_URL=...            推流地址（必须是 live/lite3_yolo；
                                live/lite3 是 120 的原始转发流，绝不能占用）
  YOLO8_CONF=0.4                person 置信度阈值（默认 0.4，可按误检情况调高）
  YOLO8_STATUS_INTERVAL=N       每 N 帧打印一次状态（默认 100）

标注策略（2026-09-23 修订）：
  * 每帧必推：无检出也推原帧并叠加 "NO PERSON"，保证 RTMP 流连续不断帧，
    播放器不会停在旧画面/黑屏；
  * 检出时画纯正红框 (0,0,255) + "person conf" 标签（不再用 ultralytics plot()
    的调色板色，避免"看不出红框"的观感问题）。
"""

import os
import signal
import sys
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from GStreamerWrapper import GStreamerWrapper
from RobotController import RobotController
from RtmpPublisher import RtmpPublisher

RED = (0, 0, 255)     # BGR 红框
GREEN = (0, 128, 0)   # fps 字色
FONT = cv2.FONT_HERSHEY_SIMPLEX


def env_flag(name, default='1'):
    return os.environ.get(name, default) != '0'


break_flag = False


def set_break_flag(signum, frame):
    global break_flag
    break_flag = True


signal.signal(signal.SIGINT, set_break_flag)
signal.signal(signal.SIGTERM, set_break_flag)

headless = env_flag('HEADLESS', '0')
enable_rtmp = env_flag('YOLO8_ENABLE_RTMP', '1')
ros2_every = int(os.environ.get('YOLO8_ROS2_EVERY', '3'))
status_interval = int(os.environ.get('YOLO8_STATUS_INTERVAL', '100'))

# headless 保险补丁：RobotController.InputAndProcess() 内部会调 cv2.waitKey(1)，
# 无 X11/桌面时 OpenCV 会抛 "The function is not implemented ... GTK+"。
# 当前主循环已不走 InputAndProcess，此处防御性保留。
if headless:
    cv2.waitKey = lambda delay=1: -1


def draw_annotated(frame, results, fps_counter):
    """画红色 person 框；无检出时叠加 NO PERSON。任何情况都返回可推流的帧。"""
    fps_counter.Count()
    n = 0
    img = frame
    if results and len(results) > 0:
        r = results[0]
        if r.boxes is not None and len(r.boxes) > 0:
            img = frame.copy()
            for box in r.boxes:
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                conf = float(box.conf[0].item())
                cv2.rectangle(img, (x1, y1), (x2, y2), RED, 2)
                label = 'person %.2f' % conf
                (tw, th), _ = cv2.getTextSize(label, FONT, 0.55, 1)
                ty = y1 - 6 if y1 - 6 > th + 4 else y2 + th + 6
                cv2.rectangle(img, (x1, ty - th - 4), (x1 + tw + 6, ty + 2), RED, -1)
                cv2.putText(img, label, (x1 + 3, ty), FONT, 0.55, (255, 255, 255), 1)
                n += 1
    if n == 0:
        img = frame.copy()
        cv2.putText(img, 'NO PERSON', (10, 40), FONT, 0.9, RED, 2)
    cv2.putText(img, 'fps %.1f' % fps_counter.GetFps(), (10, 18),
                cv2.FONT_HERSHEY_PLAIN, 1.2, GREEN, 2)
    return img


gstreamer_wrapper = GStreamerWrapper.GStreamerWrapper()
robot_controller = RobotController.RobotController()
rtmp_publisher = RtmpPublisher.RtmpPublisher() if enable_rtmp else None

print('run_tracker_publish started: headless=%s rtmp=%s ros2_every=%s'
      % (headless, enable_rtmp, ros2_every))
if rtmp_publisher is not None:
    print('rtmp url: ' + rtmp_publisher.url)

count = 0
det_frames = 0
t0 = time.time()
while not break_flag:
    # 取帧（启动初期后台线程可能尚未写入首帧）
    try:
        bgr_frame = gstreamer_wrapper.GetFrame()
    except AttributeError:
        continue
    if bgr_frame is None:
        continue
    count += 1

    # 推理：只检 person，conf 由 YOLO8_CONF 控制
    results = robot_controller.yolo_wrapper.Track(bgr_frame)
    n_boxes = 0
    if results and len(results) > 0 and results[0].boxes is not None:
        n_boxes = len(results[0].boxes)
    final_frame = draw_annotated(bgr_frame, results, robot_controller.fps_counter)
    if n_boxes:
        det_frames += 1

    # RTMP：每帧必推（硬件编码开销低，无检出也推，保证流连续）
    if rtmp_publisher is not None:
        rtmp_publisher.Push(final_frame)

    # ROS2：JPEG 软编，降频发布
    if ros2_every > 0 and count % ros2_every == 0:
        transfer = getattr(robot_controller, 'ros2_transfer', None)
        if transfer is not None:
            transfer.SendImage(final_frame)

    if status_interval > 0 and count % status_interval == 0:
        fps = count / max(time.time() - t0, 1e-6)
        err = rtmp_publisher.CheckError() if rtmp_publisher is not None else None
        print('frames=%d det_frames=%d fps=%.2f rtmp_err=%s'
              % (count, det_frames, fps, err if err else 'none'))
        if err:
            print('[ABORT] RTMP 总线报错，停止推流')
            break

    if not headless:
        cv2.imshow('DR People Tracking', final_frame)
        if cv2.waitKey(1) & 0xFF == 27:
            break

print('stopping, frames=%d det_frames=%d elapsed=%.1fs'
      % (count, det_frames, time.time() - t0))
gstreamer_wrapper.StopThread()
if rtmp_publisher is not None:
    rtmp_publisher.Stop()
if not headless:
    try:
        cv2.destroyAllWindows()
    except cv2.error:
        pass
