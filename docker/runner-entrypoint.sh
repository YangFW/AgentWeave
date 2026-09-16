#!/bin/sh
set -e

export HOME="${HOME:-/home/node}"
mkdir -p "$HOME/.codex" "$HOME/.claude" 2>/dev/null || true

# Project-local install roots live on the host bind mount at /workspace.
# Do not install runtime dependencies into the container image layer.
export PYTHONUSERBASE="${PYTHONUSERBASE:-/workspace/.python-user}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/workspace/.cache/pip}"
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-/workspace/.npm}"
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-/workspace/.npm-global}"
mkdir -p "$PYTHONUSERBASE" "$PIP_CACHE_DIR" "$NPM_CONFIG_CACHE" "$NPM_CONFIG_PREFIX/bin" 2>/dev/null || true

if [ ! -x /workspace/.venv/bin/python ]; then
    python3 -m venv /workspace/.venv
fi
export VIRTUAL_ENV="/workspace/.venv"
export PATH="/workspace/.venv/bin:/workspace/node_modules/.bin:/workspace/.npm-global/bin:/workspace/.python-user/bin:$PATH"

if [ "$#" -eq 0 ]; then
    exec /bin/bash
fi

exec "$@"
