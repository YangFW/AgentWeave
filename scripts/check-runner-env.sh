#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "公共镜像："
if docker image inspect agentnexus-runner:latest >/dev/null 2>&1; then
  docker run --rm agentnexus-runner:latest bash -c "codex --version && claude --version && python3 --version && node -v"
else
  echo "  未找到 agentnexus-runner:latest，请执行 ./scripts/build-runner-image.sh"
  exit 1
fi

echo
echo "工作区目录： ${APP_WORKSPACES_ROOT:-$ROOT/data/workspaces}"
mkdir -p "${APP_WORKSPACES_ROOT:-$ROOT/data/workspaces}"

ENV_FILE="${AGENTNEXUS_ENV_FILE:-$ROOT/.env.local}"
echo "配置文件： $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
  echo "  不存在。请复制 .env.example 为 .env.local 后填写密钥。"
  exit 1
fi

python3 - "$ENV_FILE" << 'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
wanted = [
    "APP_ALLOW_OUTBOUND_NETWORK",
    "APP_RUNNER_IMAGE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
]
vals = {}
for line in path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    vals[key.strip()] = value.strip().strip('"').strip("'")
print("  APP_ALLOW_OUTBOUND_NETWORK =", vals.get("APP_ALLOW_OUTBOUND_NETWORK") or "(未设置)")
print("  APP_RUNNER_IMAGE =", vals.get("APP_RUNNER_IMAGE") or "(未设置)")
print("密钥是否已填写（不显示内容）：")
for key in wanted[2:]:
    filled = bool(vals.get(key))
    print(f"  {key}: {'已填写' if filled else '空（待填写）'}")
PY
