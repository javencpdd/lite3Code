#!/bin/bash
# ============================================================
# SCAN-Planner 专用 ROS1 环境隔离脚本
# 103 的 ~/.bashrc 默认 source ROS2 foxy，本脚本负责清干净后只加载 noetic
#
# 用法（每次开新终端跑 ROS1 命令前必须先 source）：
#     source /home/test/scan_planner/scripts/env.sh
# 复原：
#     source /home/test/scan_planner/scripts/env_restore.sh
#
# 设计要点:
#   1) 改动任何环境变量之前, 先把原值快照到 logs/env_backup_<时间戳>_<pid>.sh
#      （含"该变量原本未设置"的记录）, 复原时精确还原。
#   2) 同一 shell 内重复 source 不会覆盖快照, 否则第二次会把 noetic 的值
#      误当成"原值"存起来, 复原就失效了。
# ============================================================
WORKSPACE=/home/test/scan_planner
BKDIR=$WORKSPACE/logs
mkdir -p "$BKDIR" 2>/dev/null

# 需要备份 / 复原的环境变量
_BACKUP_VARS=(
  PATH PYTHONPATH LD_LIBRARY_PATH PKG_CONFIG_PATH CMAKE_PREFIX_PATH
  ROS_MASTER_URI ROS_HOSTNAME ROS_IP
  ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION
  ROS_ROOT ROS_ETC_DIR ROS_PACKAGE_PATH
  AMENT_PREFIX_PATH COLCON_PREFIX_PATH
  ROS_DOMAIN_ID RMW_IMPLEMENTATION
)

# ---------- 1. 快照原环境 ----------
if [ -z "$SCAN_PLANNER_ENV_BACKUP" ]; then
  BK="$BKDIR/env_backup_$(date +%m%d_%H%M%S)_$$.sh"
  : > "$BK"
  for v in "${_BACKUP_VARS[@]}"; do
    if [ -n "${!v+x}" ]; then
      printf 'export %s=%q\n' "$v" "${!v}" >> "$BK"      # 原值
    else
      printf 'unset %s\n' "$v" >> "$BK"                   # 原本就没设
    fi
  done
  cp -f "$BK" "$BKDIR/env_origin.sh"                      # 最新一份, 方便复原脚本直接取
  export SCAN_PLANNER_ENV_BACKUP="$BK"
  echo "[env] 原环境已快照 -> ${BK#$WORKSPACE/}"
else
  echo "[env] 已在本 shell 快照过, 复用: ${SCAN_PLANNER_ENV_BACKUP#$WORKSPACE/}"
fi

# ---------- 2. 存档外部 master 地址 ----------
# ⚠️ /opt/ros/noetic/setup.bash 一 source 就会把 ROS_MASTER_URI 重置成
#    http://localhost:11311, 所以必须在 source 之前取, 否则永远覆盖不掉。
_PRE_ROS_MASTER_URI=${ROS_MASTER_URI:-}
_PRE_ROS_HOSTNAME=${ROS_HOSTNAME:-}

# ---------- 3. 清掉 ROS2 痕迹 ----------
unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH ROS_VERSION ROS_PYTHON_VERSION
unset ROS_DISTRO

if [ -n "$PYTHONPATH" ]; then
  export PYTHONPATH=$(echo "$PYTHONPATH" | tr ':' '\n' \
    | grep -v '/opt/ros/foxy' \
    | grep -v 'lite_cog_ros2' \
    | paste -sd:)
fi

# ---------- 4. 加载 ROS1 ----------
# shellcheck disable=SC1091
source /opt/ros/noetic/setup.bash

if [ -f "$WORKSPACE/devel/setup.bash" ]; then
  # shellcheck disable=SC1091
  source "$WORKSPACE/devel/setup.bash"
fi

# ---------- 5. master 地址对齐 ----------
# ⚠️ 必须与 start_slam.sh 那侧一致, 否则两边各连各的 master。
#    ~/.bashrc 第 124 行是 http://192.168.1.103:11311, 这里沿用同一个值;
#    若进入本脚本前外部已设过(带 .bashrc 的终端), 则尊重外部值。
export ROS_MASTER_URI=${_PRE_ROS_MASTER_URI:-http://192.168.1.103:11311}
export ROS_HOSTNAME=${_PRE_ROS_HOSTNAME:-192.168.1.103}
# ROS_IP 与 ROS_HOSTNAME 同时存在时以后者为准, 为避免歧义这里清掉
unset ROS_IP

echo "[env] ROS_DISTRO=$ROS_DISTRO  AMENT=[${AMENT_PREFIX_PATH:-empty}]"
echo "[env] ROS_MASTER_URI=$ROS_MASTER_URI  ROS_HOSTNAME=$ROS_HOSTNAME"
