#!/usr/bin/env bash
# ============================================================
# 启动（手工模式：只起进程，不写 systemd、不开机自启）
# ============================================================
# 用法：./start.sh [yolo|rosbridge|all]
#   默认只起 yolo。
#   若 yolo-publish.env 里 YOLO8_PUBLISH_MODE=dual|ros2，会自动连同 rosbridge 一起起，
#   免得双路模式下漏起 rosbridge 导致 Foxglove 连不上。
# ============================================================
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

target=${1:-yolo}
mode=$(grep -E '^YOLO8_PUBLISH_MODE=' yolo-publish.env | cut -d= -f2)

if [ "$target" = "yolo" ] && [ "$mode" != "rtmp" ]; then
  echo "YOLO8_PUBLISH_MODE=$mode，需要 rosbridge，一并启动"
  target=all
fi

exec ./lite3-publish-ctl.sh start "$target"
