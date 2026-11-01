#!/bin/sh
# 构建 TrainHub 沙箱镜像（Linux）。用法：
#   deploy/build-sandbox-image.sh             # CPU 最小内核镜像（base）
#   deploy/build-sandbox-image.sh trainer     # CPU + deploy/trainer-requirements.txt
#   deploy/build-sandbox-image.sh cuda        # NVIDIA CUDA 训练镜像（示例 Dockerfile.cuda）
#
# 镜像名取 TRAINHUB_SANDBOX_IMAGE，默认 trainhub-sandbox:latest。构建后把同一个值写进
# TrainHub 的 .env；镜像只包含运行时，不包含 TrainHub 代码/数据/密钥。
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-base}"
IMAGE="${TRAINHUB_SANDBOX_IMAGE:-trainhub-sandbox:latest}"
SANDBOX_UID="$(id -u)"
SANDBOX_GID="$(id -g)"

command -v docker >/dev/null 2>&1 || {
    echo "未找到 docker；沙箱功能要求 Linux 节点安装 Docker" >&2
    exit 1
}

case "$MODE" in
    base)
        docker build \
            --build-arg "SANDBOX_UID=$SANDBOX_UID" \
            --build-arg "SANDBOX_GID=$SANDBOX_GID" \
            -t "$IMAGE" -f deploy/sandbox/Dockerfile .
        ;;
    trainer)
        docker build \
            --target trainer \
            --build-arg "SANDBOX_UID=$SANDBOX_UID" \
            --build-arg "SANDBOX_GID=$SANDBOX_GID" \
            -t "$IMAGE" -f deploy/sandbox/Dockerfile .
        ;;
    cuda)
        docker build \
            --build-arg "SANDBOX_UID=$SANDBOX_UID" \
            --build-arg "SANDBOX_GID=$SANDBOX_GID" \
            -t "$IMAGE" -f deploy/sandbox/Dockerfile.cuda .
        ;;
    *)
        echo "用法：$0 [base|trainer|cuda]" >&2
        exit 2
        ;;
esac

echo "沙箱镜像已构建：$IMAGE"
echo "请在 TrainHub .env 中设置 TRAINHUB_SANDBOX=docker 与 TRAINHUB_SANDBOX_IMAGE=$IMAGE"
