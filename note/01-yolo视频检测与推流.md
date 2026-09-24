# 01 · YOLOv8 视频检测与 RTMP/ROS2 推流

**适用场景**：从 120 主机的 RTSP 拉流 → YOLOv8 检测标注 → 推到 SRS（RTMP）和/或发布为 ROS2 图像话题（Foxglove 消费）。

## 链路

```
rtsp://<HOST_120>:8554/test
  → nvv4l2decoder 硬解
  → YOLOv8 TensorRT (yolov8n_arm.engine)
  → 标注帧 640×360
  ├─ RTMP  rtmp://<SRS_HOST>:1936/live/lite3_yolo
  └─ ROS2  /yolo/image_annotated  ─ rosbridge(9090) ─ Foxglove
```

## 操作步骤

### 1. 导出 TensorRT 引擎（换主机或换模型必做）

```bash
cd /home/test/yolo8/model
bash export_engine.sh          # 等价于 yolo export model=yolov8n.pt format=engine half=True simplify=True
```

> x86 上导出的 `yolov8n_amd.engine` 在 ARM 上**不可用**，必须用 ARM 版 `yolov8n_arm.engine`。

### 2. 配置（唯一配置文件）

```bash
vi /home/test/yolo8/deploy/yolo-publish.env
```

| 配置项 | 默认 | 说明 |
|---|---|---|
| `YOLO8_PUBLISH_MODE` | `rtmp` | `rtmp` 单路 / `dual` 双路 / `ros2` 仅话题 |
| `YOLO8_RTMP_URL` | `rtmp://<SRS_HOST>:1936/live/lite3_yolo` | 推流地址 |
| `YOLO8_CONF` | `0.4` | person 置信度阈值 |
| `YOLO8_IMAGE_TOPIC` | `/yolo/image_annotated` | ROS2 话题名 |
| `YOLO8_IMAGE_QOS` | `reliable` | QoS |
| `ROSBRIDGE_PORT` | `9090` | Foxglove 连接端口 |
| `HEADLESS` | `1` | 无桌面环境必开 |

### 3. 启动

```bash
cd /home/test/yolo8/deploy
./start.sh yolo                      # 手工启动（默认按 env 模式自动决定是否带 rosbridge）
./lite3-publish-ctl.sh status        # 查状态
./lite3-publish-ctl.sh restart all   # 改配置后重启生效
./stop.sh                            # 停止
```

### 4. 开机自启（可选）

```bash
sudo ./enable-resident.sh            # 安装并 enable systemd
sudo ./disable-resident.sh           # 取消
```

## 关键命令与配置文件路径

| 类型 | 路径 |
|---|---|
| 唯一配置 | `/home/test/yolo8/deploy/yolo-publish.env` |
| 启停总控 | `/home/test/yolo8/deploy/lite3-publish-ctl.sh`（`start\|stop\|restart\|status\|mode\|logs\|install\|enable\|disable\|uninstall`） |
| 运行脚本 | `/home/test/yolo8/deploy/scripts/{run-yolo-publish.sh, run-rosbridge.sh}` |
| systemd 单元 | `/home/test/yolo8/deploy/systemd/{lite3-yolo-publish,lite3-rosbridge,lite3-yolo-rosbridge}.service` |
| 已装位置 | `/etc/systemd/system/lite3-yolo-publish.service`、`lite3-yolo-rosbridge.service` |
| 引擎 | `/home/test/yolo8/model/yolov8n_arm.engine` |
| 日志 | `/home/test/yolo8/logs/{yolo-publish,rosbridge}.log` |
| 主程序 | `/home/test/yolo8/src/run_tracker_publish.py` |

## 验证方法

```bash
./lite3-publish-ctl.sh status                                  # 期望 running、rtmp_err=none
tail -f /home/test/yolo8/logs/yolo-publish.log                 # 每 100 帧一行 frames=/fps=/rtmp_err=
curl -sL http://<SRS_HOST>:1985/api/v1/streams/             # SRS 流列表（必须 -L），看 lite3_yolo 是否 active
```

双路模式额外验证：

```bash
source /opt/ros/foxy/setup.bash
python3 -u /opt/ros/foxy/bin/ros2 topic hz /yolo/image_annotated    # ros2 CLI 必须加 python3 -u，否则输出被缓冲
```

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动 20 s 后判失败 | 冷启动约 35 s（TRT 加载 + RTSP 首帧） | 至少等 40 s 再看 |
| `no property "live"` | Python 里 `rtmpsink location=URL live=1` 缺真引号 | 必须写成 `location="URL live=1"` |
| `nvv4l2h264enc` 报格式错误 | 硬编只收 NVMM 显存 | 前面加 `! nvvidconv ! video/x-raw(memory:NVMM),format=NV12` |
| 推流一直连不上但脚本无报错 | RTMP 连通失败不体现在 push-buffer 返回值 | 挂 GStreamer 总线监听；查 `rtmp_err` 字段 |
| `ros2 topic hz` 收不到 | Foxy 的 hz 固定 RELIABLE，BEST_EFFORT 发布端收不到 | 改用 `ros2 topic echo` 或 Foxglove 看 |
| headless 报 GTK 异常 | `cv2.waitKey` 在无桌面环境抛错 | `HEADLESS=1` 会 patch 掉 |
| 主机卡死、ssh banner 超时 | 并发加载两个 TRT 引擎打爆 8 GB 共享内存 | 验证推理必须与推流进程串行（先 `./stop.sh`） |

## 回滚方式

```bash
cd /home/test/yolo8/deploy
./stop.sh
sudo ./disable-resident.sh                              # 若已 enable
cp yolo-publish.env.bak-20260923 yolo-publish.env       # 配置回滚（已存在备份）
./start.sh yolo
```

配置改动前先备份：`cp yolo-publish.env yolo-publish.env.bak-$(date +%Y%m%d)`。
