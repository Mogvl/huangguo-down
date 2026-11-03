#!/usr/bin/env bash
#
# 黄果下载 · 绿联 NAS 一键部署
#
#   ./deploy.sh              构建并启动（默认）
#   ./deploy.sh up           用已有镜像启动，不构建
#   ./deploy.sh build        只构建镜像
#   ./deploy.sh pull         拉取 GHCR 预构建镜像
#   ./deploy.sh logs         跟踪日志
#   ./deploy.sh status       查看状态与健康检查
#   ./deploy.sh restart      重启
#   ./deploy.sh stop         停止
#   ./deploy.sh down         停止并删除容器
#   ./deploy.sh update       拉代码 + 重建 + 启动
#   ./deploy.sh clean        停止并删除数据目录（危险）
#
set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------- 颜色与输出
if [ -t 1 ]; then
    G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[36m'; N=$'\033[0m'
else
    G=''; R=''; Y=''; B=''; N=''
fi
info() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '%s  ✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s  !%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s  ✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查
command -v docker >/dev/null 2>&1 || \
    die "未找到 docker。请先在绿联「应用中心」安装 Docker 套件。"

if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    die "未找到 docker compose 或 docker-compose。"
fi

docker info >/dev/null 2>&1 || \
    die "docker 守护进程未运行，或当前用户没有权限（试试 sudo）。"

# ---------------------------------------------------------------- 数据目录
# compose 文件里的宿主机路径必须真实存在且可写，否则 Docker 会以 root 建目录，
# 之后 Emby 读不到。这里提前按 compose 里的映射建好。
DATA_ROOT=${DATA_ROOT:-/volume1/docker/huangguo-down}
MEDIA_ROOT=${MEDIA_ROOT:-/volume1/media/黄果}

ensure_dirs() {
    for d in "$DATA_ROOT/config" "$DATA_ROOT/data" "$DATA_ROOT/logs" "$MEDIA_ROOT"; do
        if [ ! -d "$d" ]; then
            mkdir -p "$d" 2>/dev/null || sudo mkdir -p "$d" || \
                die "无法创建目录 $d"
            info "创建 $d"
        fi
        [ -w "$d" ] || warn "$d 不可写，容器可能无法写入（检查共享文件夹权限）"
    done
    ok "数据目录就绪"
}

# compose 里的路径如果和上面不一致，提示用户
check_paths() {
    local compose_paths
    compose_paths=$(grep -oE '^\s+- /[^:]+:' docker-compose.yml | tr -d ' -' | tr -d ':' || true)
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        case "$p" in
            "$DATA_ROOT"*|"$MEDIA_ROOT"*) ;;
            *) warn "compose 里的路径 $p 不在 $DATA_ROOT / $MEDIA_ROOT 下，请确认已存在" ;;
        esac
    done <<EOF
$compose_paths
EOF
}

# ---------------------------------------------------------------- 动作
do_build() {
    info "构建镜像（首次约 3-8 分钟）"
    $DC build --pull
    ok "镜像构建完成"
}

do_pull() {
    info "拉取预构建镜像"
    $DC pull && ok "镜像已更新" || warn "拉取失败，改用本地构建：$0 build"
}

do_up() {
    ensure_dirs
    $DC up -d --remove-orphans
    ok "容器已启动"
    echo
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$ip" ] || ip="<NAS的IP>"
    printf '  前端  http://%s:7731\n' "$ip"
    printf '  后端  http://%s:7732/healthz\n' "$ip"
    printf '\n  首次启动会同步一次，日志：%s logs -f\n' "$DC"
}

do_status() {
    $DC ps
    echo
    info "健康检查"
    if curl -fsS --max-time 5 http://127.0.0.1:7731/healthz 2>/dev/null; then
        echo; ok "前端 7731 正常"
    else
        warn "前端 7731 无响应（容器刚启动时属正常，等 30 秒再看）"
    fi
    if curl -fsS --max-time 5 http://127.0.0.1:7732/healthz 2>/dev/null; then
        echo; ok "后端 7732 正常"
    else
        warn "后端 7732 无响应"
    fi
}

do_update() {
    if [ -d .git ]; then
        info "拉取最新代码"
        git pull --ff-only || warn "git pull 失败，继续用当前代码"
    fi
    do_build
    do_up
}

do_clean() {
    warn "这会删除 $DATA_ROOT 下的状态库和日志（媒体文件不受影响）"
    printf '确认继续？输入 yes：'
    read -r ans
    [ "$ans" = "yes" ] || { info "已取消"; exit 0; }
    $DC down -v --remove-orphans || true
    rm -rf "$DATA_ROOT/data" "$DATA_ROOT/logs" "$DATA_ROOT/config"
    ok "已清理"
}

case "${1:-up}" in
    build)   do_build ;;
    pull)    do_pull ;;
    up|"")   ensure_dirs; check_paths; do_build; do_up ;;
    start)   do_up ;;
    logs)    $DC logs -f --tail=200 ;;
    status|ps) do_status ;;
    restart) $DC restart && ok "已重启" ;;
    stop)    $DC stop && ok "已停止" ;;
    down)    $DC down --remove-orphans && ok "已移除" ;;
    update)  do_update ;;
    clean)   do_clean ;;
    *)
        cat <<EOF
用法: $0 [命令]

  (空) / up   构建并启动（默认）
  build       只构建镜像
  pull        拉取 GHCR 预构建镜像
  start       用已有镜像启动
  logs        跟踪日志
  status      查看状态与健康检查
  restart     重启
  stop        停止
  down        停止并删除容器
  update      拉代码 + 重建 + 启动
  clean       清理数据目录（危险）

环境变量:
  DATA_ROOT   默认 /volume1/docker/huangguo-down
  MEDIA_ROOT  默认 /volume1/media/黄果
EOF
        exit 1 ;;
esac
