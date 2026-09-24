#!/usr/bin/env bash
# ============================================================
# 开启常驻（开机自启 + 立即启动）
# ============================================================
# 用法：sudo ./enable-resident.sh [yolo|rosbridge|all]
#   默认 yolo；若 YOLO8_PUBLISH_MODE=dual|ros2 会自动带上 rosbridge。
#
# 做了什么：
#   1. install：把 systemd unit 拷到 /etc/systemd/system（幂等，已装则覆盖同内容）
#   2. enable --now：设置开机自启并立即拉起
# 之后生命周期归 systemd，日常也可以用
#   ./lite3-publish-ctl.sh start|stop|restart|status <target>
# ============================================================
set -euo pipefail

SELF=$(readlink -f "$0")
# 整段脚本都要 root：先提权再执行，避免 install 提权后 enable 那段跑不到
[ "$(id -u)" -eq 0 ] || exec sudo "$SELF" "$@"

cd "$(dirname "$SELF")"

target=${1:-yolo}
mode=$(grep -E '^YOLO8_PUBLISH_MODE=' yolo-publish.env | cut -d= -f2)

if [ "$target" = "yolo" ] && [ "$mode" != "rtmp" ]; then
  echo "YOLO8_PUBLISH_MODE=$mode，rosbridge 也一并常驻"
  target=all
fi

./lite3-publish-ctl.sh install
exec ./lite3-publish-ctl.sh enable "$target"
