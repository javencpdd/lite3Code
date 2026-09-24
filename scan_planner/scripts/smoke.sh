#!/bin/bash
# 仿真冒烟自检：话题存在性 + 频率 + 目标点下发后 /cmd_vel 是否非零
# 用法: bash scripts/smoke.sh
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh >/dev/null || exit 1

echo "=== 1. 节点 ==="
rosnode list 2>/dev/null | sed 's/^/  /'

echo "=== 2. 关键话题 ==="
for t in /pcl_render_node/cloud /map_generator/global_cloud /quad_0/body_pose \
         /planning/bspline /planning/data_display /cmd_vel; do
  if rostopic list 2>/dev/null | grep -qx "$t"; then
    echo "  [OK]   $t"
  else
    echo "  [MISS] $t"
  fi
done

echo "=== 3. 点云频率 ==="
timeout 8 rostopic hz /pcl_render_node/cloud 2>/dev/null | head -n 3 | sed 's/^/  /'

echo "=== 4. 下发目标点并观察 /cmd_vel ==="
bash scripts/goal.sh 5.0 0.0 0.3
echo "  --- /cmd_vel 采样（8s）---"
timeout 8 rostopic echo /cmd_vel 2>/dev/null | grep -E "linear|angular" | head -n 12 | sed 's/^/  /'

echo "=== 5. 轨迹话题 ==="
timeout 6 rostopic echo /planning/bspline 2>/dev/null | head -n 8 | sed 's/^/  /'

echo "=== 6. 规划器日志错误 ==="
L=$(ls -t logs/sim_*.log 2>/dev/null | head -n 1)
if [ -n "$L" ]; then
  echo "  log=$L"
  grep -iE "error|fail|abort" "$L" | head -n 10 | sed 's/^/  /' || echo "  无 error/fail"
fi
