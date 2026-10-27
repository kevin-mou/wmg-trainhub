#!/usr/bin/env bash
# TrainHub CUDA 训练节点安装脚本（在 GPU 节点上执行）。
#
# 前提：
#   1. 代码已由开发机 rsync 到本地目录（默认 /opt/trainhub/wmg-trainhub 的上级）：
#        rsync -az --exclude .venv --exclude data --exclude '__pycache__' \
#          wmg-trainhub/ ubuntu@<node>:/tmp/wmg-trainhub/
#        sudo mkdir -p /opt/trainhub && sudo mv /tmp/wmg-trainhub /opt/trainhub/
#   2. 训练依赖与 Jupyter kernel 由本脚本自动准备（无需预置训练代码）。
#   3. 节点已安装 docker + nvidia-container-toolkit（训练镜像 wmg-trainer/cuda-torch:2.6）。
#
# 脚本做：创建 trainhub 用户（docker 组）→ venv + 依赖 → 训练依赖与 Jupyter kernel →
#         默认 env 文件 → 安装 systemd 单元并启动。控制面托管键由「部署下发」写入
#         /etc/trainhub/trainhub.env，本脚本不写密钥。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/trainhub/wmg-trainhub}"
DATA_DIR="${DATA_DIR:-/data/trainhub}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "未找到 $APP_DIR/requirements.txt —— 请先把 wmg-trainhub 代码放到该目录" >&2
  exit 1
fi

echo "==> 创建运行用户 trainhub（docker 组）"
if ! id trainhub >/dev/null 2>&1; then
  sudo useradd -r -m -G docker -s /bin/bash trainhub
fi
sudo mkdir -p "$DATA_DIR" /etc/trainhub
sudo chown -R trainhub:trainhub "$APP_DIR" "$DATA_DIR"

echo "==> 创建 venv 并安装依赖"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  sudo -u trainhub "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
sudo -u trainhub "$APP_DIR/.venv/bin/pip" install -U pip
sudo -u trainhub "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> 写入默认 env（已有则保留；控制面托管键由部署下发）"
if [ ! -f /etc/trainhub/trainhub.env ]; then
  sudo tee /etc/trainhub/trainhub.env >/dev/null <<'EOF'
# TrainHub 节点基础配置（由 deploy/cuda-node-install.sh 生成）
TRAINHUB_DATA_DIR=/data/trainhub
TRAINHUB_HOST=127.0.0.1
TRAINHUB_PORT=7470
TRAINHUB_MAX_CONCURRENT_RUNS=1
TRAINHUB_LOG_LEVEL=info
EOF
  sudo chmod 600 /etc/trainhub/trainhub.env
fi

echo "==> 准备训练依赖与 Jupyter kernel（首次安装 torch 等需数分钟）"
sudo sh "$APP_DIR/deploy/prepare-trainer-env.sh"

echo "==> 安装 systemd 单元并启动"
sudo cp "$APP_DIR/deploy/trainhub.service" /etc/systemd/system/trainhub.service
sudo systemctl daemon-reload
sudo systemctl enable --now trainhub.service
sudo systemctl --no-pager --lines=5 status trainhub.service || true

echo "完成：http://127.0.0.1:7470/api/health"
