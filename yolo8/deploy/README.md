# Lite3 YOLO 视频发布 —— 部署说明

> 适用主机：103（192.168.1.103，Jetson Xavier NX / ROS2 Foxy / Python 3.8）
> 代码位置：`/home/test/yolo8`，本目录（`deploy/`）只放部署相关文件。

## 0. 默认行为（开箱状态）

| 组件 | 默认状态 | 说明 |
| --- | --- | --- |
| YOLO 发布进程 | **停止** | 需要显式 `start` |
| 发布模式 | **rtmp（单路）** | 推 `rtmp://172.31.68.227:1936/live/lite3_yolo` |
| rosbridge | **停止** | 只有 `dual` / `ros2` 模式才需要 |
| systemd 常驻 | **未启用** | unit 默认不安装、不自启，装了也默认不启用 |

## 1. 目录结构

```
deploy/
├── README.md                      本文件
├── yolo-publish.env               唯一配置文件（模式/流名/话题/端口）
├── start.sh                       启动（手工模式，不开机自启）
├── stop.sh                        关闭
├── enable-resident.sh             开启常驻（装 unit + 开机自启 + 立即启动）
├── disable-resident.sh            关闭常驻（取消自启 + 立即停止）
├── lite3-publish-ctl.sh           控制入口：start/stop/restart/status/mode/logs/install/enable/disable/uninstall
├── scripts/
│   ├── run-yolo-publish.sh        发布本体（systemd 与手工共用同一份）
│   └── run-rosbridge.sh           rosbridge 本体
└── systemd/
    ├── lite3-yolo-publish.service
    └── lite3-yolo-rosbridge.service
```

**设计要点**：启动逻辑只在 `scripts/run-*.sh` 里写一份，`systemd` 的 `ExecStart` 和控制脚本的手工模式
都调用它 —— 避免"手工跑能通、systemd 跑不通"这种经典分裂。

## 2. 手工模式（最常用，不改系统）

四个独立脚本（内部都调 `lite3-publish-ctl.sh`，无重复逻辑）：

| 脚本 | 作用 | 默认目标 | 需要 sudo |
| --- | --- | --- | --- |
| `./start.sh [yolo\|rosbridge\|all]` | 启动进程（不开机自启） | `yolo`；若 `YOLO8_PUBLISH_MODE=dual\|ros2` 自动带上 rosbridge | 否 |
| `./stop.sh [all\|yolo\|rosbridge]` | 停止进程 | `all`（停干净，不留残留） | 否 |
| `sudo ./enable-resident.sh [yolo\|rosbridge\|all]` | 开启常驻 | 同 start.sh 的规则 | 是 |
| `sudo ./disable-resident.sh [all\|yolo\|rosbridge]` | 关闭常驻 | `all` | 是 |

```bash
cd /home/test/yolo8/deploy

./start.sh                                 # 起（默认单路 RTMP）
./stop.sh                                  # 停

./lite3-publish-ctl.sh status all          # 看状态（免 sudo）
./lite3-publish-ctl.sh logs yolo           # 看日志；跟尾加 -f
```

消费端：

- **RTMP**：VLC / ffplay 打开 `rtmp://172.31.68.227:1936/live/lite3_yolo`

## 3. 单路 / 双路切换

```bash
# 切到双路（RTMP + ROS2 图像话题）
./lite3-publish-ctl.sh mode dual
./lite3-publish-ctl.sh restart yolo
./lite3-publish-ctl.sh start rosbridge     # Foxglove 通路必需

# 切回单路
./lite3-publish-ctl.sh mode rtmp
./lite3-publish-ctl.sh restart yolo
./lite3-publish-ctl.sh stop rosbridge
```

三种模式：`rtmp`（默认，只推流）/ `dual`（两条都发）/ `ros2`（只发话题）。
模式只是一个"总开关"，实际由 `run-yolo-publish.sh` 推导出 `YOLO8_ENABLE_RTMP`、`YOLO8_ENABLE_ROS2_IMAGE`，
不会存在"模式与开关不一致"的隐性错误。

双路下 Foxglove 消费：

- Foxglove Studio → 打开连接 → `ws://192.168.1.103:9090` → 面板订阅 `/yolo/image_annotated`

## 4. systemd 常驻（默认关闭，按需开启）

### 4.1 开启步骤（一个命令）

```bash
cd /home/test/yolo8/deploy
sudo ./enable-resident.sh                  # 装 unit（幂等）+ 开机自启 + 立即启动
# 等价的手工两步：
#   sudo ./lite3-publish-ctl.sh install
#   sudo ./lite3-publish-ctl.sh enable all
```

### 4.2 关闭步骤（一个命令）

```bash
sudo ./disable-resident.sh                 # 停止 + 取消开机自启（unit 保留，随时可再开）
sudo ./lite3-publish-ctl.sh uninstall      # 彻底移除 unit（可选）
```

### 4.3 常驻期间的日常操作

判定规则很直接：**只有 enabled 的 unit 才归 systemd 管**。

- 已 `enable`（常驻中）→ `start/stop/restart` 走 systemctl，需要 root，脚本会自动提权；
  等价 `systemctl <action> lite3-yolo-publish.service`。
- 未启用（当前默认）→ 走 PID 文件直管进程，**不需要 sudo**。

```bash
./lite3-publish-ctl.sh restart yolo        # 内部就是 systemctl restart（自动提权）
journalctl -u lite3-yolo-publish.service -f
systemctl status lite3-yolo-publish.service
```

> ⚠️ 常驻期间**不要**再手工 `setsid nohup python3 run_tracker_publish.py &`，
> 会和 systemd 抢同一路 RTSP/RTMP，表现为两条流互相顶掉。要手工跑就先 `./disable-resident.sh`。

```bash
./lite3-publish-ctl.sh restart yolo        # 内部就是 systemctl restart（自动提权）
journalctl -u lite3-yolo-publish.service -f
systemctl status lite3-yolo-publish.service
```

> ⚠️ 常驻期间**不要**再手工 `setsid nohup python3 run_tracker_publish.py &`，
> 会和 systemd 抢同一路 RTSP/RTMP，表现为两条流互相顶掉。要手工跑就先 `./disable-resident.sh`。

## 5. 命令速查表

| 命令 | 作用 | 需要 sudo |
| --- | --- | --- |
| `start <target>` | 启动（已启动则跳过，幂等） | 未启用时不需要；**常驻中自动 sudo** |
| `stop <target>` | 停止（未运行则跳过，幂等） | 同上 |
| `restart <target>` | 重启 | 同上 |
| `status <target>` | 状态 + 最近 3 行日志 | 否 |
| `logs <target> [-f]` | 看日志 | 否 |
| `mode [rtmp\|dual\|ros2]` | 查看/切换模式（改完需 restart） | 否 |
| `install` | 安装 unit（不启用不启动） | 是 |
| `uninstall` | 卸载 unit（先停并取消自启） | 是 |
| `enable <target>` | 开机自启 + 立即启动 | 是 |
| `disable <target>` | 停服务 + 取消自启 | 是 |

`<target>` 取值：`yolo` / `rosbridge` / `all`。

## 6. 配置说明（`yolo-publish.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YOLO8_PUBLISH_MODE` | `rtmp` | `rtmp` / `dual` / `ros2` |
| `YOLO8_RTMP_URL` | `rtmp://172.31.68.227:1936/live/lite3_yolo` | 推流地址（与 120 自带 `live/lite3` 并存，不冲突） |
| `YOLO8_RTMP_BITRATE` | `1000000` | 硬件编码码率 |
| `YOLO8_IMAGE_TOPIC` | `/yolo/image_annotated` | ROS2 话题 |
| `YOLO8_IMAGE_QOS` | `reliable` | Foxy 的 `ros2 topic hz` 只认 reliable |
| `YOLO8_JPEG_QUALITY` | `70` | ROS2 侧 JPEG 质量 |
| `YOLO8_ROS2_EVERY` | `3` | 每 N 帧发一次 ROS2（原 ~24fps → 约 8Hz） |
| `ROSBRIDGE_PORT` | `9090` | rosbridge 端口 |
| `HEADLESS` | `1` | 无桌面必开（会 patch `cv2.waitKey`） |
| `YOLO8_STATUS_INTERVAL` | `100` | 每 N 帧打印 `frames=/fps=/rtmp_err=` |

**改完必须 `restart` 才生效**（systemd 的 `EnvironmentFile` 只在启动时读一次）。

## 7. 依赖关系

| 依赖 | 用途 | 缺失后果 |
| --- | --- | --- |
| 120 的 RTSP 源 `rtsp://192.168.1.120:8554/test` | 视频输入 | 无帧推出，日志反复 `Stream format not found` |
| RTMP 服务器 `172.31.68.227:1936` | RTMP 输出 | 推流静默失败（总线报错） |
| `/opt/ros/foxy` | ROS2 通路、rosbridge | 无法 `start` |
| rosbridge:9090（仅 dual） | Foxglove 通路 | Foxglove 连不上，RTMP 不受影响 |
| `model/yolov8n_arm.engine` | 推理 | 起不来（x86 版 `_amd.engine` 不可用） |

机器人实时链路 `transfer_ros2.service` 与本机 `lite3-monitor.service` **与本部署无关，不要动**。

## 8. 注意事项

1. **冷启动约 35s**（RTSP 首帧 + TensorRT engine 加载），别用 20s 窗口判断"没起来"；`RestartSec=5` 已留余量。
2. **RTMP 是否真通别看 `push-buffer` 返回值**（华为/服务器连不上时它照样返回 OK），
   最硬证据是 `ffprobe -v error -show_entries stream=codec_name,width,height -of default=nw=1 rtmp://...`。
3. **ROS2 侧验证必须 `python3 -u /opt/ros/foxy/bin/ros2 topic hz /yolo/image_annotated`**，
   重定向 + 缓冲会让你看到"空输出"，误判成没发。
4. **日志在 `logs/`**（`/tmp` 会被系统清理）。未配 logrotate，长期常驻建议加轮转，否则单个日志会一直涨。
5. **停止用 `stop`（SIGTERM 打进程组）**，别直接 `kill -9` 单个 PID —— `ros2 run` 这类 wrapper 会留下子进程。
6. **9090 端口冲突**：若别的 rosbridge 已占用，rosbridge 会起不来，`status` 里看日志确认。
7. **资源占用**：端到端 22~26 fps，单核 ~80% CPU、内存 ~2.6GB。跑之前确认不是导航任务满负荷时段。
8. **同一台 RTMP 服务器可并存多路**：`lite3_yolo` 与 120 现有的 `lite3` 互不影响。
9. **停止时的报错是噪声**：`TEGRA_NVDEC_H264: Stream flush failed` / `NvVideoDecoderDecode failed`
   出现在正常停止瞬间（解码器被强杀时 flush 失败），不是故障。
10. **在 Windows 上改这些脚本会引入 CRLF**：远端 bash 会报 `/usr/bin/env: 'bash\r': No such file`。
    写完务必确认是 LF（`file xxx.sh` 不带 `CRLF`）。

## 9. 故障速查

| 现象 | 检查 | 处置 |
| --- | --- | --- |
| `start` 后立刻退出 | `logs yolo` 看 Traceback | 多半是 RTSP 源不可用或 model 路径错 |
| RTMP 拉不到流 | `ffprobe` 拉一次；`grep "\[gst ERROR\]" logs/yolo-publish.log` | 服务器不可达 → 确认 1936 端口；总线有错就 restart |
| `ros2 topic list` 没有 `/yolo/image_annotated` | `mode` 是否 `rtmp` | 切到 `dual` 后 restart |
| 话题在但 hz 为空 | 是否用了 `python3 -u`；QoS 是否 reliable | 见注意事项 2、3 |
| Foxglove 连不上 | `status rosbridge` + `ss -tlnp \| grep 9090` | start rosbridge；确认 IP/端口没填错 |
| 起了两份进程 | `ps -eo pid,etime,args \| grep run_tracker` | 先 `disable`/`uninstall`，再 `stop all` 清干净 |

## 10. 实测基线

RTMP 硬编 + ROS2 JPEG 双开时端到端 22~26 fps；YOLO 推理 19.1~19.9 ms/帧；
RTMP 输出 `h264 / 640×360 / 30fps`；ROS2 图像约 8 Hz。
详细踩坑记录见 `/home/test/yolo8/note/双通路发布-踩坑记录.md`。

## 11. 观看地址与流名（2026-09-23）

| 内容 | HTTP-FLV 播放地址 | 标注 |
| --- | --- | --- |
| 120 原始流 | `http://172.31.68.227:8080/live/lite3.flv` | 无（120 的转发，无框） |
| YOLO 标注流 | `http://172.31.68.227:8080/live/lite3_yolo.flv` | 红框 person / `NO PERSON` |

- 想看 YOLO 检测结果，**必须播 `live/lite3_yolo`**；`live/lite3` 是 120 的原始流，永远没有框。
- 两路流名不同，在同一台 SRS 上天然并存（已实测同时 active），互不抢占。
- 远程验流：`curl -sL http://172.31.68.227:1985/api/v1/streams/`（必须 `-L`）。

## 12. 「lite3 被踢」排障指引（2026-09-23 实录）

现象：`live/lite3`（120 的流）断了。先按顺序查，别急着怀疑 YOLO：

1. SRS 上流还在不在：`curl -sL http://172.31.68.227:1985/api/v1/streams/`。
2. 120 推流服务：`systemctl --user status rtmp-forward`（注意 120 重启后需用户会话/linger）。
3. **120 重启过的必查 NAT**（易失规则，103 的出网全靠它）：
   ```bash
   sudo iptables -t nat -S POSTROUTING | grep "192.168.1.0/24"
   # 没有就补（立即生效）：
   sudo iptables -t nat -A POSTROUTING -s 192.168.1.0/24 -o wlan0 -j MASQUERADE
   ```
   该行 2026-09-23 起已持久化进 `/home/ysc/host/host_start2.sh`（开机自动恢复）。
   判定口诀：**120 能上网而 103 不能 → 就是这条 NAT 丢了**。
4. 103 侧推流会话：`ss -tnp | grep 172.31.68.227`，ESTAB 才算在推。

## 13. 检测配置与标注策略

- `YOLO8_CONF`（env，默认代码 0.5、部署配置 0.4）：person 置信度阈值，
  远处/小目标检不出就调低，误检多就调高。
- 标注：纯正红框 (0,0,255) + `person conf` 标签；无检出叠红色 `NO PERSON`；
  左上角常驻 fps；**每帧必推**，无人时流不会断。
- ⚠️ Xavier NX(8G) 严禁并发加载两个 TRT 引擎（每个 ~4.2GB），会把整机卡死
  （表现为 ping 通但 SSH banner 超时，只能断电重启）。推理验证与 publish 串行执行。
