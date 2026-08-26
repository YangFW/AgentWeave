#!/bin/sh

set -eu

AGENTNEXUS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$AGENTNEXUS_ROOT"

if [ ! -x ".venv/bin/uvicorn" ]; then
  echo "未找到本地运行环境，请先执行："
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  exit 1
fi

AGENTNEXUS_ENV_FILE=${AGENTNEXUS_ENV_FILE:-.env.local}
AGENTNEXUS_HOST=${AGENTNEXUS_HOST:-127.0.0.1}
AGENTNEXUS_PORT=${AGENTNEXUS_PORT:-8000}

if [ -f "$AGENTNEXUS_ENV_FILE" ]; then
  echo "使用本机配置 $AGENTNEXUS_ENV_FILE 启动 AgentNexus"
  exec .venv/bin/uvicorn app.main:app \
    --env-file "$AGENTNEXUS_ENV_FILE" \
    --host "$AGENTNEXUS_HOST" \
    --port "$AGENTNEXUS_PORT"
fi

echo "未找到 $AGENTNEXUS_ENV_FILE，将使用安全默认配置启动。"
echo "在线模型默认不可用；需要时复制 .env.example 为 .env.local 并配置网络白名单。"
exec .venv/bin/uvicorn app.main:app \
  --host "$AGENTNEXUS_HOST" \
  --port "$AGENTNEXUS_PORT"
