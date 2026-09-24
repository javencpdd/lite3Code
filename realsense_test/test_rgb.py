import pyrealsense2 as rs
import numpy as np
import cv2

pipeline = rs.pipeline()
config = rs.config()

config.enable_stream(
    rs.stream.color,
    640,
    480,
    rs.format.bgr8,
    30
)

pipeline.start(config)

try:
    # 丢弃前几帧，让曝光稳定
    for _ in range(30):
        frames = pipeline.wait_for_frames()

    color_frame = frames.get_color_frame()

    if not color_frame:
        raise RuntimeError("No RGB frame received")

    img = np.asanyarray(color_frame.get_data())

    print("RGB shape:", img.shape)
    print("dtype:", img.dtype)

    cv2.imwrite("rgb_test.jpg", img)

    print("saved: rgb_test.jpg")

finally:
    pipeline.stop()
