#!/bin/bash
# ============================================================
# 真机链路端到端冒烟（不需要真机 / 不需要雷达）
# 假 FAST-LIO -> lio_relay -> SCAN-Planner(is_real_world:=true)
# 校验点:
#   1. /LIO/* 三个话题在位
#   2. need_extrinsic 实际生效为 false（否则会叠 Go2 外参）
#   3. grid_map 真的吃到了点云（不再报 "no /grid_map/sensor_pose yet"）
#   4. 真机安全网: 只发 /scan_planner/cmd_vel, 不发裸 /cmd_vel
# ============================================================
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh || exit 1

# ---------- 清理 ----------
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

LOG=logs/e2e_$(date +%m%d_%H%M).log

roscore > logs/roscore_e2e.log 2>&1 &
sleep 6

python3 scripts/fake_lio.py > logs/fake_lio.log 2>&1 &
sleep 3

roslaunch scan_planner lio_relay.launch > logs/lio_relay.log 2>&1 &
sleep 5

roslaunch scan_planner run.launch \
  is_real_world:=true navi_mode:=1 sensor_type:=lidar need_extrinsic:=false > "$LOG" 2>&1 &
sleep 20

echo "=========================================="
echo "[e2e] 1) /LIO/* 话题"
rostopic list | grep -E "^/LIO/" | sed 's/^/  /'

echo
echo "[e2e] 2) 关键参数实取值"
for p in /scan_planner_node/grid_map/need_extrinsic \
         /scan_planner_node/grid_map/cloud_is_world \
         /scan_planner_node/grid_map/sensor_type \
         /scan_planner_node/grid_map/body_height \
         /scan_planner_node/grid_map/double_cylinder_radius; do
  printf "  %-52s %s\n" "$p" "$(rosparam get $p 2>&1)"
done

echo
echo "[e2e] 3) 点云是否被消费（应无 sensor_pose 缺失告警）"
N=$(grep -c "no /grid_map/sensor_pose yet" "$LOG")
echo "  'no /grid_map/sensor_pose yet' 次数: $N  (0 = 位姿已到位)"
echo "  /grid_map/occupancy 频率:"
timeout 8 rostopic hz /grid_map/occupancy 2>/dev/null | sed -n '2p' | sed 's/^/    /'

echo
echo "[e2e] 4) 真机安全网: cmd_vel 命名"
echo "  存在 /scan_planner/cmd_vel : $(rostopic list | grep -c '^/scan_planner/cmd_vel$')"
echo "  存在裸 /cmd_vel            : $(rostopic list | grep -c '^/cmd_vel$')  (真机应为 0)"

echo
echo "[e2e] 5) 节点/错误"
rosnode list | sed 's/^/  /'
echo "  日志 ERROR 条数: $(grep -c '\[ERROR\]' "$LOG")"
grep '\[ERROR\]' "$LOG" | head -3 | sed 's/^/    /'
echo "=========================================="
echo "[e2e] done  log=$LOG"
