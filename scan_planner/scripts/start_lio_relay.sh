#!/bin/bash
# ============================================================
# 一键启动: FAST-LIO 话题适配 relay
# 把 /Odometry、/cloud_registered_body 补成 SCAN-Planner 需要的
# /LIO/odom_vehicle、/LIO/odom_imu、/LIO/clouds_lidar
#
# 用法: bash /home/test/scan_planner/scripts/start_lio_relay.sh
# 前提: 已用 start_slam.sh 启动 SLAM（roscore 与 laserMapping 在跑）
# ------------------------------------------------------------
# 注意: 必须在脚本内部 source env.sh。
#       103 的 ~/.bashrc 默认加载 ROS2 foxy, 而 foxy 里没有 roslaunch
#       （foxy 是 ros2 launch）, 直接敲 roslaunch 会 "command not found"。
# ============================================================
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh || exit 1

# 从 ROS_MASTER_URI 拆出 host/port, 用 TCP 探测而不是 rosnode list
# （rosnode list 在无 master 时会长时间阻塞, 不适合轮询）
M_HOST=$(echo "$ROS_MASTER_URI" | sed -E 's#^http://##; s#:.*##')
M_PORT=$(echo "$ROS_MASTER_URI" | sed -E 's#.*:##; s#/.*##')

master_up() { timeout 2 bash -c "</dev/tcp/${M_HOST}/${M_PORT}" 2>/dev/null; }

echo "[relay] 等待 master: $ROS_MASTER_URI (${M_HOST}:${M_PORT})"
for i in $(seq 1 20); do
  if master_up; then
    echo "[relay] master 已就绪"
    break
  fi
  sleep 1
done

if ! master_up; then
  echo "[relay] ❌ 连不上 ROS master ($ROS_MASTER_URI)。请先启动 start_slam.sh。"
  exit 1
fi

echo "[relay] 当前 master 上的 FAST-LIO 输出话题:"
rostopic list 2>/dev/null | grep -E "^/(Odometry|cloud_registered)" | sed 's/^/        /' \
  || echo "        ⚠️ 没找到 /Odometry 与 /cloud_registered_body，SLAM 起了吗？"

exec roslaunch scan_planner lio_relay.launch "$@"
