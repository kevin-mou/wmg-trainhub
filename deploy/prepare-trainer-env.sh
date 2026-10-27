#!/bin/sh
# 训练环境准备（幂等、可后台运行）：venv + 训练依赖 + Jupyter kernel 注册。
# 由控制面部署下发在节点上以后台方式启动；状态写入 /opt/trainhub/trainer-env.status：
#   running <ts> | ready <ts> <requirements sha> | failed <ts> <rc>
set -eu
APP=/opt/trainhub/wmg-trainhub
VENV=/opt/trainhub/trainer-venv
REQ="$APP/deploy/trainer-requirements.txt"
STATUS=/opt/trainhub/trainer-env.status
LOGDIR=/opt/trainhub/trainer-logs
KERNEL_NAME=wmg-trainer
mkdir -p "$LOGDIR"
exec >>"$LOGDIR/prepare.log" 2>&1
trap 'rc=$?; [ "$rc" -eq 0 ] || echo "failed $(date -Is) $rc" >"$STATUS"' EXIT

echo "prepare trainer env: $(date -Is)"
[ -f "$REQ" ] || { echo "missing $REQ" >&2; exit 2; }
echo "running $(date -Is)" >"$STATUS"

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
chown -R trainhub:trainhub "$VENV"

SHA=$(sha256sum "$REQ" | awk '{print $1}')
MARK="$VENV/.requirements.sha256"
if [ "$(cat "$MARK" 2>/dev/null || true)" != "$SHA" ]; then
  echo "installing training requirements (sha=$SHA)…"
  runuser -u trainhub -- "$VENV/bin/pip" install -U pip wheel
  runuser -u trainhub -- "$VENV/bin/pip" install -r "$REQ"
  echo "$SHA" >"$MARK"
  chown trainhub:trainhub "$MARK"
else
  echo "training requirements unchanged (sha=$SHA), skip pip"
fi

# Jupyter kernel（系统级路径，TrainHub 服务用户可读）
"$VENV/bin/python" -m ipykernel install \
  --name "$KERNEL_NAME" --display-name "WMG Trainer (CUDA)" --prefix /usr/local >/dev/null
[ -f "/usr/local/share/jupyter/kernels/$KERNEL_NAME/kernel.json" ] || {
  echo "kernel spec missing" >&2; exit 3;
}

# 节点管理员配置 Jupyter 默认内核：训练环境即 python3（用户级 kernelspec 优先级高于 venv）
runuser -u trainhub -- "$VENV/bin/python" -m ipykernel install \
  --user --name python3 --display-name "Python 3 (WMG Trainer)" >/dev/null || true

# 依赖自检（以服务用户身份，确认 kernel 解释器可用）
runuser -u trainhub -- "$VENV/bin/python" - <<'PY'
import importlib
for name in ("torch", "transformers", "datasets", "peft", "ipykernel"):
    importlib.import_module(name)
print("training imports ok")
PY

# 让 Jupyter 的默认 python3 指向训练环境（TrainHub venv 内的 python3 kernelspec）
DEFAULT_KERNEL_DIR="$APP/.venv/share/jupyter/kernels/python3"
mkdir -p "$DEFAULT_KERNEL_DIR"
cat > "$DEFAULT_KERNEL_DIR/kernel.json" <<EOF
{
 "argv": [
  "$VENV/bin/python",
  "-Xfrozen_modules=off",
  "-m",
  "ipykernel_launcher",
  "-f",
  "{connection_file}"
 ],
 "display_name": "Python 3 (WMG Trainer)",
 "language": "python"
}
EOF
chown -R trainhub:trainhub "$DEFAULT_KERNEL_DIR"

echo "ready $(date -Is) $SHA" >"$STATUS"
echo "trainer env ready"
