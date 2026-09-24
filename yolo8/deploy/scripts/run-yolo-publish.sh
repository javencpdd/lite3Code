#!/usr/bin/env bash
# ============================================================
# YOLO 发布本体：唯一的启动执行体
# ============================================================
# systemd unit（ExecStart）与 lite3-publish-ctl.sh（手工模式）共用本脚本，
# 保证两种拉起方式行为完全一致。
#
# 职责：加载 ros2 环境 → 读配置 → 由 YOLO8_PUBLISH_MODE 推导开关 → exec 成主进程。
# 注意用 exec：让 python 直接接管本进程 PID，便于 systemd/PID 文件管理。
# ============================================================
set -euo pipefail

APP_DIR=/home/test/yolo8
ENV_FILE=$APP_DIR/deploy/yolo-publish.env

# ROS2 Foxy 环境（systemd/非交互这里必须显式 source，不能依赖 ~/.bashrc）
# 注意：setup.bash 内部会读未定义变量（AMENT_TRACE_SETUP_FILES），
# 在 set -u 下会直接中断 → 临时关掉 nounset 再 source。
set +u
# shellcheck disable=SC1091
source /opt/ros/foxy/setup.bash
set -u

# 载入配置（重复注入无害：systemd 已注入时这里只是再读一遍）
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# 由 MODE 推导两个开关，避免"模式与开关不一致"的隐性配置错误
case "${YOLO8_PUBLISH_MODE:-rtmp}" in
  rtmp) YOLO8_ENABLE_RTMP=1; YOLO8_ENABLE_ROS2_IMAGE=0 ;;
  dual) YOLO8_ENABLE_RTMP=1; YOLO8_ENABLE_ROS2_IMAGE=1 ;;
  ros2) YOLO8_ENABLE_RTMP=0; YOLO8_ENABLE_ROS2_IMAGE=1 ;;
  *)
    echo "[run-yolo-publish] 未知 YOLO8_PUBLISH_MODE=$YOLO8_PUBLISH_MODE（可选 rtmp|dual|ros2）" >&2
    exit 2
    ;;
esac
export YOLO8_PUBLISH_MODE YOLO8_ENABLE_RTMP YOLO8_ENABLE_ROS2_IMAGE HEADLESS

echo "[run-yolo-publish] mode=$YOLO8_PUBLISH_MODE rtmp=$YOLO8_ENABLE_RTMP ros2=$YOLO8_ENABLE_ROS2_IMAGE url=$YOLO8_RTMP_URL"

mkdir -p "$APP_DIR/logs"
cd "$APP_DIR/src"
exec python3 -u run_tracker_publish.py
