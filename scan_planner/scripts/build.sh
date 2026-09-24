#!/bin/bash
# SCAN-Planner 编译封装：-j2 保守并发（Xavier NX 6.7G 内存，PCL/OpenCV 头文件重）
set -o pipefail
cd /home/test/scan_planner || exit 1
# shellcheck disable=SC1091
source scripts/env.sh || exit 1

LOG=logs/build_$(date +%m%d_%H%M).log
echo "[build] 开始编译，日志: $LOG"
catkin_make -j2 -DCMAKE_BUILD_TYPE=Release 2>&1 | tee "$LOG"
code=$?
echo "[build] 退出码=$code"
tail -n 5 "$LOG"
exit $code
