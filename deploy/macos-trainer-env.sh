#!/usr/bin/env bash
# 创建/更新 macOS/MLX 训练内核 venv：
#   - 干净 venv（不使用 --system-site-packages，也不修改 TrainHub 应用 venv）
#   - 固定版本依赖（deploy/trainer-requirements-macos.txt）
#   - startlux-decide：wheelhouse 优先，否则钉 commit 从 git 安装
#   - 注册用户级 Jupyter 内核（默认 wmg-mlx）、导入自检、写 requirements.lock
#
# 用法：
#   deploy/macos-trainer-env.sh                  # 创建/更新默认 venv 并注册内核
#   deploy/macos-trainer-env.sh --dry-run        # 只打印计划
#   deploy/macos-trainer-env.sh --recreate       # 删除旧 venv 后重建
#   VENV=/opt/trainhub/trainer-mlx-venv KERNEL_NAME=wmg-mlx deploy/macos-trainer-env.sh
#
# 注意：以 TrainHub 服务用户执行（不要 sudo）；内核注册到该用户的 ~/Library/Jupyter/kernels。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HOME/wmg-model-ops/trainer-mlx-venv}"
KERNEL_NAME="${KERNEL_NAME:-wmg-mlx}"
KERNEL_DISPLAY="${KERNEL_DISPLAY:-WMG MLX}"
REQUIREMENTS="${REQUIREMENTS:-$DIR/trainer-requirements-macos.txt}"
WHEELHOUSE="${WHEELHOUSE:-/opt/trainhub/wheelhouse}"
STARTLUX_COMMIT="${STARTLUX_COMMIT:-ead714859f231c57e6263e18db63a1ee4c68f1ea}"
STARTLUX_SPEC="${STARTLUX_SPEC:-git+https://github.com/StartLuxLabs/StartLux-Decision.git@$STARTLUX_COMMIT}"

MODE="install"
case "${1:-}" in
  --dry-run) MODE="dry-run" ;;
  --recreate) MODE="recreate" ;;
  "") ;;
  *) echo "未知参数：$1（支持 --dry-run / --recreate）" >&2; exit 2 ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "此脚本只用于 macOS/MLX 节点；Linux 节点请用 prepare-trainer-env.sh 或控制面部署下发。" >&2
  exit 1
fi

if [[ "$(id -u)" -eq 0 ]]; then
  echo "不要用 root 运行：内核会注册到 root 的 ~/Library/Jupyter，TrainHub 服务用户看不到。" >&2
  exit 1
fi

resolve_path() {
  ( cd "$1" 2>/dev/null && pwd -P ) || printf '%s\n' "$1"
}

APP_VENV="$(resolve_path "$DIR/../.venv")"
if [[ "$(resolve_path "$VENV")" == "$APP_VENV" ]]; then
  echo "拒绝：VENV 指向 TrainHub 应用 venv（$APP_VENV）；训练依赖不能装进应用 venv。" >&2
  exit 1
fi

pick_python() {
  local candidate
  for candidate in "${BASE_PYTHON:-}" python3.11 python3.12 python3.13 python3; do
    [[ -n "$candidate" ]] || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

BASE_PYTHON_RESOLVED="$(pick_python)" || {
  echo "找不到 Python >= 3.10；请用 BASE_PYTHON=/path/to/python3.11 指定。" >&2
  exit 1
}

STARTLUX_SOURCE="git@${STARTLUX_COMMIT}"
if compgen -G "$WHEELHOUSE/startlux_decide-*.whl" >/dev/null; then
  STARTLUX_SOURCE="wheelhouse（$WHEELHOUSE）"
fi

if [[ "$MODE" == "dry-run" ]]; then
  cat <<EOF
计划：
  基础解释器  $BASE_PYTHON_RESOLVED（$("$BASE_PYTHON_RESOLVED" -V 2>&1)）
  训练 venv   $VENV（干净 venv，无 --system-site-packages）
  依赖清单    $REQUIREMENTS
  startlux    $STARTLUX_SOURCE
  Jupyter 内核 $KERNEL_NAME（$KERNEL_DISPLAY，注册到当前用户）
EOF
  exit 0
fi

if [[ "$MODE" == "recreate" && -e "$VENV" ]]; then
  echo "删除旧 venv：$VENV"
  rm -rf "$VENV"
fi

if [[ -x "$VENV/bin/python" ]]; then
  echo "复用已有 venv：$VENV"
else
  echo "创建干净 venv：$VENV（基于 $BASE_PYTHON_RESOLVED）"
  "$BASE_PYTHON_RESOLVED" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$REQUIREMENTS"

shopt -s nullglob
wheels=("$WHEELHOUSE"/startlux_decide-*.whl)
if (( ${#wheels[@]} > 0 )); then
  echo "安装 startlux-decide wheel：${wheels[-1]}"
  "$VENV/bin/python" -m pip install --upgrade "${wheels[-1]}"
else
  echo "安装 startlux-decide：$STARTLUX_SPEC"
  "$VENV/bin/python" -m pip install --upgrade "$STARTLUX_SPEC"
fi

"$VENV/bin/python" -m ipykernel install --user --name "$KERNEL_NAME" --display-name "$KERNEL_DISPLAY"

"$VENV/bin/python" - <<'PY'
import importlib.metadata as md

import mlx
import mlx_vlm
import transformers
import huggingface_hub
from startlux_decide.prompt import from_systemone, messages, plain_prompt  # noqa: F401
from mlx_vlm.trainer.peft.utils import get_peft_model, save_adapter  # noqa: F401

for name in ("mlx", "mlx-vlm", "transformers", "huggingface-hub", "startlux-decide", "ipykernel"):
    print(f"  {name}=={md.version(name)}")
print("[OK] 训练导入自检通过")
PY

"$VENV/bin/python" -m pip freeze > "$VENV/requirements.lock"
echo "版本快照：$VENV/requirements.lock"
echo "内核已注册：$KERNEL_NAME（TrainHub 提交任务时选择它，或在 notebook metadata 写 kernelspec）"
