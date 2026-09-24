# 部署到 103 感知导航主机

**目标机**：Jetson Xavier NX · Ubuntu 20.04 / Python 3.8 · 登录账号 `ysc` · IP `192.168.1.103`
**安装目录**：`/home/test/monitor`
> 注意：`/home/test` 是**普通目录名**，不是账号家目录（账号家目录是 `/home/ysc`）。
> 容易混淆，SSH 时用 `ysc@`，路径里用 `/home/test/...`。

> 前提：103 上 `transfer_ros2` 已独占 UDP 43897，本方案**不会**与它抢端口。

---

## 一、为什么不能直接 bind 43897

UDP 单播端口被两个进程同时 bind 时，Linux **不会**给两份拷贝，后 bind 的会把报文抢走。

```
Monitor 抢走 43897 → transfer_ros2 收不到状态
                   → leg_odom2 / /imu/data 断流
                   → Nav2 失去本体里程计
```

因此本项目在 103 上默认使用 **旁路抓包（sniff）**：通过 `AF_PACKET` 从链路层读取流经网卡的报文，
**不 bind 任何端口**，与 `transfer_ros2` 完全共存、零侵入。

| 模式 | 环境要求 | 占用 43897 | 适用 |
| --- | --- | --- | --- |
| `sniff` | Linux + `CAP_NET_RAW` | ❌ 不占用 | **103 部署（推荐）** |
| `bind` | 任意 | ✅ 占用 | Windows 开发机、无 root 权限时 |

`LITE3_UDP_MODE=auto`（默认）会优先 sniff，失败自动退回 bind。

---

## 二、部署步骤

### 方式 A：打包上传（推荐）

项目里 `frontend/node_modules` 有几百 MB，没必要传。用打包脚本只带必需文件：

```bash
# 在笔记本的 Git Bash / WSL 中
cd lite3_robot_monitor
bash deploy/pack.sh                                   # 生成 lite3-monitor-deploy.tar.gz
scp lite3-monitor-deploy.tar.gz ysc@192.168.1.103:/tmp/
```

```bash
# 在 103 上
ssh ysc@192.168.1.103
cd /tmp && tar xzf lite3-monitor-deploy.tar.gz
sudo bash lite3_robot_monitor/deploy/install.sh /home/test/monitor
```

### 方式 B：直接传目录

```bash
# 笔记本（PowerShell 自带的 scp 也可以）
ssh ysc@192.168.1.103 "mkdir -p /home/test/monitor"
scp -r backend deploy requirements.txt README.md ysc@192.168.1.103:/home/test/monitor/
# 已构建过前端的话，再传静态产物
ssh ysc@192.168.1.103 "mkdir -p /home/test/monitor/frontend"
scp -r frontend/dist ysc@192.168.1.103:/home/test/monitor/frontend/
```

```bash
# 103 上
sudo bash /home/test/monitor/deploy/install.sh /home/test/monitor
```

### 走内网 pip 源

```bash
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple sudo -E bash deploy/install.sh /home/test/monitor
```

### 安装脚本做了什么

1. 检查 Linux / Python ≥ 3.8
2. 拷贝 `backend/`、`tools/`、`requirements.txt`、可选的 `frontend/dist`
3. 建 `.venv` 并装依赖（纯 Python 版 uvicorn，避免 aarch64 编译）
4. 运行 `udp_sniffer.py` 自检
5. 把安装目录属主交还 `ysc`（sudo 拷贝后默认是 root）
6. 注册并启动 systemd 服务
7. 打印访问地址与验证要点

**不会改动** `transfer_ros2`、`jy_exe` 及任何现有配置。

---

## 三、验证

```bash
curl http://127.0.0.1:8000/api/status
```

```json
{
  "udp_mode": "sniff",      ← 必须是 sniff，说明没占用 43897
  "connected": true,        ← true 说明已收到机器人状态
  "packets_received": 12345
}
```

笔记本浏览器打开 **http://192.168.1.103:8000**（笔记本连狗的热点时，经 120 转发可达）。

若打不开，在 103 上放行防火墙：

```bash
sudo ufw allow from 192.168.2.0/24 to any port 8000 proto tcp comment 'lite3-monitor'
```

---

## 四、排错表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `udp_mode` 是 `bind` | 抓包启动失败，已降级 | `journalctl -u lite3-monitor -n 50`，多为权限 |
| 日志报 `PermissionError` | 缺 `CAP_NET_RAW` | 确认 unit 里 `AmbientCapabilities=CAP_NET_RAW`；或把 `User=test` 改成 `User=root` 后 `daemon-reload` |
| `connected` 一直 false | ① 抓错网卡 ② 机器人没开机 ③ 状态没到 103 | 见下 |
| 抓错网卡 | 状态流走业务网 | unit 里设 `Environment=LITE3_UDP_IFACE=eth0`（填 103 连 `192.168.1.120` 的那张网卡） |
| 浏览器打不开 | 防火墙 / 服务没起 | `systemctl status lite3-monitor` + `ufw allow 8000/tcp` |
| 页面能开但 Offline | 数据链路问题 | 在 103 上确认数据是否到达：`sudo tcpdump -i any -nn udp dst port 43897 -c 5` |

**最有价值的一步**（先确认 103 到底有没有收到状态）：

```bash
sudo tcpdump -i any -nn udp dst port 43897 -c 5
```

- 有输出 → 数据在，问题在 Monitor（检查网卡名、权限）
- 无输出 → 数据没到 103，去查 120 的 `jy_exe/conf/network.toml`

### 常用命令

```bash
systemctl status lite3-monitor
journalctl -u lite3-monitor -f
systemctl restart lite3-monitor
```

手动前台运行（抓包必须 sudo）：

```bash
cd /home/test/monitor/backend
sudo LITE3_UDP_MODE=sniff ../.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 五、控制通道（可选，默认关闭）

控制指令直连运动主机的 `192.168.1.120:43893`，**不经过 VOA**，因此默认关闭。
如需在 103 上启用远程控制，在 unit 的 `[Service]` 段加：

```ini
Environment=LITE3_CTRL_IP=192.168.1.120
Environment=LITE3_CTRL_PORT=43893
Environment=LITE3_CTRL_HEARTBEAT=0.25
Environment=LITE3_CTRL_LEASE=5.0
Environment=LITE3_CTRL_MAX_LINEAR=1.0
Environment=LITE3_CTRL_MAX_ANGULAR=1.5
# 保持 false：由前端按需启用，避免服务重启后自动进入可控制状态
Environment=LITE3_CTRL_ENABLED=false
```

> 心跳周期必须 < 1s（官方 SDK：1s 无指令即收回控制权）；
> 租约 5s 是防「浏览器关闭后后台持续发送」的兜底。

协议细节、待确认的指令码与安全约束见主 README 第十二章。

---

## 六、与现有服务共存

| 服务 | 端口 | 关系 |
| --- | --- | --- |
| `transfer_ros2` | 收 43897 | **不受影响**，Monitor 只是旁路"看"同一份报文 |
| `transfer_ros2` | 发 120:43893 | Monitor 不参与，只读 |
| `jetson2motion` | ROS2 Topic | 同上 |
| `lite3-monitor` | HTTP 8000 | 新增，仅此一个对外端口 |

资源开销：单进程，常驻内存约 40–60 MB，空闲 CPU < 1%。
（建议设 `LITE3_UDP_IFACE` 为业务网网卡，避免把 103 拉取的 RTSP 视频流也一并抓进来。）

---

## 七、回滚

```bash
sudo systemctl stop lite3-monitor
sudo systemctl disable lite3-monitor
sudo rm /etc/systemd/system/lite3-monitor.service
sudo systemctl daemon-reload
sudo rm -rf /home/test/monitor
```

现有 ROS 服务、120 侧配置全程未被修改，回滚后系统回到部署前状态。

---

## 八、并行部署第二套实例（保留正在运行的版本）

若 103 上已有成功运行的 `lite3-monitor`（目录 `/home/test/monitor`、HTTP 8000、ros-bridge 43900），
想把改版后版本部署到另一目录**且旧实例保持运行**，用 `TAG` 后缀做实例隔离即可——两者服务名、端口完全不冲突。

| 实例 | 部署目录 | systemd 服务 | HTTP 端口 | ros-bridge 服务 | ROS 桥接端口 |
| --- | --- | --- | --- | --- | --- |
| 旧（运行中） | `/home/test/monitor` | `lite3-monitor` | 8000 | `lite3-ros-bridge` | 43900 |
| 新（第二套） | `/home/test/monitor2` | `lite3-monitor2` | 8002 | `lite3-ros-bridge2` | 43902 |

### 完整流程（笔记本 → 103）

```bash
# —— 笔记本（Git Bash / WSL）——
cd lite3_robot_monitor
bash deploy/pack.sh                                    # 打出精简包 lite3-monitor-deploy.tar.gz
                                                       # 含 backend/ deploy/ tools/ requirements.txt README.md frontend/dist
scp lite3-monitor-deploy.tar.gz ysc@192.168.1.103:/tmp/

# —— 103 上 ——
ssh ysc@192.168.1.103
cd /tmp && tar xzf lite3-monitor-deploy.tar.gz
# ⚠️ 第二个参数 2 是 TAG 后缀，务必带上：
#    不带就等价于安装到默认目录 /home/test/monitor，会动到正在运行的旧实例
sudo bash /tmp/lite3_robot_monitor/deploy/install.sh /home/test/monitor2 2
```

> 若 103 无法访问 PyPI 导致依赖装不上，用离线轮子：
> 笔记本 `pip download -r backend/requirements.txt -d wheels --platform manylinux2014_aarch64 --python-version 3.8 --only-binary=:all:`，
> 传到 103 后 `pip install --no-index --find-links=wheels -r requirements.txt`。

### 之后的日常增量更新

首次安装之后，改了代码不必再打包，直接 rsync 并只重启 monitor2：

```bash
LITE3_TAG=2 LITE3_REMOTE=/home/test/monitor2 \
  LITE3_HOST=ysc@192.168.1.103 bash deploy/deploy_103.sh
```

> 前端改了的话，先在笔记本重新构建再把 dist 传上去：
> `cd frontend && npm run build`（本仓库 vite 版本不认 CLI `--root`，需用 Node API `build({root})`）。

### 验证与回滚

```bash
systemctl status lite3-monitor         # 旧：应仍 active
systemctl status lite3-monitor2        # 新：应 active
curl -s http://127.0.0.1:8000/api/status | head   # 旧：应不受影响
curl -s http://127.0.0.1:8002/api/status | head   # 新：udp_mode / data_source
```

重点确认 `/api/status` 里 `data_source` 与 `udp_mode`：
`set_mode` 建议保持 `auto`（ROS topic 订阅为主 + sniff 兜底，且 ros 恢复后会自动切回，
见 `docs/ros_bridge.md`）。页面访问 `:8002`。

若 ufw 开启，需放行新端口：`sudo ufw allow from 192.168.2.0/24 to any port 8002 proto tcp`。

回滚第二套：`sudo systemctl stop lite3-monitor2 lite3-ros-bridge2 && sudo systemctl disable lite3-monitor2 lite3-ros-bridge2 && sudo rm /etc/systemd/system/lite3-monitor2.service /etc/systemd/system/lite3-ros-bridge2.service && sudo systemctl daemon-reload && sudo rm -rf /home/test/monitor2`。
