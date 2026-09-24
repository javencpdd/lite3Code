# 04 · Foxglove 可视化与 rosbridge

**适用场景**：把 103 上的 ROS2 话题（如 `/yolo/image_annotated`）暴露成 WebSocket，用 Foxglove Studio 远程看图/看话题。

## 操作步骤

### 1. 切双路模式并拉起 rosbridge

```bash
cd /home/test/yolo8/deploy
cp yolo-publish.env yolo-publish.env.bak-$(date +%Y%m%d)
sed -i 's/^YOLO8_PUBLISH_MODE=.*/YOLO8_PUBLISH_MODE=dual/' yolo-publish.env
./lite3-publish-ctl.sh restart all
```

`start.sh` 检测到模式为 `dual`/`ros2` 时会自动连 rosbridge 一起起，不必单独拉。

### 2. 单独启停 rosbridge（需要时）

```bash
./lite3-publish-ctl.sh start rosbridge
./lite3-publish-ctl.sh stop rosbridge
./lite3-publish-ctl.sh status
```

### 3. Foxglove Studio 连接

打开 Foxglove Studio → Open connection → **Foxglove WebSocket** →
URL 填 `ws://<HOST_103>:9090` → 订阅 `/yolo/image_annotated`。

### 4. 本机 colcon 工作区（可选，ROS2 侧节点）

```bash
source /opt/ros/foxy/setup.bash
cd /home/test/foxglove/foxglove_ws
colcon build
source install/setup.sh
```

## 关键命令与配置文件路径

| 类型 | 路径 |
|---|---|
| 模式开关 | `/home/test/yolo8/deploy/yolo-publish.env` 的 `YOLO8_PUBLISH_MODE` |
| 端口配置 | 同文件 `ROSBRIDGE_PORT`（默认 9090） |
| 启动脚本 | `/home/test/yolo8/deploy/scripts/run-rosbridge.sh` |
| 单元模板 | `/home/test/yolo8/deploy/systemd/lite3-yolo-rosbridge.service` |
| 已装单元 | `/etc/systemd/system/lite3-yolo-rosbridge.service` |
| 日志 | `/home/test/yolo8/logs/rosbridge.log` |
| colcon 工作区 | `/home/test/foxglove/foxglove_ws`（`src/` `install/`） |

## 验证方法

```bash
./lite3-publish-ctl.sh status                       # rosbridge 期望 running
printf "\047\n" | sudo -S ss -tlnp | grep :9090     # 期望 LISTEN
tail -f /home/test/yolo8/logs/rosbridge.log

source /opt/ros/foxy/setup.bash
python3 -u /opt/ros/foxy/bin/ros2 topic list | grep yolo
python3 -u /opt/ros/foxy/bin/ros2 topic hz /yolo/image_annotated
```

103 没有 `websockets`/`websocket-client` 库，若要脚本化验证需手写最小 RFC6455 客户端（客户端帧必须掩码）。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| Foxglove 连不上 9090 | 单路模式下 rosbridge 没起 | 改 `YOLO8_PUBLISH_MODE=dual` 并 `restart all` |
| 能看到话题但没数据 | QoS 不匹配（发布端 BEST_EFFORT） | 把 `YOLO8_IMAGE_QOS` 设成 `reliable` |
| `ros2 topic hz` 收不到 | Foxy 的 hz 固定 RELIABLE 且无 QoS 参数 | 用 Foxglove 看，或改用 `ros2 topic echo` |
| ros2 CLI 输出为空 | 输出被缓冲，被 SIGTERM 杀掉时丢失 | 用 `python3 -u /opt/ros/foxy/bin/ros2 ...` |
| 端口被占 | 9090 被别的实例占用 | 改 `ROSBRIDGE_PORT` 后 restart |
| 图像帧率偏低 | `YOLO8_ROS2_EVERY=3`（每 3 帧发 1 帧） | 调成 1（会增加带宽） |

## 回滚方式

```bash
cd /home/test/yolo8/deploy
cp yolo-publish.env.bak-<日期> yolo-publish.env     # 或直接改回 rtmp
sed -i 's/^YOLO8_PUBLISH_MODE=.*/YOLO8_PUBLISH_MODE=rtmp/' yolo-publish.env
./lite3-publish-ctl.sh restart all
./lite3-publish-ctl.sh stop rosbridge               # 不需要时停掉
```
