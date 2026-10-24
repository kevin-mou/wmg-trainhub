#!/usr/bin/env bash
# 启动 TrainHub：优先使用子工程自带的 .venv，其次使用 PATH 上的 python3。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ -f "$DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$DIR/.env"
    set +a
fi

export PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}"

PYTHON="${TRAINHUB_PYTHON:-$DIR/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

exec "$PYTHON" -m trainhub.cli serve --host "${TRAINHUB_HOST:-0.0.0.0}" --port "${TRAINHUB_PORT:-7470}"
