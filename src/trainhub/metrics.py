from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 指标文件是可选的过程观测数据：只读取尾部有限字节，避免长训练把 API 拖慢。
MAX_METRICS_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_POINTS = 2000
MAX_POINTS_LIMIT = 5000
MAX_SERIES = 32
MAX_NAME_LENGTH = 80


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _downsample(points: list[list[float]], limit: int) -> list[list[float]]:
    if len(points) <= limit:
        return points
    if limit <= 1:
        return [points[-1]]
    step = (len(points) - 1) / (limit - 1)
    indexes = {0, len(points) - 1}
    indexes.update(round(index * step) for index in range(limit))
    return [points[index] for index in sorted(indexes)]


def parse_metrics_text(text: str, *, max_points: int = DEFAULT_MAX_POINTS) -> dict[str, Any]:
    """解析 JSONL 指标文本，返回图表友好的 series。

    每行契约：``{"step": 1, "metrics": {"loss": 0.5, "accuracy": 0.9}}``。
    ``step`` 缺失时用 ``epoch``，两者都缺失时用有效样本序号；只有有限数值
    会进入结果，非法行被跳过。最后一行没有换行符时按“正在写入”处理。
    """
    limit = max(1, min(int(max_points), MAX_POINTS_LIMIT))
    raw_lines = text.splitlines()
    if text and not text.endswith(("\n", "\r")) and raw_lines:
        tail = raw_lines[-1]
        try:
            json.loads(tail)
        except (TypeError, ValueError):
            # 训练进程正在追加最后一行：不要把半个 JSON 计成错误。
            raw_lines.pop()

    series: dict[str, list[list[float]]] = {}
    order: list[str] = []
    sample_count = 0
    skipped_lines = 0
    point_count = 0
    truncated = False

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            skipped_lines += 1
            continue
        if not isinstance(payload, dict):
            skipped_lines += 1
            continue
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            skipped_lines += 1
            continue

        sample_index = sample_count + 1
        x = _finite_number(payload.get("step"))
        if x is None:
            x = _finite_number(payload.get("epoch"))
        if x is None:
            x = float(sample_index)

        row_points: list[tuple[str, float]] = []
        new_names: set[str] = set()
        for raw_name, raw_value in metrics.items():
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()[:MAX_NAME_LENGTH]
            if not name:
                continue
            value = _finite_number(raw_value)
            if value is None:
                continue
            if name not in series and name not in new_names:
                if len(order) + len(new_names) >= MAX_SERIES:
                    truncated = True
                    continue
                new_names.add(name)
            row_points.append((name, value))

        if not row_points:
            skipped_lines += 1
            continue

        sample_count = sample_index
        for name, value in row_points:
            if name not in series:
                series[name] = []
                order.append(name)
            series[name].append([x, value])
            point_count += 1

    output: list[dict[str, Any]] = []
    for name in order:
        points = series[name]
        if len(points) > limit:
            points = _downsample(points, limit)
            truncated = True
        output.append({"name": name, "points": points})

    return {
        "series": output,
        "sampleCount": sample_count,
        "pointCount": point_count,
        "skippedLines": skipped_lines,
        "truncated": truncated,
    }


def read_metrics_file(path: Path, *, max_points: int = DEFAULT_MAX_POINTS) -> dict[str, Any]:
    """读取运行目录里的可选指标文件；不存在/不可读都返回可渲染的空结果。"""
    path = Path(path)
    empty: dict[str, Any] = {
        "present": False,
        "series": [],
        "sampleCount": 0,
        "pointCount": 0,
        "skippedLines": 0,
        "truncated": False,
        "updatedAt": None,
    }
    if path.is_symlink():
        return {**empty, "present": True, "error": "指标文件不能是符号链接"}
    if not path.is_file():
        return empty

    try:
        stat = path.stat()
        empty["present"] = True
        empty["updatedAt"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        )
        if stat.st_size <= 0:
            return empty

        read_bytes = min(stat.st_size, MAX_METRICS_BYTES)
        tail_truncated = stat.st_size > MAX_METRICS_BYTES
        with path.open("rb") as handle:
            if tail_truncated:
                handle.seek(stat.st_size - read_bytes)
            raw = handle.read(read_bytes)
        if tail_truncated:
            # 从中间开始读时丢掉第一条不完整的行。
            newline = raw.find(b"\n")
            raw = raw[newline + 1 :] if newline >= 0 else b""
        parsed = parse_metrics_text(raw.decode("utf-8", errors="replace"), max_points=max_points)
        parsed["present"] = True
        parsed["updatedAt"] = empty["updatedAt"]
        parsed["truncated"] = bool(parsed["truncated"] or tail_truncated)
        return parsed
    except OSError as exc:
        return {**empty, "present": True, "error": f"无法读取指标文件: {exc}"}
