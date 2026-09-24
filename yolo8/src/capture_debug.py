"""抓帧诊断：拉 RTSP 原始帧 + 多阈值推理，输出图片供人工核对。

用法（必须 cd 到 /home/test/yolo8/src，ultralytics 相对依赖）：
    cd /home/test/yolo8/src && python3 capture_debug.py
产物：/home/test/yolo8/logs/debug/*.jpg
"""
import os
import sys
import time

import cv2
import numpy as np
import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst

sys.path.insert(0, '/home/test/yolo8/src')

OUT = '/home/test/yolo8/logs/debug'
os.makedirs(OUT, exist_ok=True)

PIPE = ("rtspsrc location=rtsp://192.168.1.120:8554/test latency=200 ! "
        "rtph264depay ! h264parse config-interval=1 ! nvv4l2decoder ! "
        "video/x-raw(memory:NVMM) ! nvvidconv ! video/x-raw,format=BGRx ! "
        "videoscale ! video/x-raw,width=640,height=360 ! videoconvert ! "
        "video/x-raw,format=BGR ! appsink name=sink sync=false max-buffers=1 drop=1")

Gst.init(None)
pipeline = Gst.parse_launch(PIPE)
pipeline.set_state(Gst.State.PLAYING)
sink = pipeline.get_by_name('sink')

# 拉首帧（冷启动最多等 40s）
sample = None
t0 = time.time()
while time.time() - t0 < 40:
    sample = sink.emit('pull-sample')
    if sample:
        break
    time.sleep(0.2)

if not sample:
    print('[FATAL] 40s 内拉不到 RTSP 帧')
    sys.exit(2)

buf = sample.get_buffer()
ok, mi = buf.map(Gst.MapFlags.READ)
if not ok:
    print('[FATAL] buffer map 失败')
    sys.exit(3)
arr = np.frombuffer(mi.data, dtype=np.uint8).copy()
buf.unmap(mi)
st = sample.get_caps().get_structure(0)
w, h = st.get_value('width'), st.get_value('height')
frame = arr.reshape((h, w, 3))

print('[INFO] frame shape=%s dtype=%s mean=%.2f std=%.2f min=%d max=%d'
      % (frame.shape, frame.dtype, frame.mean(), frame.std(), frame.min(), frame.max()))
cv2.imwrite(OUT + '/raw.jpg', frame)
print('[OK] saved ' + OUT + '/raw.jpg')

# 多采几帧看画面是否在动
frames = [frame]
for i in range(3):
    s = sink.emit('pull-sample')
    if not s:
        break
    b = s.get_buffer()
    ok, m = b.map(Gst.MapFlags.READ)
    if ok:
        a = np.frombuffer(m.data, dtype=np.uint8).copy()
        b.unmap(m)
        frames.append(a.reshape((h, w, 3)))
    time.sleep(0.3)
for i, f in enumerate(frames[1:], start=1):
    diff = np.abs(f.astype(int) - frames[0].astype(int)).mean()
    print('[INFO] 帧间差异 frame0 vs frame%d: %.2f' % (i, diff))

pipeline.set_state(Gst.State.NULL)

# ---- 推理对比 ----
from ultralytics import YOLO

model = YOLO('/home/test/yolo8/model/yolov8n_arm.engine')
print('[INFO] 模型类别数=%d' % len(model.names))

def report(tag, res):
    try:
        r = res[0]
        names = r.names
        if r.boxes is None or len(r.boxes) == 0:
            print('[%s] 无检出' % tag)
            return
        items = []
        for b in r.boxes:
            cls = int(b.cls[0].item()) if b.cls is not None else -1
            conf = float(b.conf[0].item()) if b.conf is not None else -1
            items.append('%s=%.2f' % (names.get(cls, cls), conf))
        print('[%s] %d 个: %s' % (tag, len(r.boxes), ', '.join(items)))
        cv2.imwrite(OUT + '/' + tag + '.jpg', r.plot())
    except Exception as e:
        print('[%s] 异常: %r' % (tag, e))

report('cur_person_conf050', model.track(frame, persist=True, classes=[0], conf=0.5))
report('person_conf025', model(frame, classes=[0], conf=0.25))
report('all_conf025', model(frame, conf=0.25))
report('all_conf010', model(frame, conf=0.10))
print('[DONE]')
