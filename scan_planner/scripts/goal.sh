#!/bin/bash
# 下发导航目标点（navi_mode 1）
# 用法: bash scripts/goal.sh [x] [y] [z]
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh >/dev/null || exit 1

X=${1:-5.0}
Y=${2:-0.0}
Z=${3:-0.3}

echo "[goal] 目标点 ($X, $Y, $Z) -> /move_base_simple/goal"
rostopic pub -1 /move_base_simple/goal geometry_msgs/PoseStamped \
  "{header: {stamp: now, frame_id: world}, pose: {position: {x: $X, y: $Y, z: $Z}, orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}}}"
