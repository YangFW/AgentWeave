#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${1:-agentnexus-runner:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Building runner image: ${IMAGE_NAME}..."
BUILD_ARGS=()
if [ -n "${http_proxy:-}" ]; then
    BUILD_ARGS+=(--build-arg "http_proxy=${http_proxy}")
fi
if [ -n "${https_proxy:-}" ]; then
    BUILD_ARGS+=(--build-arg "https_proxy=${https_proxy}")
fi
docker build \
    --network=host \
    "${BUILD_ARGS[@]}" \
    -t "${IMAGE_NAME}" \
    -f "${ROOT_DIR}/docker/runner.Dockerfile" \
    "${ROOT_DIR}"

echo "Successfully built ${IMAGE_NAME}"
