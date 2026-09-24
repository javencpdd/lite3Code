#!/usr/bin/env bash
# ============================================================
# Lite3 YOLO 视频发布 —— 统一控制入口（所有操作幂等）
# ============================================================
#
#   ./lite3-publish-ctl.sh start   <yolo|rosbridge|all>
#   ./lite3-publish-ctl.sh stop    <yolo|rosbridge|all>
#   ./lite3-publish-ctl.sh restart <yolo|rosbridge|all>
#   ./lite3-publish-ctl.sh status  <yolo|rosbridge|all>
#   ./lite3-publish-ctl.sh logs    <yolo|rosbridge> [-f]
#
#   ./lite3-publish-ctl.sh mode [rtmp|dual|ros2]        # 查看/切换单路双路
#
#   sudo ./lite3-publish-ctl.sh install                 # 装 systemd unit（不启用、不启动）
#   sudo ./lite3-publish-ctl.sh uninstall               # 卸载 unit（先停并取消自启）
#   sudo ./lite3-publish-ctl.sh enable   <yolo|rosbridge|all>   # 开机自启 + 立即启动
#   sudo ./lite3-publish-ctl.sh disable  <yolo|rosbridge|all>   # 停服务 + 取消自启
#
# 两条铁律：
#   1) 真正的启动逻辑只有 scripts/run-*.sh 一份，systemd 与手工轮流共用，行为一致。
#   2) unit 一旦 install 到 /etc/systemd/system，生命周期就交给 systemd，
#      本脚本的 start/stop/status 会自动改走 systemctl，避免"两套管理打架"。
#
# 默认策略：RTMP 单路（live/lite3_yolo）；systemd 常驻与 rosbridge 均不启用。
# ============================================================
set -euo pipefail

readonly APP_DIR=/home/test/yolo8
readonly DEPLOY_DIR=$APP_DIR/deploy
readonly LOG_DIR=$APP_DIR/logs
readonly ENV_FILE=$DEPLOY_DIR/yolo-publish.env
readonly UNIT_SRC=$DEPLOY_DIR/systemd
readonly UNIT_DST=/etc/systemd/system

readonly SELF=$(readlink -f "$0")
readonly TARGETS=(yolo rosbridge)

# ---------------- 目标 -> 属性 ----------------
unit_for()   { case "$1" in yolo) echo lite3-yolo-publish.service ;; rosbridge) echo lite3-yolo-rosbridge.service ;; esac; }
script_for() { case "$1" in yolo) echo "$DEPLOY_DIR/scripts/run-yolo-publish.sh" ;; rosbridge) echo "$DEPLOY_DIR/scripts/run-rosbridge.sh" ;; esac; }
log_for()    { case "$1" in yolo) echo "$LOG_DIR/yolo-publish.log" ;; rosbridge) echo "$LOG_DIR/rosbridge.log" ;; esac; }
pid_file()   { echo "$LOG_DIR/$1.pid"; }
installed()  { [ -f "$UNIT_DST/$(unit_for "$1")" ]; }

# 只有"已安装 **且** enabled"的目标才真正归 systemd 管
# （unit 装了但没启用时仍然走进程直管，这样手工启停不需要 root）
uses_systemd() {
  installed "$1" || return 1
  systemctl is-enabled "$(unit_for "$1")" >/dev/null 2>&1
}

# 目标列表里是否有任何一个归 systemd 管（决定要不要提权）
any_systemd() {
  local t list
  list=$(targets_of "$1") || exit $?
  for t in $list; do
    uses_systemd "$t" && return 0
  done
  return 1
}

targets_of() {
  case "$1" in
    all) echo "${TARGETS[@]}" ;;
    yolo | rosbridge) echo "$1" ;;
    *) echo "未知目标 '$1'（可选 yolo|rosbridge|all）" >&2; exit 2 ;;
  esac
}

ensure_root() {
  [ "$(id -u)" -eq 0 ] && return 0
  echo "该操作需要 root 权限。" >&2
  # 有终端 → 直接提权让用户输入密码；无终端（ssh 批量/CI）→ 只能依赖 NOPASSWD
  if [ -t 0 ]; then
    exec sudo "$SELF" "$@"
  fi
  if sudo -n true 2>/dev/null; then
    exec sudo -n "$SELF" "$@"
  fi
  echo "非交互环境无法提权，请显式执行：sudo $SELF $*" >&2
  exit 1
}

# ---------------- 进程直管（未安装 unit 时） ----------------
running() {
  local f p
  f=$(pid_file "$1")
  [ -f "$f" ] || return 1
  p=$(cat "$f")
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

start_proc() {
  local t=$1 script log f p
  script=$(script_for "$t"); log=$(log_for "$t"); f=$(pid_file "$t")
  if running "$t"; then
    echo "[$t] 已在运行 PID=$(cat "$f")（幂等，跳过启动）"
    return 0
  fi
  mkdir -p "$LOG_DIR"
  setsid nohup "$script" >>"$log" 2>&1 </dev/null &
  p=$!
  echo "$p" >"$f"
  sleep 2
  if running "$t"; then
    echo "[$t] 已启动 PID=$p，日志 -> $log"
  else
    echo "[$t] 启动失败，看日志：$log" >&2
    rm -f "$f"
    return 1
  fi
}

stop_proc() {
  local t=$1 f p pgid
  f=$(pid_file "$t")
  if ! running "$t"; then
    echo "[$t] 未运行（幂等，无需停止）"
    rm -f "$f"
    return 0
  fi
  p=$(cat "$f")
  # setsid 起的是新会话，按进程组一起收，避免留下 ros2 run 的子进程
  pgid=$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ') || pgid=""
  if [ -n "${pgid:-}" ]; then
    kill -TERM "-$pgid" 2>/dev/null || true
  else
    kill -TERM "$p" 2>/dev/null || true
  fi
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$p" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$p" 2>/dev/null; then
    kill -KILL "$p" 2>/dev/null || true
  fi
  rm -f "$f"
  echo "[$t] 已停止"
}

# ---------------- 统一动作分发 ----------------
do_action() {
  local action=$1 unit list
  local t
  # 只有 enabled 的 unit 才归 systemd 管，此时 systemctl 需要 root → 先确保有权限。
  # status 只读，不需要提权。
  if [ "$action" != "status" ] && any_systemd "$2"; then
    ensure_root "$@"
  fi
  # 先取值再循环：targets_of 里的 exit 若发生在命令替换子 shell 中，外层收不到退出码
  list=$(targets_of "$2") || exit $?
  for t in $list; do
    unit=$(unit_for "$t")
    if uses_systemd "$t"; then
      case "$action" in
        start)   systemctl start "$unit"   && echo "[$t] systemctl start ok" ;;
        stop)    systemctl stop "$unit"    && echo "[$t] systemctl stop ok" ;;
        restart) systemctl restart "$unit" && echo "[$t] systemctl restart ok" ;;
        status)  show_status "$t" ;;
      esac
    else
      case "$action" in
        start)   start_proc "$t" ;;
        stop)    stop_proc "$t" ;;
        restart) stop_proc "$t"; start_proc "$t" ;;
        status)  show_status "$t" ;;
      esac
    fi
  done
}

show_status() {
  local t=$1 unit f
  unit=$(unit_for "$t"); f=$(pid_file "$t")
  if uses_systemd "$t"; then
    echo "[$t] unit=$unit 已启用（systemd 托管）"
    systemctl show "$unit" -p ActiveState -p SubState -p MainPID -p NRestarts --no-pager 2>/dev/null |
      sed 's/^/     /' || true
  elif installed "$t"; then
    local enabled
    enabled=$(systemctl is-enabled "$unit" 2>/dev/null) || enabled=disabled
    echo "[$t] unit=$unit 已安装但未启用（enabled=$enabled）"
    process_line "$t"
  elif running "$t"; then
    echo "[$t] unit=未安装，进程直管"
    process_line "$t"
  else
    echo "[$t] unit=未安装，进程未运行"
  fi
  tail_log "$(log_for "$t")" -n 3
}

process_line() {
  local t=$1 f
  f=$(pid_file "$t")
  if running "$t"; then
    echo "     进程 PID=$(cat "$f") uptime=$(ps -o etime= -p "$(cat "$f")" | tr -d ' ')"
  else
    echo "     进程未运行"
  fi
}

# 注意：本脚本开了 set -o pipefail，日志不存在时 tail 返回 1 会被 set -e 当成致命错误中断脚本，
# 因此日志相关操作统一走这里兜底。
tail_log() {
  local log=$1; shift
  [ -f "$log" ] || return 0
  tail "$@" "$log" 2>/dev/null | sed 's/^/     | /' || true
}

# ---------------- 子命令 ----------------
cmd_mode() {
  local new=${1:-}
  if [ -z "$new" ]; then
    grep -E '^YOLO8_PUBLISH_MODE=' "$ENV_FILE" || true
    return 0
  fi
  case "$new" in
    rtmp | dual | ros2) ;;
    *) echo "模式只能为 rtmp|dual|ros2" >&2; exit 2 ;;
  esac
  sed -i "s/^YOLO8_PUBLISH_MODE=.*/YOLO8_PUBLISH_MODE=$new/" "$ENV_FILE"
  echo "已切换 -> YOLO8_PUBLISH_MODE=$new"
  echo "生效：./lite3-publish-ctl.sh restart yolo"
  [ "$new" = "rtmp" ] || echo "提示：$new 模式还需 ./lite3-publish-ctl.sh start rosbridge"
}

cmd_logs() {
  local t=$1; shift || true
  tail "${@:--n 30}" "$(log_for "$t")" 2>/dev/null || echo "无日志：$(log_for "$t")"
}

cmd_install() {
  ensure_root "$@"
  for t in "${TARGETS[@]}"; do
    install -m 0644 "$UNIT_SRC/$(unit_for "$t")" "$UNIT_DST/$(unit_for "$t")"
  done
  systemctl daemon-reload
  echo "unit 已安装到 $UNIT_DST（按默认策略：未启用、未启动）"
  echo "开启常驻：sudo ./lite3-publish-ctl.sh enable yolo"
}

cmd_uninstall() {
  ensure_root "$@"
  for t in "${TARGETS[@]}"; do
    local unit
    unit=$(unit_for "$t")
    if [ -f "$UNIT_DST/$unit" ]; then
      systemctl stop "$unit" 2>/dev/null || true
      systemctl disable "$unit" 2>/dev/null || true
      rm -f "$UNIT_DST/$unit"
      echo "[$t] unit 已卸载"
    else
      echo "[$t] unit 不存在（幂等，跳过）"
    fi
  done
  systemctl daemon-reload
}

cmd_switch() { # enable|disable <target>
  ensure_root "$@"
  local act=$1 t unit list
  list=$(targets_of "$2") || exit $?
  for t in $list; do
    unit=$(unit_for "$t")
    if ! installed "$t"; then
      echo "[$t] unit 未安装，先跑：sudo $0 install" >&2
      exit 1
    fi
    systemctl "$act" --now "$unit"
    echo "[$t] $act 完成（$(systemctl is-enabled "$unit")）"
  done
}

usage() { sed -n '2,26p' "$SELF" | sed 's/^# \{0,1\}//'; }

main() {
  [ $# -ge 1 ] || { usage; exit 2; }
  local cmd=$1; shift
  case "$cmd" in
    start | stop | restart | status)
      [ $# -ge 1 ] || { echo "用法：$0 $cmd <yolo|rosbridge|all>" >&2; exit 2; }
      do_action "$cmd" "$1"
      ;;
    logs)
      [ $# -ge 1 ] || { echo "用法：$0 logs <yolo|rosbridge> [-f]" >&2; exit 2; }
      cmd_logs "$@"
      ;;
    mode)
      cmd_mode "${1:-}"
      ;;
    install | uninstall)
      "cmd_$cmd" "$@"
      ;;
    enable | disable)
      [ $# -ge 1 ] || { echo "用法：sudo $0 $cmd <yolo|rosbridge|all>" >&2; exit 2; }
      cmd_switch "$cmd" "$1"
      ;;
    help | -h | --help)
      usage
      ;;
    *)
      echo "未知命令 '$cmd'" >&2
      usage
      exit 2
      ;;
  esac
}

main "$@"
