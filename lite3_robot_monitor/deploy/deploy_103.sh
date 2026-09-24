#!/usr/bin/env bash
# 把本地修改后的后端 + 前端 dist 同步到 103 并重启服务。
#
# 前置条件：
#   1. 本机已配置到 103 的 SSH 免密（ssh-copy-id ysc@103）。
#   2. 103 上已存在基础目录 /home/test/monitor，且 lite3-monitor 服务可用。
#   3. 如需常驻 ros 桥接，请先把 deploy/lite3-ros-bridge.service 装到 103（见其文件头）。
#
# 用法：
#   LITE3_HOST=ysc@192.168.1.103 bash deploy/deploy_103.sh
#   （默认 HOST=ysc@192.168.1.103，可用环境变量覆盖；目录默认 /home/test/monitor）
#   并行部署第二套实例时：
#   LITE3_TAG=2 LITE3_REMOTE=/home/test/monitor2 LITE3_HOST=ysc@192.168.1.103 bash deploy/deploy_103.sh
#   ↑ 只重启 lite3-monitor2 / lite3-ros-bridge2，不影响正在运行的 lite3-monitor

set -euo pipefail

HOST="${LITE3_HOST:-ysc@192.168.1.103}"
REMOTE_DIR="${LITE3_REMOTE:-/home/test/monitor}"

# TAG：留空=单实例（lite3-monitor / 8000 / lite3-ros-bridge / 43900）；
#      填 2=并行第二套（lite3-monitor2 / 8002 / lite3-ros-bridge2 / 43902）。
# 注意：本脚本只同步文件并重启「同一 TAG 的实例」，绝不触碰旧实例。
TAG="${LITE3_TAG:-}"
SERVICE_NAME="lite3-monitor${TAG}"
ROS_BRIDGE_SVC="lite3-ros-bridge${TAG}"
HTTP_PORT=8000
if [[ -n "$TAG" ]]; then
    HTTP_PORT=$((8000 + 10#$TAG))
fi
# 仓库根目录（本脚本位于 <repo>/deploy/，故上级即仓库根）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> 目标主机: $HOST"
echo "==> 远程目录: $REMOTE_DIR"
echo "==> 本地目录: $LOCAL_DIR"

echo "==> [1/2] 同步后端（排除 .venv / __pycache__）"
rsync -avz --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  "$LOCAL_DIR/backend/" "$HOST:$REMOTE_DIR/backend/"

echo "==> [2/2] 同步前端静态资源 dist"
rsync -avz --delete \
  "$LOCAL_DIR/frontend/dist/" "$HOST:$REMOTE_DIR/frontend/dist/"

echo "==> 重启服务（仅 $SERVICE_NAME / $ROS_BRIDGE_SVC，不动旧实例）"
ssh "$HOST" "sudo systemctl restart $SERVICE_NAME 2>/dev/null; sudo systemctl restart $ROS_BRIDGE_SVC 2>/dev/null; sudo systemctl status $SERVICE_NAME $ROS_BRIDGE_SVC --no-pager || true"

echo "==> 完成。中控页面： http://<103-IP>:$HTTP_PORT/"
