#!/bin/bash
# ============================================================
# 一键启动: SCAN-Planner 真机模式（不接机器人, 只出 /scan_planner/cmd_vel）
# 默认已带 need_extrinsic:=false（否则会叠 Go2 的雷达外参）
# 输出被强制 remap 到 /scan_planner/cmd_vel, 不会碰机器人正在消费的 /cmd_vel
#
# 用法: bash /home/test/scan_planner/scripts/start_scan_real.sh
# 覆盖参数: bash scripts/start_scan_real.sh max_vel:=1.0 navi_mode:=1
# ============================================================
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh || exit 1

M_HOST=$(echo "$ROS_MASTER_URI" | sed -E 's#^http://##; s#:.*##')
M_PORT=$(echo "$ROS_MASTER_URI" | sed -E 's#.*:##; s#/.*##')
master_up() { timeout 2 bash -c "</dev/tcp/${M_HOST}/${M_PORT}" 2>/dev/null; }

echo "[scan] master=$ROS_MASTER_URI (${M_HOST}:${M_PORT})"
for i in $(seq 1 20); do
  master_up && break
  sleep 1
done
if ! master_up; then
  echo "[scan] ❌ 连不上 ROS master ($ROS_MASTER_URI)。请先启动 start_slam.sh。"
  exit 1
fi

echo "[scan] 检查 /LIO/* 是否就绪:"
rostopic list 2>/dev/null | grep -E "^/LIO/" | sed 's/^/        /' \
  || echo "        ⚠️ 没有 /LIO/*，先在另一个终端跑 scripts/start_lio_relay.sh"

exec roslaunch scan_planner run.launch \
  is_real_world:=true navi_mode:=1 sensor_type:=lidar need_extrinsic:=false "$@"
