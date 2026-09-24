#!/usr/bin/env bash
#
# Lite3 Robot Monitor —— 103 感知导航主机一键部署脚本
#
# 目标机：Jetson Xavier NX（Ubuntu 20.04 / Python 3.8），IP 192.168.1.103
#
# 103 实测环境：用户 test，IP 192.168.1.103，Ubuntu 20.04 / Python 3.8
#
# 用法：
#   sudo bash deploy/install.sh                          # 部署到 /home/test/monitor（单实例）
#   sudo bash deploy/install.sh /home/test/monitor2 2    # 并行部署第二套：服务名/端口自动加后缀
#                                                        # 注意：TAG 就是第二个参数，不要再插第三个
#   sudo bash deploy/install.sh /home/test/monitor2 --tag 2
#   sudo bash deploy/install.sh /opt/lite3_monitor        # 指定目录
#   PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple sudo -E bash deploy/install.sh
#   ↑ 走内网 pip 源时用环境变量 PIP_INDEX 传入（pip 源不再支持位置参数）
#   LITE3_TAG=2 sudo -E bash deploy/install.sh /home/test/monitor2   # 也可用环境变量指定 TAG
#
# 说明：
#   1. 本脚本只做“安装与注册”，不会启动后立即占用 43897（默认走旁路抓包模式）；
#   2. 不会改动 transfer_ros2 / jy_exe / 任何现有服务；
#   3. 失败可随时回滚：见 deploy/README.md 的“回滚”章节。

set -euo pipefail

# ---------------------------------------------------------------- 辅助函数
log()  { echo -e "\033[1;32m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
err()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; }

usage() {
    cat <<'USAGE'
用法: sudo bash deploy/install.sh [安装目录] [TAG] [选项]

  install.sh /home/test/monitor           单实例（服务 lite3-monitor，HTTP 8000）
  install.sh /home/test/monitor2 2        第二套（服务 lite3-monitor2，HTTP 8002，桥接 43902）
  install.sh /home/test/monitor2 --tag 2  同上
  PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple install.sh /home/test/monitor

  安装目录        默认 /home/test/monitor
  TAG             非负整数；留空=单实例，填 N 则服务名加后缀 N、HTTP=8000+N、桥接=43900+N
                  注意：TAG 是第二个位置参数。历史版本把第二个参数当成 pip 源，
                  导致 `install.sh <dir> 2` 被解析成 `-i 2` 且 TAG 为空，请勿再那样传
  --tag N         等价于第二个位置参数 TAG
  --pip-index URL pip 源，等价于环境变量 PIP_INDEX
  --force         允许覆盖已存在、且指向其它目录的同名 systemd 单元
USAGE
}

# 安全护栏：防止 TAG 漏传/传错时，拿默认服务名去覆盖正在运行的旧实例。
# 若目标 unit 已存在，且它的 WorkingDirectory 不属于本次安装目录，直接中止。
check_unit_owner() {
    local unit="$1" want="$2" old
    [[ -f "$unit" ]] || return 0
    [[ "$FORCE" -eq 0 ]] || return 0
    old="$(sed -n 's/^WorkingDirectory=//p' "$unit" | head -n1)"
    [[ -n "$old" ]] || return 0
    [[ "$old" == "$want" ]] && return 0
    err "中止：$(basename "$unit") 已存在，且属于另一个安装目录："
    err "  已存在: $old"
    err "  本次将: $want"
    err "这通常是 TAG 漏传/传错导致的。并行部署第二套请把 TAG 作为第二个参数传入，例如："
    err "  sudo bash $0 $TARGET 2"
    err "若确实要覆盖该实例，请显式加 --force。"
    exit 1
}

# ---------------------------------------------------------------- 参数
TARGET_DEFAULT="/home/test/monitor"
PIP_INDEX_URL="${PIP_INDEX:-}"
TAG="${LITE3_TAG:-}"
FORCE=0

_POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--tag)        TAG="$2"; shift 2 ;;
        --tag=*)         TAG="${1#*=}"; shift ;;
        --pip-index|-i)  PIP_INDEX_URL="$2"; shift 2 ;;
        --pip-index=*)   PIP_INDEX_URL="${1#*=}"; shift ;;
        --force)         FORCE=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        --)              shift; _POSITIONAL+=("$@"); break ;;
        -*)              err "未知选项: $1"; usage >&2; exit 1 ;;
        *)               _POSITIONAL+=("$1"); shift ;;
    esac
done

TARGET="${_POSITIONAL[0]:-$TARGET_DEFAULT}"
# 第二个位置参数即 TAG
if [[ ${#_POSITIONAL[@]} -ge 2 ]]; then
    TAG="${_POSITIONAL[1]}"
fi

if [[ -n "$TAG" ]] && ! [[ "$TAG" =~ ^[0-9]+$ ]]; then
    err "TAG 必须是非负整数，收到: '$TAG'"
    err "用法: sudo bash $0 $TARGET 2"
    exit 1
fi

HTTP_PORT=8000
ROS_PORT=43900
# ROS2 节点名（并行实例必须唯一，否则 ROS 图里同名节点会互相顶掉）
ROS_NODE="lite3_ros_bridge${TAG}"
if [[ -n "$TAG" ]]; then
    HTTP_PORT=$((8000 + 10#$TAG))
    ROS_PORT=$((43900 + 10#$TAG))
fi
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="lite3-monitor${TAG}"

# 以 sudo 运行时，真正的使用者是 SUDO_USER
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"

# ---------------------------------------------------------------- 前置检查
[[ "$(uname -s)" == "Linux" ]] || { err "本脚本仅用于 Linux（103 主机），Windows 请用 start-backend.bat"; exit 1; }
[[ -f "$SRC_DIR/backend/main.py" ]] || { err "未找到 backend/main.py，请在项目根目录执行"; exit 1; }

PY_BIN="$(command -v python3 || true)"
[[ -n "$PY_BIN" ]] || { err "未找到 python3"; exit 1; }

PY_VER="$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$("$PY_BIN" -c 'import sys; print(1 if sys.version_info >= (3, 8) else 0)')"
if [[ "$PY_OK" != "1" ]]; then
    err "Python 版本过低：$PY_VER（需要 3.8 及以上）"
    exit 1
fi
log "目标目录 : $TARGET"
log "运行用户 : $RUN_USER:$RUN_GROUP"
log "Python   : $PY_BIN ($PY_VER)"

# 注意：Ubuntu 20.04 上 `import venv` 恒为真，但 ensurepip 在独立的
# python3.x-venv 包中。真正的检测放在下面的「虚拟环境（多层降级）」段落。

# 提前拦截：TAG 漏传/传错时立刻中止，避免白白建 venv、装依赖才发现服务名不对。
# （systemd 段里还有一次同样校验，那里是写单元前的最后一道防线。）
check_unit_owner "/etc/systemd/system/${SERVICE_NAME}.service" "$TARGET/backend"

# ---------------------------------------------------------------- 拷贝文件
log "拷贝工程文件 ..."
mkdir -p "$TARGET"
cp -r "$SRC_DIR/backend"    "$TARGET/"
cp -r "$SRC_DIR/tools"      "$TARGET/" 2>/dev/null || true
cp    "$SRC_DIR/requirements.txt" "$TARGET/" 2>/dev/null || true
cp    "$SRC_DIR/README.md"  "$TARGET/" 2>/dev/null || true

# 前端静态资源：只拷 dist，避免把 node_modules 一起拖到板子上
if [[ -d "$SRC_DIR/frontend/dist" ]]; then
    rm -rf "$TARGET/frontend"
    mkdir -p "$TARGET/frontend"
    cp -r "$SRC_DIR/frontend/dist" "$TARGET/frontend/"
    log "已部署前端构建产物（可通过 http://<103>:8000 直接访问）"
else
    warn "未发现 frontend/dist，将只提供 API（可在笔记本构建后拷贝，或用 VITE_BACKEND_URL 直连）"
fi

# 关键：必须先修正属主，再创建虚拟环境。
# 后续 venv / pip 都以 $RUN_USER 身份执行，若目录仍属 root 会直接写不进去。
if [[ "$(id -u)" -eq 0 ]]; then
    chown -R "$RUN_USER:$RUN_GROUP" "$TARGET"
    log "已将 $TARGET 属主设为 $RUN_USER:$RUN_GROUP"
fi

# ---------------------------------------------------------------- 辅助函数
# 以目标用户身份执行：sudo 下 HOME 会变成 root，导致找不到装在 ~/.local 的
# pip / virtualenv（这在无外网 apt、只能用 pip --user 的机器上是常态）。
as_user() {
    if [[ "$(id -u)" -eq 0 ]] && [[ -n "$RUN_USER" ]]; then
        sudo -u "$RUN_USER" -H "$@"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------- 虚拟环境（多层降级）
#
# Ubuntu 20.04 常见坑：`python3 -m venv` 存在，但 ensurepip 位于独立的
# `python3.8-venv` 包里，未安装时报 “ensurepip is not available”。
# 因此这里检测的必须是 ensurepip，而不是 venv。
VENV_DIR="$TARGET/.venv"
PY_VENV=""

# 清理上一次失败留下的残缺环境
if [[ -d "$VENV_DIR" ]] && [[ ! -x "$VENV_DIR/bin/python" ]]; then
    warn "发现残缺的 .venv，删除后重建"
    rm -rf "$VENV_DIR"
fi

if [[ -x "$VENV_DIR/bin/python" ]]; then
    log "复用已存在的虚拟环境"
    PY_VENV="$VENV_DIR/bin/python"
else
    log "创建虚拟环境 ..."
    if ! "$PY_BIN" -c "import ensurepip" >/dev/null 2>&1; then
        warn "ensurepip 不可用（Ubuntu 需 python${PY_VER}-venv），尝试安装 ..."
        # 常见坑：apt 默认 IPv6 优先，IPv6 不通时会静默超时数十秒再失败。
        # 强制 IPv4 可让 apt 在很多"看似不通外网的"内网机器上恢复可用。
        APT_OPTS=(-o Acquire::ForceIPv4=true)
        log "apt 强制 IPv4 重试 ..."
        (timeout 180 apt-get "${APT_OPTS[@]}" update -qq && \
         timeout 300 apt-get "${APT_OPTS[@]}" install -y -qq \
            "python${PY_VER}-venv" python3-pip) || \
            warn "apt 安装失败（无外网或 IPv6 不通），继续尝试其他方式"
    fi

    if as_user "$PY_BIN" -m venv "$VENV_DIR" 2>/dev/null; then
        PY_VENV="$VENV_DIR/bin/python"
        log "虚拟环境创建成功（venv）"
    else
        warn "venv 不可用，尝试 virtualenv（PyPI 在线安装）..."
        # 优先用模块方式调用，避免 ~/.local/bin 不在 sudo 的 PATH 中
        if ! as_user "$PY_BIN" -m virtualenv --version >/dev/null 2>&1; then
            log "安装 virtualenv 到用户目录 ..."
            as_user "$PY_BIN" -m pip install -q --user virtualenv 2>/dev/null || \
                warn "virtualenv 安装失败（PyPI 是否可达？）"
        fi
        if as_user "$PY_BIN" -m virtualenv "$VENV_DIR" 2>/dev/null; then
            PY_VENV="$VENV_DIR/bin/python"
            log "虚拟环境创建成功（virtualenv）"
        fi
    fi
fi

# 最后兜底：直接使用系统 Python（依赖会写入系统 site-packages）
if [[ -z "$PY_VENV" ]]; then
    warn "无法创建隔离环境，改用系统 Python 安装依赖"
    warn "（依赖将写入系统 site-packages，卸载时执行 pip3 uninstall 即可）"
    PY_VENV="$PY_BIN"
fi
log "使用解释器: $PY_VENV"

if ! as_user "$PY_VENV" -m pip --version >/dev/null 2>&1; then
    err "pip 不可用。可尝试（apt 务必强制 IPv4，否则会静默超时）："
    err "  sudo apt-get -o Acquire::ForceIPv4=true install -y python3-pip"
    err "  或：python3 -m ensurepip --user"
    exit 1
fi

# 组装 pip 参数（无外网 apt 时，PyPI 通常仍可达）
PIP_OPTS=(--disable-pip-version-check)
[[ -n "$PIP_INDEX_URL" ]] && PIP_OPTS+=(-i "$PIP_INDEX_URL")

# 兜底用系统解释器时，普通用户写不了 /usr/lib/python3.x，显式走 --user。
# （venv 内禁止 --user，所以只在 PY_VENV 就是系统 python 时才加）
if [[ "$PY_VENV" == "$PY_BIN" ]] && [[ "$(id -u)" -eq 0 ]]; then
    PIP_OPTS+=(--user)
    log "依赖将装到 ~$RUN_USER/.local（系统 Python + 用户级安装）"
fi

log "安装依赖（纯 Python 版 uvicorn，避免 aarch64 编译）..."
as_user "$PY_VENV" -m pip install "${PIP_OPTS[@]}" --upgrade pip >/dev/null 2>&1 || \
    warn "pip 升级失败，沿用现有版本"
as_user "$PY_VENV" -m pip install "${PIP_OPTS[@]}" -r "$TARGET/backend/requirements.txt" || {
    err "依赖安装失败。若 103 无法访问 PyPI，请用离线方式："
    err "  笔记本: pip download -r backend/requirements.txt -d wheels --platform manylinux2014_aarch64 --python-version 3.8 --only-binary=:all:"
    err "  103   : pip install --no-index --find-links=wheels -r requirements.txt"
    exit 1
}

# ---------------------------------------------------------------- 自检
log "运行抓包模块自检 ..."
as_user "$PY_VENV" "$TARGET/backend/udp_sniffer.py"

# 权限说明：AF_PACKET 需要 CAP_NET_RAW。
#
# 这里刻意**不**给 python 做 setcap：venv 里的 python 通常是 /usr/bin/python3.x 的符号链接，
# setcap 会穿透到系统解释器，等于给这台机器上所有 Python 程序都开了抓包能力。
# 正确做法是由 systemd 的 AmbientCapabilities 在启动服务时按需授予，权限不外溢。
if [[ "$(id -u)" -eq 0 ]]; then
    log "抓包能力由 systemd AmbientCapabilities 授予（不对系统 python 做 setcap）"
    # 以 sudo 拷贝/建 venv 后属主是 root，交还给运行用户
    chown -R "$RUN_USER:$RUN_GROUP" "$TARGET"
    log "已将 $TARGET 属主设为 $RUN_USER:$RUN_GROUP"
else
    warn "非 root 运行：无法注册 systemd 服务，稍后需手动启动"
fi

# ---------------------------------------------------------------- systemd
if [[ "$(id -u)" -eq 0 ]]; then
    log "安装 systemd 服务 ..."
    RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)"
    [[ -z "$RUN_HOME" ]] && RUN_HOME="/home/$RUN_USER"

    # 写单元前先确认不会覆盖别的实例（TAG 漏传的最后一道防线）
    check_unit_owner "/etc/systemd/system/${SERVICE_NAME}.service" "$TARGET/backend"

    # 单元模板：默认走外部 ros_bridge_node；LITE3_ROS_IMPL=direct 时改用内嵌订阅版
    # （后者 ExecStart 会 source ROS 环境，让后端自己 import rclpy）。
    UNIT_TEMPLATE="$SRC_DIR/deploy/lite3-monitor.service"
    if [[ "${LITE3_ROS_IMPL:-bridge}" == "direct" ]]; then
        UNIT_TEMPLATE="$SRC_DIR/deploy/lite3-monitor-rosdirect.service"
        log "使用内嵌 ROS 订阅模板（LITE3_ROS_IMPL=direct）"
    fi

    # venv 的 site-packages：内嵌订阅模板用它保证 venv 依赖优先于 ROS 自带包
    VENV_SITE="$("$PY_VENV" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || true)"

    sed -e "s|__DIR__|$TARGET|g" \
        -e "s|__VENV_PY__|$PY_VENV|g" \
        -e "s|__VENV_SITE__|$VENV_SITE|g" \
        -e "s|__USER__|$RUN_USER|g" \
        -e "s|__GROUP__|$RUN_GROUP|g" \
        -e "s|__HOME__|$RUN_HOME|g" \
        -e "s|__HTTP_PORT__|$HTTP_PORT|g" \
        -e "s|__ROS_PORT__|$ROS_PORT|g" \
        "$UNIT_TEMPLATE" > "/etc/systemd/system/${SERVICE_NAME}.service"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"

    log "启动服务 ..."
    # 先停掉本实例对应的 systemd 服务（不影响其它 TAG 的实例）。
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    # 清理残留的「手动」uvicorn 进程（它们没有 CAP_NET_RAW，会占着端口且 sniff 必然失败）。
    # 必须按端口精确匹配，绝不能 pkill "uvicorn main:app" 整条——否则会误杀正在运行的
    # 其它实例（例如旧 lite3-monitor 也跑着同一条命令，会被一起杀掉）。
    pkill -f "uvicorn main:app --port $HTTP_PORT" 2>/dev/null || true
    sleep 1
    systemctl restart "$SERVICE_NAME"
    sleep 3

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log "服务已启动 ✔"
    else
        warn "服务未处于 active，查看日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
    fi

    # ROS 桥接服务（ros 数据源）：best-effort 安装。
    # 它依赖 103 上 ROS2 环境就绪；即使没起来，monitor 也会自动回退 sniff（LITE3_DATA_SOURCE=auto），
    # 不影响监控。topic 名若与 transfer_ros2 实际发布的不一致，可用 /api/source 切回 sniff。
    ROS_BRIDGE_SRC="$SRC_DIR/deploy/lite3-ros-bridge.service"
    if [[ -f "$ROS_BRIDGE_SRC" ]]; then
        ROS_BRIDGE_SVC="lite3-ros-bridge${TAG}"
        check_unit_owner "/etc/systemd/system/${ROS_BRIDGE_SVC}.service" "$TARGET/backend"
        sed -e "s|__DIR__|$TARGET|g" \
            -e "s|__USER__|$RUN_USER|g" \
            -e "s|__GROUP__|$RUN_GROUP|g" \
            -e "s|__HOME__|$RUN_HOME|g" \
            -e "s|__HTTP_PORT__|$HTTP_PORT|g" \
            -e "s|__ROS_PORT__|$ROS_PORT|g" \
            -e "s|__ROS_NODE__|$ROS_NODE|g" \
            -e "s|__MONITOR_SVC__|$SERVICE_NAME|g" \
            "$ROS_BRIDGE_SRC" > "/etc/systemd/system/${ROS_BRIDGE_SVC}.service"
        systemctl daemon-reload
        systemctl enable "$ROS_BRIDGE_SVC" || true
        systemctl restart "$ROS_BRIDGE_SVC" || true
        log "已安装 ROS 桥接服务（best-effort，需 103 上 ROS2 环境就绪）"
    fi
else
    warn "非 root，已跳过 systemd 注册。手动启动命令："
    echo "      sudo bash $0 $TARGET            # 重新以 sudo 执行即可注册服务"
    echo "      # 或前台临时运行（抓包需 sudo）："
    echo "      sudo $TARGET/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port $HTTP_PORT"
fi

# ---------------------------------------------------------------- 防火墙
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    warn "检测到 ufw 已启用，如需从笔记本访问请放行 $HTTP_PORT："
    echo "      sudo ufw allow from 192.168.2.0/24 to any port $HTTP_PORT proto tcp comment 'lite3-monitor${TAG}'"
fi

# ---------------------------------------------------------------- 收尾
LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

$(echo -e '\033[1;32m')部署完成$(echo -e '\033[0m')

  安装目录 : $TARGET
  服务状态 : systemctl status $SERVICE_NAME
  实时日志 : journalctl -u $SERVICE_NAME -f
  接口自检 : curl http://127.0.0.1:$HTTP_PORT/api/status
  监控页面 : http://192.168.1.103:$HTTP_PORT（笔记本浏览器打开）
             若打不开，在 103 上放行：sudo ufw allow from 192.168.2.0/24 to any port $HTTP_PORT proto tcp

  请确认 /api/status 中：
    udp_mode  = "sniff"   ← 旁路抓包生效，未占用 43897
    connected = true      ← 已收到机器人状态

  数据源模式（默认 auto = 优先 ros，收不到自动回退 sniff）：
    GET  /api/source                              # 查看当前/配置模式
    POST /api/source/set?mode=ros|sniff|auto      # 中控页面也可一键切换
    （ros 模式需 lite3-ros-bridge${TAG} 服务在跑且 ROS2 topic 名匹配）

  若 udp_mode 为 bind 或 connected 一直为 false，见 deploy/README.md 排错表。
EOF
