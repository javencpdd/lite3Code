# 03 · SCAN-Planner 路径规划器（ROS1）

**适用场景**：在 103 上部署 SCAN-Planner（ROS1 Noetic），先仿真跑通，再接真机做自主导航。
**上游**：https://github.com/wuyi2121/SCAN-Planner `main` 分支（ROS1 版），本机 commit `348e8a5`。
⚠️ 不要用 `ros2-community` 分支——它要求 Ubuntu 22.04 + Humble + C++17，与 20.04/Foxy 不匹配。

## 操作步骤

### 1. 拉源码（103 直连 GitHub 不通，走代理）

```bash
export https_proxy=http://<PROXY_HOST>:7897 http_proxy=http://<PROXY_HOST>:7897
git clone https://github.com/wuyi2121/SCAN-Planner.git /home/test/scan_planner/src
```

备选：`https://gh-proxy.com/https://github.com/wuyi2121/SCAN-Planner.git`（完整 clone 比代理慢）；
兜底：本机下载后 `scp` 上机。

### 2. 环境隔离（**每次新终端跑 ROS1 前必做**）

```bash
source /home/test/scan_planner/scripts/env.sh        # 清掉 foxy，只加载 noetic
source /home/test/scan_planner/scripts/env_restore.sh # 用完复原
```

`env.sh` 会先把原环境快照到 `logs/env_backup_<时间戳>_<pid>.sh`，复原时精确还原（含"原本未设置"）。

### 3. 编译与冒烟

```bash
source /home/test/scan_planner/scripts/env.sh
bash /home/test/scan_planner/scripts/build.sh        # catkin_make -j2，约 14 min
bash /home/test/scan_planner/scripts/check.sh        # 环境自检
bash /home/test/scan_planner/scripts/smoke.sh        # 仿真冒烟（navi_mode=1，起点(-19,1)→目标(5,0)）
bash /home/test/scan_planner/scripts/run_sim.sh      # 起仿真
bash /home/test/scan_planner/scripts/goal.sh         # 下发目标点
bash /home/test/scan_planner/scripts/ab_test.sh      # A/B 参数回归
```

### 4. 真机接入（FAST-LIO 话题适配）

```bash
source /home/test/scan_planner/scripts/env.sh
bash scripts/start_lio_relay.sh        # 起 LIO 中继（补 /LIO/* 话题）
bash scripts/test_lio_relay.sh         # 中继自检
bash scripts/start_scan_real.sh        # 真机规划启动（自带 master 探活）
bash scripts/test_realworld_e2e.sh     # 端到端验证（可用 scripts/fake_lio.py 造假数据源）
```

## 关键配置文件路径

| 类型 | 路径 |
|---|---|
| 工作区 | `/home/test/scan_planner`（`src/` `build/` `devel/` `logs/`） |
| 环境隔离 | `scripts/env.sh` / `scripts/env_restore.sh` |
| 编译/仿真/真机 | `scripts/{build,smoke,run_sim,goal,ab_test,start_lio_relay,start_scan_real,test_lio_relay,test_realworld_e2e}.sh` |
| 环境快照 | `logs/env_backup_*.sh`、`logs/env_origin.sh` |
| 机体参数 | `src/.../advanced_param.xml`（Lite3 包络与速度已适配） |
| 机体模型 | `src/.../lite3_description`（官方 URDF + 网格，新增包） |
| 深度文档 | `note/01-兼容性评估.md` ～ `note/05-避坑事项.md` |

## 验证方法

```bash
source /home/test/scan_planner/scripts/env.sh
echo $ROS_DISTRO                                    # 期望 noetic
ls /home/test/scan_planner/devel/lib                # 期望 9 个可执行文件
bash /home/test/scan_planner/scripts/smoke.sh       # 期望抵达目标，约 24 m 路径
source /home/test/scan_planner/scripts/env_restore.sh
echo $ROS_DISTRO                                    # 期望 foxy（复原成功）
```

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `git clone` 卡死 | 103 直连 GitHub TCP 443 超时 | 走代理 `http://<PROXY_HOST>:7897` 或 gh-proxy 镜像 |
| 编译时报 foxy/noetic 混用 | 默认 source 了 foxy | 先 `source scripts/env.sh` |
| 编译到一半系统卡死 | `-j` 开太大，6 GiB 内存不够 | 统一用 `-j2` |
| `source env.sh` 报 unbound variable | `set -u` 与 ROS setup.bash 冲突 | 用 `set +u` / `set -u` 包裹 |
| FAST-LIO 无 `/LIO/*` 话题 | 实际发的是 `/Odometry` + `/cloud_registered_body` | 用 `lio_relay.launch` 补齐 |
| 规划结果尺寸/速度不对 | 上游默认参数面向 Unitree Go2 | 改 `advanced_param.xml`（已适配 Lite3，换机需复核） |
| ROS1/ROS2 话题不通 | 跨栈 | 用 `ros-foxy-ros1-bridge`；**只需桥 `/cmd_vel` 一个话题** |

## 回滚方式

- 环境：`source scripts/env_restore.sh`（读取 `logs/env_backup_*.sh` 精确复原）。
- 编译：`rm -rf build devel` 后重跑 `build.sh`（约 14 min）。
- 误改参数：`git -C src checkout -- <文件>`（前提是已纳入 Git）。
- ⚠️ 不要动 `transfer_ros2.service`，它承载实时链路；ROS1 实验一律在隔离终端里做。

## 进度

已完成：clone / 编译 / 仿真闭环 / ros1_bridge / Lite3 模型与参数适配 / FAST-LIO 话题适配 / 临时环境脚本 / 真机封装脚本。
⏳ 未开始：真机本体接入（见 `note/05-避坑事项.md` 第 9 章 6 步清单）。
