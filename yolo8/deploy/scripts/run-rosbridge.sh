#!/usr/bin/env bash
# ============================================================
# rosbridge 本体：Foxglove 通路的执行体（dual 模式才需要）
# ============================================================
# 只有走 ROS2 图像话题时才用得上：
#   Foxglove Studio → ws://<103IP>:9090 → rosbridge → /yolo/image_annotated
# ============================================================
set -euo pipefail

APP_DIR=/home/test/yolo8
ENV_FILE=$APP_DIR/deploy/yolo-publish.env

# ROS2 setup.bash 会引用未定义变量（AMENT_TRACE_SETUP_FILES），
# 在 set -u 下会中断 → 临时关掉 nounset 再 source。
set +u
# shellcheck disable=SC1091
source /opt/ros/foxy/setup.bash
set -u

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

PORT=${ROSBRIDGE_PORT:-9090}
echo "[run-rosbridge] port=$PORT"

mkdir -p "$APP_DIR/logs"
exec ros2 run rosbridge_server rosbridge_websocket --port "$PORT"
