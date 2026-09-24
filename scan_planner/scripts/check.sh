#!/bin/bash
# SCAN-Planner 部署前依赖自检（ROS1 Noetic 视角）
fail=0
chk() { if [ "$1" = "ok" ]; then echo "  [OK]   $2"; else echo "  [MISS] $2"; fail=1; fi; }

echo "=== 系统 ==="
lsb_release -d | sed 's/^/  /'
echo "  arch=$(uname -m)  cores=$(nproc)"
free -g | head -n 2 | sed 's/^/  /'
df -h / | tail -n 1 | sed 's/^/  /'

echo "=== 编译器 ==="
gcc --version | head -n 1 | sed 's/^/  /'
cmake --version | head -n 1 | sed 's/^/  /'
echo | gcc -fopenmp -x c++ -c -o /dev/null - 2>/dev/null && echo "  [OK]   OpenMP" || echo "  [MISS] OpenMP"

echo "=== 系统库 ==="
for p in libeigen3-dev libpcl-dev libopencv-dev libarmadillo-dev; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok" \
    && echo "  [OK]   $p $(dpkg-query -W -f='${Version}' $p)" \
    || echo "  [MISS] $p"
done
ls /usr/share/cmake-*/Modules/FindArmadillo.cmake >/dev/null 2>&1 \
  && echo "  [OK]   FindArmadillo.cmake" || echo "  [MISS] FindArmadillo.cmake"
for c in filesystem iostreams program-options serialization system; do
  dpkg-query -W -f='${Status}' "libboost-$c-dev" 2>/dev/null | grep -q "install ok" \
    && echo "  [OK]   libboost-$c-dev" || echo "  [MISS] libboost-$c-dev"
done

echo "=== ROS1 组件 ==="
for p in roscpp rospy std_msgs geometry_msgs nav_msgs sensor_msgs visualization_msgs \
         tf message_filters message_generation cv_bridge pcl-ros pcl-conversions \
         cmake-modules roslaunch robot-state-publisher rviz; do
  dpkg-query -W -f='${Status}' "ros-noetic-$p" 2>/dev/null | grep -q "install ok" \
    && echo "  [OK]   ros-noetic-$p" || echo "  [MISS] ros-noetic-$p"
done

echo "=== 环境隔离 ==="
[ -z "$AMENT_PREFIX_PATH" ] && echo "  [OK]   AMENT_PREFIX_PATH 为空" || echo "  [WARN] AMENT_PREFIX_PATH=$AMENT_PREFIX_PATH"
[ "$ROS_DISTRO" = "noetic" ] && echo "  [OK]   ROS_DISTRO=noetic" || echo "  [WARN] ROS_DISTRO=${ROS_DISTRO:-unset}"

echo
[ $fail -eq 0 ] && echo "结论：依赖齐全，可以编译" || echo "结论：存在缺失项"
exit $fail
