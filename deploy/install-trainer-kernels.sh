#!/bin/sh
# 训练加速内核预装（幂等，可后台运行）：flash-linear-attention + causal-conv1d。
#
# 由 deploy/prepare-trainer-env.sh 在准备训练环境时调用；也可手工执行（需 root）：
#   sudo sh /opt/trainhub/wmg-trainhub/deploy/install-trainer-kernels.sh
# 只服务训练 venv（/opt/trainhub/trainer-venv），不改变 TrainHub 应用 venv。
#
# - fla（+ fla-core）：线性注意力（Gated DeltaNet）与 FusedRMSNormGated 的 Triton 融合算子；
#   不带依赖安装，避免顺带升级训练脚本自己安装的 transformers。
# - causal-conv1d：短卷积 CUDA 扩展。必须用与 torch 相同大版本的 nvcc 编译
#   （torch cu13 → CUDA 13 工具链），并且只按本机 GPU 的 compute capability 编译。
# - 任何一步失败都只是让训练回退到较慢的 torch 实现，不影响可用性；
#   状态写入 /opt/trainhub/trainer-kernels.status（running|ready|failed），
#   日志追加到 /opt/trainhub/trainer-logs/kernels.log。
set -u

VENV=/opt/trainhub/trainer-venv
PY="$VENV/bin/python"
STATUS=/opt/trainhub/trainer-kernels.status
LOGDIR=/opt/trainhub/trainer-logs
FLA_VERSION=0.6.0
CONV1D_VERSION=1.7.0
# causal-conv1d 1.7.0 的 gencode 白名单（CUDA 13 也仍支持 sm75 起）
SUPPORTED_ARCHS="75 80 87 90 100 103 110 120 121"

mkdir -p "$LOGDIR"
exec >>"$LOGDIR/kernels.log" 2>&1

say() { echo "$(date -Is) $*"; }
fail() {
  say "FAILED: $*"
  echo "failed $(date -Is) $*" >"$STATUS"
  exit 1
}
as_trainhub() { runuser -u trainhub -- "$@"; }

module_ok() {
  as_trainhub "$PY" - "$1" <<'PY' 2>/dev/null
import importlib.util, sys
print("ok" if importlib.util.find_spec(sys.argv[1]) else "missing")
PY
}

say "=== trainer kernels install start"
[ -x "$PY" ] || fail "trainer venv missing: $VENV"

# ---- 0) startlux_decide wheel 缓存（消除训练时对 GitHub 的依赖） ---------------
# 训练脚本每次任务会 bootstrap startlux_decide；节点访问 GitHub 偶发失败会把任务直接打挂。
# 部署时在这里预构建一份 wheel 到节点本地 wheelhouse，训练脚本优先用它（见 startlux_requirement）。
WHEELHOUSE=/opt/trainhub/wheelhouse
STARTLUX_GIT="git+https://github.com/StartLuxLabs/StartLux-Decision.git@v1.1.0"
mkdir -p "$WHEELHOUSE"
chown trainhub:trainhub "$WHEELHOUSE"
if ls "$WHEELHOUSE"/startlux_decide-*.whl >/dev/null 2>&1; then
  say "startlux wheel already cached, skip"
else
  built=1
  for attempt in 1 2 3; do
    say "building startlux_decide wheel (attempt $attempt)"
    if as_trainhub "$PY" -m pip wheel --disable-pip-version-check --no-cache-dir \
      --no-deps -w "$WHEELHOUSE" "$STARTLUX_GIT"; then
      built=0
      break
    fi
    sleep 5
  done
  if [ "$built" = 0 ]; then
    say "startlux wheel cached: $(ls "$WHEELHOUSE" | tr '\n' ' ')"
  else
    say "WARN: startlux wheel 构建失败（训练时会回退到运行时 git 安装）"
  fi
fi

if [ "$(uname -s)" != "Linux" ]; then
  say "non-Linux node: skip (no CUDA kernels)"
  echo "ready $(date -Is) (skipped non-linux)" >"$STATUS"
  exit 0
fi

# ---- 1) flash-linear-attention -------------------------------------------------
if [ "$(module_ok fla)" = "ok" ] && [ "$(module_ok fla.ops)" = "ok" ]; then
  say "fla already installed, skip"
else
  echo "running $(date -Is) fla" >"$STATUS"
  say "installing flash-linear-attention==$FLA_VERSION (+fla-core)"
  as_trainhub "$PY" -m pip install --disable-pip-version-check --no-cache-dir \
    --no-deps "flash-linear-attention==$FLA_VERSION" "fla-core==$FLA_VERSION" \
    || fail "pip install flash-linear-attention failed"
  as_trainhub "$PY" -m pip install --disable-pip-version-check --no-cache-dir einops \
    || fail "pip install einops failed"
fi

# ---- 2) causal-conv1d（可选） ---------------------------------------------------
# ninja 是扩展构建依赖（causal-conv1d 的包声明也要求它）；装上是无损的
if [ "$(module_ok ninja)" = "ok" ]; then
  say "ninja already installed, skip"
else
  as_trainhub "$PY" -m pip install --disable-pip-version-check --no-cache-dir ninja \
    || say "WARN: pip install ninja failed（构建可能退化为串行，较慢）"
fi

if [ "$(module_ok causal_conv1d)" = "ok" ]; then
  say "causal-conv1d already installed, skip"
  echo "ready $(date -Is)" >"$STATUS"
  say "=== trainer kernels ready"
  exit 0
fi

echo "running $(date -Is) causal-conv1d" >"$STATUS"

TORCH_CUDA_MAJOR=$(as_trainhub "$PY" - <<'PY' 2>/dev/null || true
import torch
print((torch.version.cuda or "0").split(".")[0] or "0")
PY
)
[ -n "${TORCH_CUDA_MAJOR:-}" ] || TORCH_CUDA_MAJOR=0
if [ "$TORCH_CUDA_MAJOR" -lt 13 ] || ! command -v nvidia-smi >/dev/null 2>&1; then
  say "torch CUDA $TORCH_CUDA_MAJOR / nvidia-smi unavailable: skip causal-conv1d"
  echo "ready $(date -Is) (causal-conv1d skipped)" >"$STATUS"
  say "=== trainer kernels ready (causal-conv1d skipped)"
  exit 0
fi

ARCHS=""
for cap in $(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | sort -u); do
  arch=$(echo "$cap" | tr -d '.')
  case " $SUPPORTED_ARCHS " in
    *" $arch "*) ARCHS="$ARCHS $arch" ;;
  esac
done
[ -n "$ARCHS" ] || fail "no CUDA arch supported by causal-conv1d (caps: $(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | tr '\n' ' '))"

# ---- 3) CUDA 13 编译器（与 torch cu13 匹配） ------------------------------------
CUDA_HOME=""
for d in /usr/local/cuda-13.*; do
  [ -x "$d/bin/nvcc" ] && CUDA_HOME="$d"
done
if [ -z "$CUDA_HOME" ]; then
  command -v apt-get >/dev/null 2>&1 || fail "no CUDA 13 toolkit and apt-get unavailable"
  say "installing CUDA 13 compiler (nvcc) via NVIDIA apt repo"
  wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
    -O /tmp/cuda-keyring.deb || fail "download cuda keyring failed"
  dpkg -i /tmp/cuda-keyring.deb >/dev/null || fail "install cuda keyring failed"
  apt-get update -qq || fail "apt-get update failed"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cuda-nvcc-13-0 cuda-cudart-dev-13-0 cuda-crt-13-0 cuda-cccl-13-0 \
    || fail "install CUDA 13 compiler failed"
  for d in /usr/local/cuda-13.*; do
    [ -x "$d/bin/nvcc" ] && CUDA_HOME="$d"
  done
  [ -n "$CUDA_HOME" ] || fail "CUDA 13 nvcc still missing after install"
fi
say "using CUDA_HOME=$CUDA_HOME"

# ---- 4) 源码构建（裁剪 gencode 到本机架构，缩短构建时间） ------------------------
BUILD=$(mktemp -d /tmp/causal-conv1d.XXXXXX) || fail "mktemp failed"
as_trainhub "$PY" -m pip download --disable-pip-version-check --no-cache-dir --no-deps \
  --no-binary :all: "causal-conv1d==$CONV1D_VERSION" -d "$BUILD" \
  || fail "download causal-conv1d sdist failed"
as_trainhub "$PY" - "$BUILD" "$CONV1D_VERSION" "$ARCHS" <<'PY' || fail "patch setup.py failed"
import pathlib, sys, tarfile

build, version, archs = sys.argv[1], sys.argv[2], sys.argv[3].split()
tgz = next(pathlib.Path(build).glob(f"causal_conv1d-{version}.tar.gz"))
with tarfile.open(tgz) as tar:
    tar.extractall(build)
setup = pathlib.Path(build, f"causal_conv1d-{version}", "setup.py")
lines = setup.read_text().splitlines(keepends=True)
start = next(i for i, line in enumerate(lines) if 'cc_flag.append("-gencode")' in line)
end = next(
    i for i, line in enumerate(lines) if line.strip().startswith("# HACK: The compiler flag")
)
replacement = []
for arch in archs:
    replacement += [
        '        cc_flag.append("-gencode")\n',
        f'        cc_flag.append("arch=compute_{arch},code=sm_{arch}")\n',
    ]
replacement.append("\n")
lines[start:end] = replacement
setup.write_text("".join(lines))
print("patched gencode:", archs)
PY
JOBS=$(nproc 2>/dev/null || echo 2)
[ "$JOBS" -gt 4 ] && JOBS=4
say "building causal-conv1d for archs:$ARCHS (MAX_JOBS=$JOBS)"
as_trainhub env CUDA_HOME="$CUDA_HOME" MAX_JOBS="$JOBS" \
  "$PY" -m pip install --disable-pip-version-check --no-cache-dir \
  --no-build-isolation --no-deps "$BUILD/causal_conv1d-$CONV1D_VERSION" \
  || { rm -rf "$BUILD"; fail "build causal-conv1d failed (see kernels.log)"; }
rm -rf "$BUILD"

[ "$(module_ok causal_conv1d)" = "ok" ] || fail "causal_conv1d import failed after install"
echo "ready $(date -Is)" >"$STATUS"
say "=== trainer kernels ready"
