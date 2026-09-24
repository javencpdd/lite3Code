import pyrealsense2 as rs

pipeline = rs.pipeline()
config = rs.config()

config.enable_stream(
    rs.stream.depth,
    640,
    480,
    rs.format.z16,
    30
)

config.enable_stream(
    rs.stream.color,
    640,
    480,
    rs.format.bgr8,
    30
)

pipeline.start(config)

try:
    for _ in range(30):
        frames = pipeline.wait_for_frames()

    # 对齐 Depth 到 RGB
    align = rs.align(rs.stream.color)
    frames = align.process(frames)

    depth = frames.get_depth_frame()
    color = frames.get_color_frame()

    if not depth or not color:
        raise RuntimeError("Missing depth or color frame")

    pc = rs.pointcloud()

    pc.map_to(color)
    points = pc.calculate(depth)

    vertices = points.get_vertices()

    print("point count:", len(vertices))

    ply = rs.save_to_ply("scene.ply")

    ply.set_option(
        rs.save_to_ply.option_ply_binary,
        True
    )

    ply.set_option(
        rs.save_to_ply.option_ply_normals,
        False
    )

    ply.process(frames)

    print("saved: scene.ply")

finally:
    pipeline.stop()
