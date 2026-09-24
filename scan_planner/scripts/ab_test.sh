#!/bin/bash
# ============================================================
# Lite3 参数 A/B 回归测试驱动
# 用法: bash scripts/ab_test.sh <tag> <radius> <offset> <vy> [delay]
#   tag    : 结果标记, 日志写入 logs/ab_<tag>.log
#   radius : grid_map/double_cylinder_radius
#   offset : grid_map/double_cylinder_offset
#   vy     : closed_loop_controller/max_vy
#   delay  : 启动后等待多少秒再发目标点, 默认 25
#
# 前置条件(保证可重复):
#   - mockamap seed=127 固定, 地图每次一致
#   - 起点固定 (-19, 1, 0.25), 目标点固定 (5, 0, 0.3)
#   - max_vel 由 launch 文件当前值决定, 本脚本不修改
# 流程: 清理旧节点 -> 改写 3 个参数 -> 启动仿真 -> 发目标 -> 等待 -> 统计
# 注意: 统计期间严禁并行跑 rostopic hz / echo, 否则抢占 Jetson CPU 造成伪故障
# ============================================================
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh || exit 1

TAG=$1; R=$2; O=$3; VY=$4; DELAY=${5:-25}
if [ -z "$TAG" ]; then echo "usage: $0 <tag> <radius> <offset> <vy> [delay]"; exit 1; fi

F=src/planner/plan_manage/launch/advanced_param.xml
LOG=logs/ab_${TAG}.log

# ---------- 1. 清理上一轮 ROS 进程 ----------
# 模式内嵌 [] 避免 pkill 匹配到本脚本自身/外层 shell 命令行
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
sleep 4

# ---------- 2. 写入本轮参数 ----------
sed -i "s|<param name=\"grid_map/double_cylinder_radius\" value=\"[0-9.]*\"|<param name=\"grid_map/double_cylinder_radius\" value=\"${R}\"|" "$F"
sed -i "s|<param name=\"grid_map/double_cylinder_offset\" value=\"[0-9.]*\"|<param name=\"grid_map/double_cylinder_offset\" value=\"${O}\"|" "$F"
sed -i "s|<param name=\"closed_loop_controller/max_vy\" value=\"[0-9.]*\"|<param name=\"closed_loop_controller/max_vy\" value=\"${VY}\"|" "$F"

echo "============================================================"
echo "[AB] tag=$TAG  radius=$R  offset=$O  max_vy=$VY  发目标延迟=${DELAY}s"
echo "[AB] max_vel(launch 现值): $(grep -o 'max_vel" default="[0-9.]*"' "$F")"
echo "[AB] 写入后校验:"
grep -E "double_cylinder_(radius|offset)|closed_loop_controller/max_vy" "$F" | sed 's/^/      /'
echo "============================================================"

# ---------- 3. 启动仿真 ----------
roslaunch scan_planner run.launch is_real_world:=false navi_mode:=1 sensor_type:=lidar > "$LOG" 2>&1 &
sleep "$DELAY"
echo "[AB] 节点数: $(rosnode list 2>/dev/null | wc -l)"

# ---------- 4. 发布目标点 (5, 0, 0.3) ----------
rostopic pub /move_base_simple/goal geometry_msgs/PoseStamped \
  '{header: {frame_id: "world"}, pose: {position: {x: 5.0, y: 0.0, z: 0.3},
    orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}}}' -1 > /dev/null 2>&1
echo "[AB] 目标点已发布, 等待 90s ..."
sleep 90

# ---------- 5. 采集结果 ----------
END_POS=$(timeout 5 rostopic echo -n 1 /odom_visualization/pose 2>/dev/null \
          | grep -E "^\s+(x|y|z):" | tr -d ' \n' | head -c 120)
FSM=$(grep "\[FSM\]" "$LOG" | tail -1)

echo "---------------------------------------------"
echo "[AB] 结果 tag=$TAG"
echo "      终态位姿 : $END_POS"
echo "      最后状态 : $FSM"
printf "      %-24s %s\n" "a star error"                "$(grep -c 'a star error' "$LOG")"
printf "      %-24s %s\n" "drone is in obstacle"        "$(grep -c 'drone is in obstacle' "$LOG")"
printf "      %-24s %s\n" "terminal point in obs"       "$(grep -c 'terminal point' "$LOG")"
printf "      %-24s %s\n" "EMERGENCY_STOP 次数"          "$(grep -c 'EMERGENCY_STOP' "$LOG")"
printf "      %-24s %s\n" "Replan failed 1000"          "$(grep -c 'Replan failed 1000' "$LOG")"
printf "      %-24s %s\n" "日志行数"                     "$(wc -l < "$LOG")"
echo "---------------------------------------------"
echo "[AB] tag=$TAG done"
