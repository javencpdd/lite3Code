# lite3Code

Lite3 四足机器人**感知导航主机**（Jetson Xavier NX · Ubuntu 20.04 · ROS1 + ROS2）上的功能复现仓库：
视频检测推流、机器狗监控面板、路径规划与远程可视化，以及配套的部署与排障文档。

> 目录结构、环境基线、标准复现流程见 **[readme](readme)**；
> 分模块的部署步骤与排障手册见 **[note/](note/)**。

## 功能一览

| 模块 | 说明 | 文档 |
|---|---|---|
| YOLOv8 视频检测与推流 | RTSP 拉流 → TensorRT 检测 → RTMP / ROS2 双通路发布 | [note/01](note/01-yolo视频检测与推流.md) |
| 机器狗监控面板 | UDP 43897 旁路抓包 → WebSocket 实时面板 | [note/02](note/02-机器狗监控面板.md) |
| SCAN-Planner 路径规划 | ROS1 Noetic，仿真闭环与真机接入 | [note/03](note/03-SCAN-Planner规划器.md) |
| Foxglove 可视化 | rosbridge WebSocket，远程看图 | [note/04](note/04-Foxglove可视化与rosbridge.md) |
| 网络与出网配置 | NAT、代理、apt 源 | [note/05](note/05-网络与出网配置.md) |
| 环境基线 / 零散测试脚本 | 新主机初始化、硬件自检 | [note/00](note/00-环境基线.md) / [note/06](note/06-零散测试脚本.md) |

## 环境要求

- Jetson Xavier NX（6 核 / 6 GiB 起），Ubuntu 20.04.6，内核 `5.10.120-tegra`（JetPack R35.4.1）
- ROS **Noetic**（ROS1）+ **Foxy**（ROS2），Python 3.8
- GStreamer 硬编解码、TensorRT

## 快速开始

1. 读 [readme](readme) 的「标准复现流程」，按阶段 0 → 5 顺序执行。
2. 各模块的具体命令、验证方法与常见问题查 `note/` 下对应文档。
3. **部署前替换占位符**：内网地址已统一写成 `<HOST_103>`、`<HOST_120>`、`<LAN_NET>`、`<SRS_HOST>`、`<PROXY_HOST>` 等，
   对照表见 [readme](readme) 的「占位符对照表」。

## 关于第三方代码

本仓库只收录自有源码、部署脚本与文档。以下内容**不在本仓库**，请按其各自许可从上游获取：

- SCAN-Planner 上游源码（EGO-Planner 系）与 Unitree 模型资产 —— 见 [note/03](note/03-SCAN-Planner规划器.md)
- Ultralytics YOLOv8（AGPL-3.0）—— 以 pip 依赖方式引入，见 `yolo8` 说明

## 许可

本仓库采用 **[PolyForm Noncommercial License 1.0.0](LICENSE)**：

- ✅ 允许为**非商业目的**使用、修改（二次开发）与分发本软件及其衍生作品
- ❌ 禁止任何**商业用途**
- 📌 分发时须一并保留许可条款与开头的版权声明行

> 说明：这不是 OSI 认证的开源许可（开源许可均允许商用），它是专为"可二开、禁商用"场景设计的软件许可。
> 商业授权请联系仓库作者。
