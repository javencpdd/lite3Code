# DRY-RUN 变更预演清单（只读）

> 生成时间：2026-09-24 · 目标主机：`ysc@<HOST_103>` · 目录：`/home/test`
> **本清单为只读预演**：描述"将要做什么、会动哪些文件、出事怎么回滚"，
> 生成清单本身**不修改任何配置文件、不启停任何服务、不执行 git 写入操作**。
> 请逐项确认后，再按 B 节命令执行落地。

---

## A. 将要新增 / 修改的文件

| # | 路径 | 类型 | 说明 | 冲突 |
|---|---|---|---|---|
| 1 | `/home/test/readme` | **修改（覆盖）** | 原文件 663 B（个人速记），重写为全局总结 + 复现入口 | ⚠️ 有，需先备份 |
| 2 | `/home/test/.gitignore` | 新增 | 版本管理过滤规则（分组注释版） | 无 |
| 3 | `/home/test/note/00-环境基线.md` | 新增 | 新主机初始化 | 无 |
| 4 | `/home/test/note/01-yolo视频检测与推流.md` | 新增 | YOLO 部署/排查 | 无 |
| 5 | `/home/test/note/02-机器狗监控面板.md` | 新增 | Monitor 部署/排查 | 无 |
| 6 | `/home/test/note/03-SCAN-Planner规划器.md` | 新增 | 规划器部署/排查 | 无 |
| 7 | `/home/test/note/04-Foxglove可视化与rosbridge.md` | 新增 | 远程可视化 | 无 |
| 8 | `/home/test/note/05-网络与出网配置.md` | 新增 | NAT/代理/apt 源 | 无 |
| 9 | `/home/test/note/06-零散测试脚本.md` | 新增 | 硬件自检与调试脚本 | 无 |
| 10 | `/home/test/DRYRUN-变更预演清单.md` | 新增 | 本文件 | 无 |

**不改动**的现有内容（仅读取用于保证路径一致）：

- `yolo8/deploy/yolo-publish.env`、`yolo8/deploy/systemd/*.service`
- `lite3_robot_monitor/deploy/{install.sh,pack.sh,*.service}`
- `scan_planner/scripts/*.sh`、`scan_planner/note/*.md`
- `/etc/systemd/system/lite3-*.service`（**不启停任何服务**）

## B. 预计执行的命令（落地步骤，当前未执行）

```bash
# --- B1 备份（在 103 上，唯一会动现有文件的动作）---
cp /home/test/readme /home/test/readme.bak-20260924

# --- B2 上传（在本机 Git Bash 上执行）---
SRC=/c/Users/19046/AppData/Local/Temp/lite3-docs
scp "$SRC/readme"                 ysc@<HOST_103>:/home/test/readme
scp "$SRC/.gitignore"             ysc@<HOST_103>:/home/test/.gitignore
scp "$SRC/DRYRUN-变更预演清单.md" ysc@<HOST_103>:/home/test/
scp -r "$SRC/note/."              ysc@<HOST_103>:/home/test/note/

# --- B3 修 CRLF（Windows 文本模式可能带入 \r，会让远端脚本报 bash\r）---
sed -i 's/\r$//' /home/test/readme /home/test/.gitignore /home/test/note/*.md

# --- B4 核验（在 103 上）---
ls -la /home/test/readme /home/test/.gitignore /home/test/DRYRUN-变更预演清单.md
ls -la /home/test/note/
wc -l /home/test/readme /home/test/note/*.md
file /home/test/readme
```

## C. 涉及的路径与配置项（只读，未修改）

| 路径 / 配置项 | 本次用途 |
|---|---|
| `/home/test/note/` | 已存在且为空，用作文末索引的目标目录 |
| `/home/test/readme` | 覆盖目标，原内容含部署速记与 IEEE754 速查，已并入新文档 |
| `/home/test/yolo8/deploy/yolo-publish.env` | 读取确认 `YOLO8_PUBLISH_MODE`、`ROSBRIDGE_PORT` 等默认值 |
| `/etc/systemd/system/lite3-*` | 读取确认已装单元（monitor / monitor2 / ros-bridge / ros-bridge2 / yolo-publish / yolo-rosbridge） |
| `/home/test/monitor/.venv` | 线上 monitor 的解释器位置（写入 `.gitignore` 的忽略范围） |

## D. 风险点与应对

| # | 风险 | 等级 | 应对 |
|---|---|---|---|
| R1 | 覆盖 `readme` 丢失原内容 | 中 | B1 先备份为 `readme.bak-20260924`；原内容中的部署命令与 IEEE754 速查已并入新文档第 6 节与 `note/06` |
| R2 | Windows 文本模式带入 CRLF | 中 | B3 统一 `sed -i 's/\r$//'`；重点是 `.gitignore`（CRLF 会导致规则失效） |
| R3 | `.gitignore` 误伤必需文件 | 中 | 已用 `!yolo8/deploy/yolo-publish.env` 例外保留唯一配置；落地后跑 `git check-ignore -v` 抽查 |
| R4 | 中文文件名 scp 乱码 | 低 | 上传后 `ls` 核对文件名；若乱码改用本地打包 `tar` 后在远端解包 |
| R5 | 文档路径与实际不符 | 中 | 所有路径均来自 2026-09-24 实测（`ls`/`find`/`systemctl cat`），非记忆 |
| R6 | 误启停服务导致业务中断 | — | 本次**不执行**任何 systemctl 操作；`transfer_ros2`（实时链路）全程不动 |

## E. 回滚方式

```bash
# 撤销 readme 覆盖
cp /home/test/readme.bak-20260924 /home/test/readme

# 撤销全部新增文件
rm -f /home/test/.gitignore /home/test/DRYRUN-变更预演清单.md
rm -f /home/test/note/00-环境基线.md \
      /home/test/note/01-yolo视频检测与推流.md \
      /home/test/note/02-机器狗监控面板.md \
      /home/test/note/03-SCAN-Planner规划器.md \
      /home/test/note/04-Foxglove可视化与rosbridge.md \
      /home/test/note/05-网络与出网配置.md \
      /home/test/note/06-零散测试脚本.md
# 注：note/ 目录原本就是空的，删除后即恢复原状
```

## F. 后续动作预演（**未执行**，确认后再做）

```bash
# F1 纳管预演：只看不写
cd /home/test && git init -b main
git add -An | wc -l                                   # 预期约 120~200 个文件（不含被忽略项）
git add -An | grep -E "\.bag$|\.engine$|/build/|/devel/|\.venv/"   # 期望无任何输出
git check-ignore -v yolo8/model/yolov8n_arm.engine    # 期望命中 *.engine
git check-ignore -v yolo8/deploy/yolo-publish.env     # 期望「未被忽略」（例外规则生效）

# F2 推送（103 直连 GitHub 不通，走本机中转）
git bundle create /tmp/home-test.bundle --all
# 本机：scp 下来 → git clone → remote set-url → push
```

F 段仅作预演说明，本次不执行。执行前请再次确认：仓库设为**私有**
（`note/05` 与各部署文档含 `192.168.x`、`<SRS_HOST>` 等内网信息）。
