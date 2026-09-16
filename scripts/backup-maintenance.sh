#!/bin/sh
set -eu

if [ "${1:-}" != "--maintenance-window-approved" ]; then
  echo '此操作会短暂停止 API/Worker；确认维护窗口后传入 --maintenance-window-approved。' >&2
  exit 2
fi
AGENTNEXUS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
AGENTNEXUS_COMPOSE_ENV=${AGENTNEXUS_COMPOSE_ENV:-"$AGENTNEXUS_ROOT/.env.release"}
AGENTNEXUS_MAINTENANCE_LOCK=${AGENTNEXUS_MAINTENANCE_LOCK:-"$AGENTNEXUS_ROOT/data/.backup-maintenance.lock"}
if ! command -v flock >/dev/null 2>&1; then
  echo '维护备份需要本机 flock 命令；尚未操作容器。' >&2
  exit 1
fi
mkdir -p "$(dirname -- "$AGENTNEXUS_MAINTENANCE_LOCK")"
exec 9>>"$AGENTNEXUS_MAINTENANCE_LOCK"
if ! flock -n 9; then
  echo '已有维护备份正在运行，尚未操作容器。' >&2
  exit 1
fi
compose() {
  docker compose --env-file "$AGENTNEXUS_COMPOSE_ENV" -f "$AGENTNEXUS_ROOT/docker-compose.yml" "$@"
}
compose config --quiet
AGENTNEXUS_RUNNING_IDS=$(compose ps -q --status running agentnexus worker)
for container in $AGENTNEXUS_RUNNING_IDS; do
  case "$container" in *[!a-f0-9]*) echo '无效容器 ID，操作已中止' >&2; exit 1 ;; esac
done
restore_services() {
  result=$?
  trap - EXIT HUP INT TERM
  for container in $AGENTNEXUS_RUNNING_IDS; do
    if ! docker start "$container"; then
      echo "无法恢复备份前运行的容器：$container" >&2
      result=1
    fi
  done
  exit "$result"
}
trap restore_services EXIT
trap 'exit 130' HUP INT TERM
for container in $AGENTNEXUS_RUNNING_IDS; do
  docker stop --time 30 "$container"
done
sh "$AGENTNEXUS_ROOT/scripts/backup-data.sh" --quiesced
