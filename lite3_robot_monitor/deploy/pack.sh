#!/usr/bin/env bash
#
# 在**笔记本**上生成精简部署包。
#
# 为什么需要它：项目里 frontend/node_modules 有几百 MB，
# 整个目录往 103 传既慢又没必要 —— 103 只需要后端和已构建好的前端产物。
#
# 用法（Git Bash / WSL / Linux）：
#   bash deploy/pack.sh                      # 生成 lite3-monitor-deploy.tar.gz
#   bash deploy/pack.sh /tmp/monitor.tgz     # 指定输出路径
#
set -euo pipefail

OUT="${1:-lite3-monitor-deploy.tar.gz}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
PKG="$TMP_DIR/lite3_robot_monitor"

mkdir -p "$PKG"

echo "[pack] 收集文件 ..."
cp -r "$SRC_DIR/backend" "$PKG/"
cp -r "$SRC_DIR/deploy"  "$PKG/"
# tools/ 里有 mock_sender / smoke_test / 抓包分析工具，在 103 上排障很实用，一并带上
[[ -d "$SRC_DIR/tools" ]] && cp -r "$SRC_DIR/tools" "$PKG/"
[[ -f "$SRC_DIR/requirements.txt" ]] && cp "$SRC_DIR/requirements.txt" "$PKG/"
[[ -f "$SRC_DIR/README.md" ]] && cp "$SRC_DIR/README.md" "$PKG/"

if [[ -d "$SRC_DIR/frontend/dist" ]]; then
    mkdir -p "$PKG/frontend"
    cp -r "$SRC_DIR/frontend/dist" "$PKG/frontend/"
    echo "[pack] 已包含前端构建产物 frontend/dist"
else
    echo "[pack] 警告：未发现 frontend/dist（103 上将只提供 API）"
    echo "        建议先执行：cd frontend && npm run build"
fi

# 清理本地缓存与编译产物，减小体积
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -type d -name ".venv" -prune -exec rm -rf {} + 2>/dev/null || true

tar -czf "$OUT" -C "$TMP_DIR" lite3_robot_monitor
rm -rf "$TMP_DIR"

SIZE="$(du -h "$OUT" 2>/dev/null | awk '{print $1}')"
cat <<EOF

$(echo -e '\033[1;32m')打包完成$(echo -e '\033[0m')  $OUT  ($SIZE)

上传到 103：
  scp "$OUT" ysc@192.168.1.103:/tmp/

在 103 上部署：
  ssh ysc@192.168.1.103
  cd /tmp && tar xzf $(basename "$OUT")
  sudo bash lite3_robot_monitor/deploy/install.sh /home/test/monitor
EOF
