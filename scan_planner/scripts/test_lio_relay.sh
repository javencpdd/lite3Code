#!/bin/bash
# ============================================================
# lio_relay 链路冒烟测试（不需要真机 / 不需要雷达）
# 假数据源(fake_lio.py) -> topic_tools relay -> /LIO/* 三话题
# 校验: 话题是否存在、频率是否正常、frame_id 与点数是否正确
# ============================================================
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh || exit 1

# ---------- 1. 清理上一轮 ROS1 进程（方括号避免 pkill 自杀）----------
pkill -9 -f 'rosmas[t]er'          2>/dev/null
pkill -9 -f 'roslau[n]ch'          2>/dev/null
pkill -9 -f 'rosou[t]'             2>/dev/null
pkill -9 -f 'scan_planne[r]_node'  2>/dev/null
pkill -9 -f 'pcl_rende[r]_node'    2>/dev/null
pkill -9 -f 'mockama[p]_node'      2>/dev/null
pkill -9 -f 'closed_loo[p]_controller' 2>/dev/null
pkill -9 -f 'go2_kinematic_si[m]'  2>/dev/null
pkill -9 -f 'go2_gait_publishe[r]' 2>/dev/null
pkill -9 -f 'odom_visualizatio[n]' 2>/dev/null
pkill -9 -f 'robot_state_publishe[r]' 2>/dev/null
pkill -9 -f 'fake_li[o].py'        2>/dev/null
pkill -9 -f 'topi[c]_tools'        2>/dev/null
sleep 4

# ---------- 2. 起 roscore ----------
roscore > logs/roscore_relay.log 2>&1 &
sleep 6
echo "[test] roscore: $(rostopic list 2>/dev/null | wc -l) 个话题(基线)"

# ---------- 3. 起假 FAST-LIO ----------
python3 scripts/fake_lio.py > logs/fake_lio.log 2>&1 &
sleep 4
echo "[test] 假数据源已启动"

# ---------- 4. 起 relay ----------
roslaunch scan_planner lio_relay.launch > logs/lio_relay.log 2>&1 &
sleep 8

echo "=========================================="
echo "[test] 1) /LIO/* 话题是否存在"
rostopic list | grep -E "^/LIO/" || echo "  ❌ 一个都没有"

echo
echo "[test] 2) 频率（各测 5s）"
for tp in /LIO/odom_vehicle /LIO/odom_imu /LIO/clouds_lidar; do
  printf "  %-22s " "$tp"
  timeout 6 rostopic hz "$tp" 2>/dev/null | sed -n '2p' | sed 's/^/  /' || echo "  ❌ 无数据"
done

echo
echo "[test] 3) 内容校验"
echo "  --- /LIO/odom_vehicle ---"
timeout 4 rostopic echo -n 1 /LIO/odom_vehicle 2>/dev/null \
  | grep -E "frame_id|child_frame_id|position" -A2 | head -12 | sed 's/^/  /'
echo "  --- /LIO/clouds_lidar ---"
timeout 4 rostopic echo -n 1 /LIO/clouds_lidar 2>/dev/null \
  | grep -E "frame_id|width:|height:|point_step:|row_step:" | head -6 | sed 's/^/  /'

echo
echo "[test] 4) relay 节点是否都活着"
rosnode list | grep relay | sed 's/^/  /'
echo "=========================================="
echo "[test] done"
