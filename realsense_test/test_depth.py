import pyrealsense2 as rs
import numpy as np
import cv2

pipeline = rs.pipeline()
config = rs.config()

config.enable_stream(
    rs.stream.depth,
    640,
    480,
    rs.format.z16,
    30
)

pipeline.start(config)

try:
    for _ in range(30):
        frames = pipeline.wait_for_frames()

    depth = frames.get_depth_frame()

    if not depth:
        raise RuntimeError("No depth frame received")

    depth_np = np.asanyarray(depth.get_data())

    print("Depth shape:", depth_np.shape)
    print("dtype:", depth_np.dtype)

    # 仅用于肉眼查看，不是实际深度值
    depth_vis = cv2.convertScaleAbs(depth_np, alpha=0.03)
    depth_vis = cv2.applyColorMap(
        depth_vis,
        cv2.COLORMAP_JET
    )

    cv2.imwrite("depth_test.png", depth_vis)

    center_distance = depth.get_distance(
        depth_np.shape[1] // 2,
        depth_np.shape[0] // 2
    )

    print("center distance:", center_distance, "m")
    print("saved: depth_test.png")

finally:
    pipeline.stop()
