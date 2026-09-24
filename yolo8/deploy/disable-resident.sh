#!/usr/bin/env bash
# ============================================================
# 关闭常驻（取消开机自启 + 立即停止）
# ============================================================
# 用法：sudo ./disable-resident.sh [all|yolo|rosbridge]
#   默认 all。
# 只取消自启与停服，unit 文件保留 —— 之后 ./enable-resident.sh 可随时再开。
# 要彻底移除 unit 用：sudo ./lite3-publish-ctl.sh uninstall
# ============================================================
set -euo pipefail

SELF=$(readlink -f "$0")
[ "$(id -u)" -eq 0 ] || exec sudo "$SELF" "$@"

cd "$(dirname "$SELF")"

exec ./lite3-publish-ctl.sh disable "${1:-all}"
