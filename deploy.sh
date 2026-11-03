#!/usr/bin/env bash
#
# 黄果解析 · 一键部署
#
#   ./deploy.sh            # 构建并启动
#   ./deploy.sh logs       # 查看日志
#   ./deploy.sh stop       # 停止
#   ./deploy.sh restart    # 重启
#   ./deploy.sh update     # 拉取最新代码并重建
#
set -euo pipefail
cd "$(dirname "$0")"

# ---- 颜色
if [ -t 1 ]; then
  G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[36m'; N=$'\033[0m'
else
  G=''; R=''; Y=''; B=''; N=''
fi
info() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '%s  ✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s  !%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s  ✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# ---- 选择 compose 命令
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "未找到 docker compose。请先安装 Docker 与 Compose 插件。"
fi

CMD="${1:-up}"

case "$CMD" in
  up|"")
    command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker。"

    info "检查端口占用"
    for p in 7731 7732; do
      if (command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":$p ") || \
         (command -v netstat >/dev/null && netstat -ltn 2>/dev/null | grep -q ":$p "); then
        warn "端口 $p 已被占用，若部署失败请先释放该端口"
      fi
    done
    ok "端口检查完成"

    info "拉取镜像（ghcr.io）"
    $DC pull

    info "启动服务"
    $DC up -d

    info "等待服务就绪"
    ready=0
    for i in $(seq 1 60); do
      if curl -sf http://127.0.0.1:7731/healthz >/dev/null 2>&1; then
        ready=1; break
      fi
      sleep 2
    done

    echo
    if [ "$ready" = "1" ]; then
      ok "部署成功"
      echo
      IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "你的NAS_IP")
      printf '  %s前端%s     http://%s:7731/\n' "$G" "$N" "$IP"
      printf '  %s后端%s     http://%s:7732/healthz  （仅内网，未对外映射）\n' "$G" "$N" "$IP"
      printf '  %s接口自检%s http://%s:7731/api/\n' "$G" "$N" "$IP"
      echo
      $DC ps
    else
      warn "服务启动超时，请检查日志："
      echo
      $DC logs --tail=50
      exit 1
    fi
    ;;

  logs)
    $DC logs -f --tail=100
    ;;

  stop)
    info "停止服务"
    $DC down
    ok "已停止"
    ;;

  restart)
    info "重启服务"
    $DC restart
    ok "已重启"
    ;;

  update)
    info "拉取最新代码"
    if [ -d .git ]; then
      git pull --ff-only
    else
      warn "当前目录不是 git 仓库，跳过拉取"
    fi
    info "拉取最新镜像并重建"
    $DC pull
    $DC up -d
    ok "更新完成"
    ;;

  status)
    $DC ps
    ;;

  # 从源码本地构建（不依赖 GHCR），适合改代码后自测
  build)
    info "从源码本地构建镜像"
    $DC build
    $DC up -d
    ok "本地构建并启动完成"
    ;;

  *)
    echo "用法: $0 [up|logs|stop|restart|update|status|build]"
    exit 1
    ;;
esac
