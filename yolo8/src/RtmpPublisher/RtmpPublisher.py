"""GStreamer RTMP 推流封装（Jetson 硬件 H.264 编码）。

链路：appsrc(BGR) -> videoconvert -> I420 -> nvvidconv -> NVMM/NV12
      -> nvv4l2h264enc(硬件) -> h264parse -> flvmux -> rtmpsink
硬件编码不占 CPU，可 30fps 全帧率推流。

环境变量：
  YOLO8_RTMP_URL=rtmp://host:port/live/key   推流地址（默认 172.31.68.227:1936/live/lite3_yolo）
  YOLO8_RTMP_SINK=fakesink                   本地自检用，不对外推流
  YOLO8_RTMP_BITRATE=1000000                 码率
"""

import os

import cv2
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst


class RtmpPublisher:
    def __init__(self, url=None, width=640, height=360, fps=30,
                 bitrate=None, sink=None):
        Gst.init(None)

        self.url = url or os.environ.get(
            'YOLO8_RTMP_URL', 'rtmp://172.31.68.227:1936/live/lite3_yolo')
        self.sink = sink or os.environ.get('YOLO8_RTMP_SINK', 'rtmpsink')
        self.bitrate = int(bitrate or os.environ.get('YOLO8_RTMP_BITRATE', '1000000'))
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.duration = int(Gst.SECOND / self.fps)
        self.n = 0

        if self.sink == 'rtmpsink':
            # 注意（Python 专属坑）：Gst.parse_launch() 按空格切分 token，
            # 写成 `location=URL live=1` 会被当成给元素设一个名为 live 的属性，
            # 报 gst_parse_error: no property "live" in element "rtmpsink0"。
            # shell 里 `rtmpsink location="$url live=1"` 能work是因为整个串是单个 argv，
            # Python 必须自己加转义引号把 live=1 并进 location 值里。
            # 用真·双引号字符（Python 单引号包住即可，不要写成 \\" —— 反斜杠转义
            # 不会被 gst_parse 当作引号，同样报 no property "live"）。
            tail = 'rtmpsink location="' + self.url + ' live=1"'
        else:
            tail = self.sink  # 允许 fakesink 等，用于本地自检

        # 注意（Jetson 关键）：nvv4l2h264enc 的 sink 只接受 video/x-raw(memory:NVMM)，
        # 必须先经 nvvidconv 把系统内存帧搬进 NVMM 显存，否则 link 失败：
        #   "nvv4l2h264enc0 can't handle caps video/x-raw, format=I420"
        self.pipeline_str = (
            'appsrc name=src is-live=true do-timestamp=false format=time '
            'caps=video/x-raw,format=BGR,width=' + str(self.width) +
            ',height=' + str(self.height) +
            ',framerate=' + str(self.fps) + '/1 '
            '! videoconvert ! video/x-raw,format=I420 '
            '! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 '
            '! nvv4l2h264enc bitrate=' + str(self.bitrate) + ' '
            '! h264parse ! flvmux streamable=true '
            '! ' + tail
        )

        self.pipeline = Gst.parse_launch(self.pipeline_str)
        if self.pipeline is None:
            raise RuntimeError('GStreamer pipeline 创建失败: ' + self.pipeline_str)

        self.src = self.pipeline.get_by_name('src')
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('GStreamer pipeline 启动失败: ' + self.pipeline_str)

        # 关键：rtmpsink 连不上服务器时，push-buffer 仍会返回 OK，
        # 错误只出现在总线消息里。不挂 bus 监听就会「看起来在推、实际没推」。
        self.last_error = None
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect('message::error', self._on_error)
        self.bus.connect('message::warning', self._on_warning)
        self.warnings = []

    def _on_error(self, bus, msg):
        err, debug = msg.parse_error()
        self.last_error = str(err) + (' | debug: ' + str(debug) if debug else '')
        print('[gst ERROR] ' + self.last_error)

    def _on_warning(self, bus, msg):
        warn, _ = msg.parse_warning()
        self.warnings.append(str(warn))

    def Drain(self):
        """非阻塞地取一次总线消息，返回是否有致命错误。"""
        while self.bus.have_pending():
            self.bus.pop()
        return self.last_error is not None

    def CheckError(self):
        return self.last_error

    def Push(self, frame):
        """推一帧。frame 可为 cv2.UMat（原项目返回）或 ndarray。"""
        np_frame = frame.get() if hasattr(frame, 'get') else frame
        if np_frame is None:
            return False

        if np_frame.shape[1] != self.width or np_frame.shape[0] != self.height:
            np_frame = cv2.resize(np_frame, (self.width, self.height))

        data = np_frame.tobytes()
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = self.n * self.duration
        buf.duration = self.duration
        self.n += 1

        ret = self.src.emit('push-buffer', buf)
        return ret == Gst.FlowReturn.OK

    def Stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
