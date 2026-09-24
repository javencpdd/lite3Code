** This session may be vulnerable to "store now, decrypt later" attacks.
# SCAN-Planner 部署到 103 主机：评估结论与方案

> 目标仓库：https://github.com/wuyi2121/SCAN-Planner （main 分支，ROS 1 版）
> 目标主机：103（lite3-f20-1-103 / 192.168.1.103，用户 ysc）
> 作业空间：`/home/test/scan_planner`
> 文档状态：**已执行**（已 clone、已编译通过、仿真闭环跑通、Lite3 参数适配已完成并通过双次复跑）
> 编写时间：2026-09-21　最后更新：2026-09-23 02:41

## 〇、当前进度（一眼看完）

| 阶段 | 状态 | 说明 |
|---|---|---|
| 环境准备（代理 / apt 源 / ROS1 隔离） | ✅ 完成 | 清华 403 源已换 `ports.ubuntu.com`；代理可用 |
| 拉取源码 | ✅ 完成 | 走代理 clone，commit `348e8a5` |
| 编译（`-j2`，约 14 min） | ✅ 通过 | 0 错误，9 个可执行文件 + 2 个自定义 msg |
| 仿真冒烟（navi_mode=1，lidar） | ✅ 通过 | 起点 (−19,1) → 目标 (5,0)，约 24 m，正常抵达 |
| ros1_bridge 安装 | ✅ 完成 | apt `ros-foxy-ros1-bridge 0.9.7-1focal`（arm64） |
| 桥接方案修正 | ✅ 完成 | **只需桥 1 个 `/cmd_vel`**，点云/里程计原生同域 |
| Lite3 模型替换（官方 URDF + 网格） | ✅ 完成 | 新增 `lite3_description` 包 |
| Lite3 参数适配 + A/B 回归 | ✅ 完成 | 见 `05-避坑事项.md` 第 8 章，双次复跑通过 |
| FAST-LIO 话题适配（`/LIO/*`） | ✅ 完成 | FAST-LIO 实际发 `/Odometry` + `/cloud_registered_body`，已用 `lio_relay.launch` 补齐（第 10 章），假数据源端到端验证通过 |
| 临时环境脚本（建立 + 备份 + 复原） | ✅ 完成 | `scripts/env.sh` 自动快照、`scripts/env_restore.sh` 精确复原，往返已实测 |
| 真机封装脚本（自带 master 探活） | ✅ 完成 | `scripts/start_lio_relay.sh`、`scripts/start_scan_real.sh` |
| **真机本体接入** | ⏳ 未开始 | 见 `05-避坑事项.md` 第 9 章，6 步清单 |

---

## 一、结论（先行）

**结论：可以部署，且兼容性风险低。建议按"先离线落地 + 仿真冒烟，再真机接入"两阶段推进。**

判定依据一句话概括：SCAN-Planner main 分支要求的运行环境是 **Ubuntu 20.04 + ROS Noetic + Eigen3/PCL/OpenCV/Armadillo**，而 103 实测恰好是 **Ubuntu 20.04.6 + ROS Noetic（347 个包）+ PCL 1.10 + Eigen 3.3.7 + OpenCV 4.2 + Armadillo 9.8**，编译工具链（gcc 9.4 / cmake 3.16）与全部 catkin 依赖均已就位，源码中也没有 x86 专属指令或 CUDA 硬依赖，aarch64 上不存在架构阻断项。

但有三条必须提前接受的现实约束：

| # | 约束 | 影响 | 处置 |
|---|---|---|---|
| C1 | **103 直连 GitHub 不通**（可解析但 TCP 超时），国内镜像/ROS/NVIDIA 源正常 | 裸 `git clone https://github.com/...` 会卡死 | 走本机代理 `http://192.168.2.47:7897`（**实测 clone 成功，4m29s**）；或 `gh-proxy.com` 镜像；离线 scp 仅作兜底 |
| C2 | **103 默认跑 ROS 2 Foxy**（`transfer_ros2.service` 常驻），而 SCAN-Planner 主分支是 ROS 1 | 环境串味、话题不通 | 编译/运行前用专用脚本只 source Noetic；真机数据用 `ros1_bridge` 跨栈桥接 |
| C3 | **SCAN-Planner 默认参数与执行器面向 Unitree Go2**，103 载的是 Lite3 | 尺寸/速度/步态接口不匹配 | 阶段二改 `advanced_param.xml` 的机体包络与速度参数，并用 `/cmd_vel` 对接 Lite3 控制口 |

分级建议：

- **阶段一（可行、低风险、建议立即做）**：离线落地源码 → `catkin_make` 编译 → 用自带仿真器（mockamap + pcl_render_node）跑通 navi_mode 1/2/3。此阶段**不碰真机、不改主机现有 ROS 2 服务**。
- **阶段二（可行、中风险）**：接真机。需要 ros1_bridge（foxy↔noetic）或等效中继，把 faster_lio 的里程计与雷达点云喂给规划器，并把 `/cmd_vel` 回传给 Lite3。
- **不建议**：使用 `ros2-community` 分支 —— 该分支明确要求 **Ubuntu 22.04 + ROS 2 Humble + C++17**，与 103 的 20.04/Foxy 不匹配。

---

## 二、文档索引

| 文件 | 内容 |
|---|---|
| `01-兼容性评估.md` | 项目依赖清单、103 实测环境、逐项对照表、风险登记与降级方案 |
| `02-部署执行方案.md` | 准备工作、作业空间目录设计、离线传输、编译、三级验证、真机接入方案、参数适配清单 |
| `03-命令清单与回滚.md` | 可直接复制执行的命令集（含代理配置 A0、打包/传输/编译/验证）、回滚与清理步骤 |
| `04-阶段一执行报告.md` | 阶段一落地结果：编译产物、仿真冒烟数据、ros1_bridge 需求修正说明 |
| **`05-避坑事项.md`** | ⭐ **踩坑手册**：网络/ ROS1 隔离/ 编译/ 桥接/ Shell 操作/ 仿真判读/ Lite3 参数 全链路坑点与对策，含 A/B 实测数据表。**开工前先看这一份** |

### 脚本清单（`scripts/`）

| 脚本 | 作用 |
|---|---|
| `env.sh` | **建立 ROS1 临时环境**（source 用）。改动前先快照 18 个环境变量到 `logs/env_backup_<ts>_<pid>.sh` |
| `env_restore.sh` | **关闭临时环境并复原**（source 用）。默认复原最近一次快照，也可带参数指定某一份 |
| `build.sh` / `check.sh` | 编译（固定 `-j2`）／依赖自检 |
| `run_sim.sh` / `smoke.sh` / `goal.sh` | 仿真启动、冒烟、发目标点 |
| `ab_test.sh` | 无人值守 A/B（**测量期间禁止手动 rostopic**，见避坑 6.1） |
| `fake_lio.py` / `test_lio_relay.sh` / `test_realworld_e2e.sh` | 假数据源 + relay 与真机链路的离线验证 |
| `start_lio_relay.sh` | 真机：先探 master 再启 `/LIO/*` 中继，连不上 20s 内报错退出 |
| `start_scan_real.sh` | 真机：以 `is_real_world:=true navi_mode:=1 need_extrinsic:=false` 启动规划器 |

---

## 二·补、临时环境的建立与复原

103 的 `~/.bashrc` 默认 source **foxy**，而 SCAN-Planner 是 ROS 1，所以每个终端开工前都要切环境。
`env.sh` 在动手改任何变量**之前**先把原值存下来，收工时 `env_restore.sh` 精确复原：

```bash
cd /home/test/scan_planner

source scripts/env.sh          # 建环境；同时生成 logs/env_backup_<时间戳>_<pid>.sh
                               # 输出: [env] ROS_DISTRO=noetic  AMENT=[empty]
                               #       [env] ROS_MASTER_URI=http://192.168.1.103:11311

# ... 干活 ...

source scripts/env_restore.sh  # 关环境；含"原本没设"的变量也会被 unset
                               # 输出: [restore] ✅ 已复原: env_backup_xxxx.sh
```

要点：

- 覆盖 18 个变量：`PATH`、`PYTHONPATH`、`LD_LIBRARY_PATH`、`PKG_CONFIG_PATH`、`CMAKE_PREFIX_PATH`、
  `ROS_*`（MASTER_URI / HOSTNAME / IP / DISTRO / VERSION / PYTHON_VERSION / ROOT / ETC_DIR / PACKAGE_PATH）、
  `AMENT_PREFIX_PATH`、`COLCON_PREFIX_PATH`、`ROS_DOMAIN_ID`、`RMW_IMPLEMENTATION`。
- 原本**没设**的变量在快照里写 `unset`，不是空串，复原后不留空壳。
- 同一 shell 内重复 `source env.sh` **不会**覆盖快照（靠 `SCAN_PLANNER_ENV_BACKUP` 标记），
  否则第二次会把 noetic 的值误当成"原值"。
- 历史快照：`ls -lt logs/env_backup_*.sh`；指定复原：
  `source scripts/env_restore.sh logs/env_backup_0923_023000_1234.sh`

> 忘了 source 就敲命令的典型症状：`roslaunch: command not found`（foxy 里没有 roslaunch）。

---

## 三、一页速览：关键实测数据

### 103 主机（实测，非文档推断）

```
hostname      : lite
型号          : NVIDIA Jetson Xavier NX Developer Kit
内核 / 架构   : 5.10.120-tegra / aarch64
JetPack       : R35.4.1（CUDA 11.4 在 /usr/local/cuda-11.4）
系统          : Ubuntu 20.04.6 LTS (focal)
CPU / 内存    : 6 核 / 约 6.7 GiB，swap 3 GiB
磁盘          : / 共 117G，已用 79G，可用 33G
编译链        : gcc/g++ 9.4.0，cmake 3.16.3，git 2.25.1，Python 3.8.10
ROS           : /opt/ros/noetic（347 个包）+ /opt/ros/foxy；~/.bashrc 默认 source foxy
库            : Eigen 3.3.7 / PCL 1.10.0 / OpenCV 4.2.0 / Armadillo 9.800.4 / Boost(filesystem,iostreams,program_options,serialization,system) / libglew-dev 2.1.0
网络          : IPv4 出网可用（默认路由 via 192.168.1.120）；GitHub 直连超时，
                国内镜像/ROS 源/NVIDIA 源正常；走 192.168.2.47:7897 代理可直连 GitHub
常驻服务      : transfer_ros2.service（ROS 2 Foxy）、lite3-monitor.service
现有资源      : /home/ysc/lite_cog_ros2/{driver(mid360_ws,leishen_ws,orbbec_ws,realsense_ws), slam(faster_lio,pcd2grid,octomap), nav(dr_nav2,hdl_*), transfer}
作业目录      : /home/test/scan_planner 已存在且为空，属主 ysc:ysc，可写
```

### SCAN-Planner（main 分支，源码实测）

```
许可证        : Apache-2.0
构建系统      : catkin_make（ROS 1，无顶层 CMakeLists，仓库根即工作空间根）
官方测试环境  : Ubuntu 20.04 + ROS Noetic
源码规模      : src 62 MB（.git 另 35 MB，525 个文件）
ROS 包数量    : 12 个
  规划器      : plan_manage / plan_env / path_searching / bspline_opt / traj_utils
  仿真        : local_sensing / map_generator / mockamap
  仿真工具    : go2_description / odom_visualization / pose_utils / waypoint_generator
硬依赖        : Eigen3 ≥3、PCL ≥1.7、OpenCV、catkin 组件
              （roscpp/rospy/std_msgs/geometry_msgs/nav_msgs/sensor_msgs/
                visualization_msgs/tf/message_filters/message_generation/
                cv_bridge/pcl_ros/pcl_conversions/cmake_modules/roslaunch）
仅仿真需要    : Armadillo（pose_utils）、OpenMP、Boost
仅 GPU 版需要 : GLEW + glfw3 + 桌面 OpenGL（默认 OFF）
架构相关      : 无 SSE/AVX 内联汇编；无 CUDA 代码；唯一 x86_64 资产是 third_party 预编译 GLFW（USE_GPU=ON 且系统无 glfw 时才可能用到）
运行期资源    : 纯 CPU 算法，无 GPU 推理
```

---

## 四、网络复测结论（2026-09-21 01:47–18:03 二次实测，推翻初次结论）

初次评估（01:24）判定"103 无外网"，复测发现**网络状态是波动的**，当前已恢复且明显更好：

| 测试项 | 结果 |
|---|---|
| 默认路由 | `default via 192.168.1.120 dev eth0`（103 的上联是 120 主机） |
| DNS | 正常（223.5.5.5 / 119.29.29.29），github.com 等均可解析 |
| IPv4 | **通**（ustc 镜像 0.16s、tuna 0.26s） |
| IPv6 | 不通（无全局 IPv6 地址、无 IPv6 默认路由） |
| GitHub 直连 | ❌ 超时（`20.205.243.166:443` 连不上） |
| GitHub 镜像 | ✅ `gh-proxy.com`、`ghproxy.net` 的 git 协议可用（返回正确 commit `348e8a5`） |
| **代理 192.168.2.47:7897** | ✅ **可用**。103→代理 ping 1.8ms，HTTP 代理访问 GitHub `200 / 1.8s`；**实测 clone 成功，4m29s，122M，commit `348e8a5` 校验一致** |
| apt | ROS 源(`packages.ros.org`)、NVIDIA(`repo.download.nvidia.com`)、`ports.ubuntu.com` 均 Hit；清华 `ubuntu-ports` 返回 403（该源异常，与代理无关） |
| **ros-foxy-ros1-bridge** | ✅ **可直接 apt 安装**（候选 `0.9.7-1focal`，arm64，来自 ROS 源）—— 阶段二桥接难点大幅降低 |

**结论：可以而且应该用代理加速。** 部署路径从"离线 scp"升级为"103 上直接走代理 clone"，省去本机打包传输环节。

---

## 五、下一步（阶段二：真机接入）

已完成：clone / apt 源 / 编译 / ros1_bridge / Lite3 尺寸参数 / FAST-LIO 话题适配。
当前只剩真机接入，**详细步骤见 `05-避坑事项.md` 第 9 章**，摘要：

```bash
cd /home/test/scan_planner

# ① 雷达（注意: c16.yaml 是镭神 C16, 配 start_lslidar.sh, 不是 start_livox.sh）
cd /home/ysc/lite_cog/system/scripts/lidar && bash start_lslidar.sh
# ② SLAM（输出 /Odometry 与 /cloud_registered_body, 不是 /LIO/*）
bash /home/ysc/lite_cog/system/scripts/slam/start_slam.sh
# ③ 补出 /LIO/* 三话题（脚本内部自带 source env.sh + master 探活）
bash scripts/start_lio_relay.sh
# ④ SCAN-Planner 真机模式（先不接机器人）
bash scripts/start_scan_real.sh
# ⑤ 只桥一个 Twist
ros2 run ros1_bridge parameter_bridge /scan_planner/cmd_vel@geometry_msgs/msg/Twist@geometry_msgs/Twist
# ⑥ 首次上电：四腿离地 + 限速，再落地
```

> ③ ④ 也可以手动跑：`source scripts/env.sh` 后再
> `roslaunch scan_planner lio_relay.launch` /
> `roslaunch scan_planner run.launch is_real_world:=true navi_mode:=1 sensor_type:=lidar need_extrinsic:=false`。
> 但**裸敲 `roslaunch` 会报 command not found**——终端默认是 foxy，必须先建环境。

⚠️ 上述 6 步中的**真机部分尚未执行**；第 ①～④ 的数据链路已用假数据源端到端验证通过。

---

## 六、采纳的 Lite3 参数（回归通过）

```
double_cylinder_radius        0.18   # 包络 0.60 m × 0.36 m（源自官方 URDF 实测）
double_cylinder_offset        0.12
body_height                   0.30   # 仅 navi_mode=3 生效，navi_mode=1 下为空操作
max_vel                       0.75   # 开阔场地再显式覆盖到 1.0
closed_loop_controller/max_vy    0.50
closed_loop_controller/max_vyaw   1.00   # 受源码 kMaxVYawLimit 钳位，最高只能 1.0
```

对照组（`run.launch`）：`robot_pkg:=go2_description` 可随时切回 Go2 模型。
