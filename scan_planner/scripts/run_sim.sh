#!/bin/bash
# 用法: bash scripts/run_sim.sh [navi_mode] [sensor_type]
#   navi_mode:   1=RViz/话题目标点  2=关键点  3=参考路径跟踪
#   sensor_type: lidar | depth
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh || exit 1

MODE=${1:-1}
SENSOR=${2:-lidar}
LOG=logs/sim_mode${MODE}_${SENSOR}_$(date +%m%d_%H%M).log

echo "[sim] navi_mode=$MODE sensor_type=$SENSOR -> $LOG"
roslaunch scan_planner run.launch \
  is_real_world:=false navi_mode:=$MODE sensor_type:=$SENSOR 2>&1 | tee "$LOG"
