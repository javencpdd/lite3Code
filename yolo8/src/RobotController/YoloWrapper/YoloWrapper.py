import os

from ultralytics import YOLO
from . import CocoTypeId

class YoloWrapper:
    def __init__(self):
        self.model = YOLO('../model/yolov8n_arm.engine')
        # 置信度阈值可调：部署层 deploy/yolo-publish.env 里配 YOLO8_CONF
        # （默认 0.5；0.4 对远距离/小目标更友好，误检多可调回）
        self.conf = float(os.environ.get('YOLO8_CONF', '0.5'))

    def Track(self, img):
        return self.model.track(img, persist=True, classes=[CocoTypeId.kPerson], conf=self.conf)
