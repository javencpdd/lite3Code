#!/bin/bash
# ============================================================
# 复原 env.sh 修改前的环境变量
# 用法: source /home/test/scan_planner/scripts/env_restore.sh
#       （必须 source, 不能 bash 执行, 否则改不到当前 shell）
# 也可指定某一份快照: source scripts/env_restore.sh logs/env_backup_0923_023000_1234.sh
# 查看有哪些快照:   ls -lt /home/test/scan_planner/logs/env_backup_*.sh
# ============================================================
WORKSPACE=/home/test/scan_planner

F=${1:-${SCAN_PLANNER_ENV_BACKUP:-$WORKSPACE/logs/env_origin.sh}}

if [ ! -f "$F" ]; then
  echo "[restore] ❌ 找不到快照文件: $F"
  exit 1
fi

# shellcheck disable=SC1090
source "$F"

# 复原后本标记已无意义, 让下一次 source env.sh 重新快照
unset SCAN_PLANNER_ENV_BACKUP

echo "[restore] ✅ 已复原: $(basename "$F")"
echo "[restore] ROS_DISTRO=${ROS_DISTRO:-未设置}"
echo "[restore] ROS_MASTER_URI=${ROS_MASTER_URI:-未设置}"
echo "[restore] AMENT_PREFIX_PATH=${AMENT_PREFIX_PATH:-未设置}"
