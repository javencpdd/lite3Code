#!/usr/bin/env python3
"""模拟 103 上 FAST-LIO(C16 分支) 的输出, 用于在不接真机的情况下验证 lio_relay 链路。

发布的正是 faster-lio 源码里的默认话题名与 frame:
  /Odometry                frame_id=camera_init, child_frame_id=body
  /cloud_registered_body   frame_id=body
"""
import struct

import rospy
from nav_msgs.msg import Odometry
from sensor_msgs.msg import PointCloud2, PointField

rospy.init_node("fake_lio", anonymous=False)

odom_pub = rospy.Publisher("/Odometry", Odometry, queue_size=10)
cloud_pub = rospy.Publisher("/cloud_registered_body", PointCloud2, queue_size=10)

rate = rospy.Rate(10)
t = 0.0

while not rospy.is_shutdown():
    t += 0.1

    o = Odometry()
    o.header.stamp = rospy.Time.now()
    o.header.frame_id = "camera_init"
    o.child_frame_id = "body"
    o.pose.pose.position.x = 0.1 * t
    o.pose.pose.position.z = 0.30
    o.pose.pose.orientation.w = 1.0
    odom_pub.publish(o)

    cloud = PointCloud2()
    cloud.header.stamp = rospy.Time.now()
    cloud.header.frame_id = "body"
    cloud.height = 1
    cloud.width = 4
    cloud.fields = [
        PointField("x", 0, PointField.FLOAT32, 1),
        PointField("y", 4, PointField.FLOAT32, 1),
        PointField("z", 8, PointField.FLOAT32, 1),
    ]
    cloud.is_bigendian = False
    cloud.point_step = 12
    cloud.row_step = 12 * 4
    cloud.is_dense = True
    cloud.data = b"".join(
        struct.pack("fff", *p)
        for p in [(1.0, 0.0, 0.0), (-1.0, 0.5, 0.1), (0.0, 2.0, -0.2), (0.5, -1.5, 0.3)]
    )
    cloud_pub.publish(cloud)

    rate.sleep()
