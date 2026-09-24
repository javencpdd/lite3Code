import os

import cv2
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data, QoSProfile, QoSHistoryPolicy
from geometry_msgs.msg import Twist
from sensor_msgs.msg import CompressedImage


def _reliability_name(reliability):
    """rclpy 里 ReliabilityPolicy: RELIABLE=1, BEST_EFFORT=2（不是 0/1，别想当然）。"""
    return 'best_effort' if int(reliability) == 2 else 'reliable'


def _build_image_qos():
    """构造图像话题 QoS。

    背景：Foxy 的 `ros2 topic hz` / `ros2 topic echo` 固定用 RELIABLE 订阅且无 QoS 参数，
    若发布端用 BEST_EFFORT（sensor_data），CLI 工具会一条都收不到（话题可见但 hz 为空）。
    Foxglove 走 rosbridge，rosbridge_library 会按发布端 QoS 自动适配，两种都可用。

    默认 reliable(depth=10)：开箱即被所有消费端（ros2 CLI / rqt / rosbridge / 自写节点）兼容。
    env YOLO8_IMAGE_QOS 可选：reliable(默认) | best_effort | sensor(sensor_data)
    """
    mode = os.environ.get('YOLO8_IMAGE_QOS', 'reliable').strip().lower()
    if mode in ('best_effort', 'besteffort', 'sensor', 'sensor_data', 'sensordata'):
        return qos_profile_sensor_data
    depth = int(os.environ.get('YOLO8_IMAGE_QOS_DEPTH', '10'))
    return QoSProfile(depth=depth, history=QoSHistoryPolicy.KEEP_LAST)


class ROS2Transfer(Node):
    """ROS2 传输：发布 /cmd_vel，并可选发布标注图像供 Foxglove 消费。

    新增（相对原项目）：
      - SendImage()：把标注帧以 JPEG(CompressedImage) 发布，供 Foxglove 经 rosbridge 订阅。
    开关（环境变量，均为可选）：
      YOLO8_ENABLE_ROS2_IMAGE=0   关闭图像发布（只发 cmd_vel）
      YOLO8_IMAGE_TOPIC=/xxx      改话题名
      YOLO8_JPEG_QUALITY=70       JPEG 质量
      YOLO8_FRAME_ID=camera       帧坐标系
      YOLO8_IMAGE_QOS=reliable    图像话题 QoS：reliable|best_effort|sensor
    """

    def __init__(self, image_topic=None, enable_image=None):
        rclpy.init()
        super().__init__('track_twist_publisher')
        self.cmd_vel_pub = self.create_publisher(Twist, 'cmd_vel', 1)

        if enable_image is None:
            enable_image = os.environ.get('YOLO8_ENABLE_ROS2_IMAGE', '1') != '0'
        self.enable_image = bool(enable_image)

        if self.enable_image:
            topic = image_topic or os.environ.get('YOLO8_IMAGE_TOPIC', '/yolo/image_annotated')
            self.image_qos = _build_image_qos()
            self.image_pub = self.create_publisher(
                CompressedImage, topic, self.image_qos)
            # 注意：Foxy 的 logger.info() 只接受单个字符串，不能用 %s 多参数格式化
            self.get_logger().info(
                'image publisher enabled on ' + str(topic)
                + ' qos=' + _reliability_name(self.image_qos.reliability))
        else:
            self.image_pub = None
            self.image_qos = None

        self.quality = int(os.environ.get('YOLO8_JPEG_QUALITY', '70'))
        self.frame_id = os.environ.get('YOLO8_FRAME_ID', 'camera')

    def SendCmdVel(self, linear_velocity, radian_velocity):
        twist_cmd = Twist()
        twist_cmd.linear.x = linear_velocity
        twist_cmd.angular.z = radian_velocity
        self.cmd_vel_pub.publish(twist_cmd)

    def SendImage(self, frame, quality=None):
        """发布标注帧。frame 可为 cv2.UMat（原项目返回值）或 ndarray。"""
        if not self.enable_image or self.image_pub is None:
            return False
        np_frame = frame.get() if hasattr(frame, 'get') else frame
        if np_frame is None:
            return False

        q = int(quality if quality is not None else self.quality)
        ok, buf = cv2.imencode('.jpg', np_frame, [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if not ok:
            return False

        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id
        msg.format = 'jpeg'
        msg.data = buf.tobytes()
        self.image_pub.publish(msg)
        return True
