#!/bin/sh

set -eu

AGENTWEAVE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$AGENTWEAVE_ROOT"

if [ ! -x ".venv/bin/python3" ]; then
  echo "未找到本地运行环境，请先执行："
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  exit 1
fi

AGENTWEAVE_ENV_FILE=${AGENTWEAVE_ENV_FILE:-${AGENTNEXUS_ENV_FILE:-.env.local}}
AGENTWEAVE_HOST=${AGENTWEAVE_HOST:-${AGENTNEXUS_HOST:-0.0.0.0}}
AGENTWEAVE_PORT=${AGENTWEAVE_PORT:-${AGENTNEXUS_PORT:-8000}}

if [ -f "$AGENTWEAVE_ENV_FILE" ]; then
  echo "使用本机配置 $AGENTWEAVE_ENV_FILE 启动 AgentWeave"
  exec .venv/bin/python3 -m uvicorn app.main:app \
    --env-file "$AGENTWEAVE_ENV_FILE" \
    --host "$AGENTWEAVE_HOST" \
    --port "$AGENTWEAVE_PORT"
fi

echo "未找到 $AGENTWEAVE_ENV_FILE，将使用安全默认配置启动。"
echo "在线模型默认不可用；需要时复制 .env.example 为 .env.local 并配置网络白名单。"
exec .venv/bin/python3 -m uvicorn app.main:app \
  --host "$AGENTWEAVE_HOST" \
  --port "$AGENTWEAVE_PORT"
