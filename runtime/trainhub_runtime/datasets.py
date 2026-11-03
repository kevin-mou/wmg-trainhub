"""数据集读取：按名称加载 TrainHub 数据集（含其他客户共享的数据集）。

数据集在服务端按「所有者 + 数据集 ID」存放
（``customers/<owner>/datasets/<dataset_id>/files``）：名称既不是目录名，也不保证
全局唯一；共享数据集的文件目录属于所有者。因此运行时**不自己拼路径**，而是读取
TrainHub 写入执行环境的清单（``WMG_DATASET_CATALOG``，缺省回落到
``$WMG_WORK_DIR/.trainhub/datasets.json``），由服务端解析好绝对路径。

引用规则：

- ``load_dataset("my-set")``：自己的数据集；当前 Notebook/任务选中的自己的数据集优先；
- ``load_dataset("c_ab12cd34/my-set")``：其他客户共享的数据集，**必须**用
  ``customer_id/数据集名称`` 引用（``customer_id`` 可在 ``list_datasets()`` 里看到）；
- ``load_dataset()``：当前 Notebook/任务选中的数据集（不论自有还是共享）。

标准 WMG 数据集目录包含 ``dataset_manifest.json``、``groups.json`` 与
``train.jsonl`` / ``valid.jsonl`` / ``test.jsonl``；加载器也识别 manifest ``artifacts``
里带客户前缀的 JSONL 文件名（如 ``wmg_mail_v3full_train.jsonl``）。

    from trainhub_runtime import load_dataset

    dataset = load_dataset("wmg-mail-v3full-v1")
    print(len(dataset.train), len(dataset.test))
    for record in dataset.train:
        print(record["fields"]["subject"], record["answers"])
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

CATALOG_DIR = ".trainhub"
CATALOG_FILENAME = "datasets.json"
DEFAULT_SPLITS = ("train", "valid", "test")


class DatasetError(RuntimeError):
    """数据集不存在、名称不唯一、目录不可用或文件格式错误。"""


@dataclass
class Dataset:
    """``load_dataset`` 的返回结果。"""

    name: str
    path: str
    owner_id: str = ""
    owner_name: str = ""
    shared: bool = False
    manifest: dict[str, Any] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)
    splits: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)

    @property
    def reference(self) -> str:
        """可再次传给 ``load_dataset`` 的引用（共享数据集带 customer_id 前缀）。"""
        if self.shared and self.owner_id:
            return f"{self.owner_id}/{self.name}"
        return self.name

    @property
    def train(self) -> list[dict[str, Any]]:
        return self.splits.get("train", [])

    @property
    def valid(self) -> list[dict[str, Any]]:
        return self.splits.get("valid", [])

    @property
    def test(self) -> list[dict[str, Any]]:
        return self.splits.get("test", [])

    def split(self, name: str, default: Any = None) -> Any:
        return self.splits.get(name, default)

    def __getitem__(self, name: str) -> list[dict[str, Any]]:
        return self.splits[name]

    def __contains__(self, name: object) -> bool:
        return name in self.splits

    def __len__(self) -> int:
        return sum(len(records) for records in self.splits.values())


def _catalog_file(explicit: str | Path | None = None) -> Path | None:
    if explicit is not None:
        raw = str(explicit).strip()
        return Path(raw).expanduser() if raw else None
    value = (os.environ.get("WMG_DATASET_CATALOG") or "").strip()
    if value:
        return Path(value).expanduser()
    work_dir = (os.environ.get("WMG_WORK_DIR") or "").strip()
    if work_dir:
        candidate = Path(work_dir).expanduser() / CATALOG_DIR / CATALOG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _normalize_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    directory = str(raw.get("dir") or "").strip()
    if not directory:
        return None
    return {
        "name": str(raw.get("name") or "").strip(),
        "dataset_id": (str(raw.get("dataset_id")).strip() if raw.get("dataset_id") else None),
        "owner_id": str(raw.get("owner_id") or ""),
        "owner_name": str(raw.get("owner_name") or ""),
        "shared": bool(raw.get("shared")),
        "selected": bool(raw.get("selected")),
        "file_count": int(raw.get("file_count") or 0),
        "size_bytes": int(raw.get("size_bytes") or 0),
        "dir": directory,
    }


def _load_catalog(explicit: str | Path | None = None) -> list[dict[str, Any]] | None:
    path = _catalog_file(explicit)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"数据集清单无法解析：{path}（{exc}）") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("datasets"), list):
        raise DatasetError(f"数据集清单格式不正确：{path}")
    entries = []
    for raw in payload["datasets"]:
        entry = _normalize_entry(raw)
        if entry is not None:
            entries.append(entry)
    return entries


def _selected_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    selected = [entry for entry in entries if entry.get("selected")]
    return selected[0] if selected else None


def _reference(entry: dict[str, Any]) -> str:
    if entry.get("shared") and entry.get("owner_id"):
        return f"{entry['owner_id']}/{entry['name']}"
    return entry["name"]


def _available_names(entries: list[dict[str, Any]], limit: int = 10) -> str:
    names = []
    for entry in entries:
        label = _reference(entry)
        owner = entry.get("owner_name") or entry.get("owner_id")
        names.append(f"{label}（{owner}）" if entry.get("shared") and owner else label)
    shown = "、".join(names[:limit])
    return shown + ("……" if len(names) > limit else "")


def _resolve(entries: list[dict[str, Any]], name: str | None) -> dict[str, Any]:
    target = str(name).strip() if name is not None else ""
    if not target:
        entry = _selected_entry(entries)
        if entry is None:
            raise DatasetError("当前执行没有选中的数据集")
        return entry

    # 共享数据集：customer_id/数据集名称
    if "/" in target:
        owner_id, _, dataset_name = target.partition("/")
        qualified = [
            entry
            for entry in entries
            if entry.get("owner_id") == owner_id and entry["name"] == dataset_name
        ]
        if qualified:
            if len(qualified) == 1:
                return qualified[0]
            raise DatasetError(
                f"数据集引用不唯一：{target}（同一客户下有同名的 {len(qualified)} 个数据集）"
            )

    matches = [entry for entry in entries if entry["name"] == target]
    own = [entry for entry in matches if not entry.get("shared")]
    selected = [entry for entry in own if entry.get("selected")]
    if len(selected) == 1:
        return selected[0]
    if len(own) == 1:
        return own[0]
    if own:
        candidates = "、".join(_reference(entry) for entry in own)
        raise DatasetError(
            f"数据集名称不唯一：{target} 匹配到 {len(own)} 个自己的数据集（{candidates}）"
        )

    shared = [entry for entry in matches if entry.get("shared")]
    if shared:
        references = "、".join(_reference(entry) for entry in shared)
        raise DatasetError(
            f"数据集 {target} 属于其他客户；共享数据集请用 'customer_id/数据集名称' 引用，"
            f"例如：{references}"
        )

    available = _available_names(entries)
    suffix = f"；可用数据集：{available}" if available else "；当前没有可见数据集"
    raise DatasetError(f"数据集不存在：{target}{suffix}")


def _fallback_dir(name: str | None, *, required: bool) -> str | None:
    target = str(name).strip() if name is not None else ""
    directory = (os.environ.get("WMG_DATASET_DIR") or os.environ.get("DATASET_DIR") or "").strip()
    if not directory:
        if required:
            raise DatasetError("当前执行没有数据集目录（WMG_DATASET_DIR 未设置）")
        return None
    selected_name = (os.environ.get("WMG_DATASET_NAME") or "").strip()
    if target and target != selected_name:
        if not selected_name:
            raise DatasetError(
                "当前执行没有数据集清单，无法按名称解析数据集；请直接使用 WMG_DATASET_DIR"
            )
        raise DatasetError(f"当前执行没有数据集清单，只能读取已挂载的数据集：{selected_name}")
    path = Path(directory).expanduser()
    if not path.is_dir():
        if required:
            raise DatasetError(f"数据集目录不存在：{path}")
        return None
    return str(path)


def _fallback_entry(name: str | None) -> dict[str, Any]:
    directory = _fallback_dir(name, required=True)
    assert directory is not None
    selected_name = (os.environ.get("WMG_DATASET_NAME") or "").strip()
    return {
        "name": selected_name or "dataset",
        "dataset_id": (os.environ.get("WMG_DATASET_ID") or "").strip() or None,
        "owner_id": (os.environ.get("WMG_CUSTOMER_ID") or "").strip(),
        "owner_name": "",
        "shared": False,
        "selected": True,
        "file_count": 0,
        "size_bytes": 0,
        "dir": directory,
    }


def _entry_for(name: str | None, catalog: str | Path | None) -> dict[str, Any]:
    entries = _load_catalog(catalog)
    if entries is None:
        return _fallback_entry(name)
    return _resolve(entries, name)


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result = dict(entry)
    result["path"] = result.pop("dir")
    result["reference"] = _reference(entry)
    return result


def _split_token(split: str) -> re.Pattern[str]:
    return re.compile(rf"(^|[^a-z0-9]){re.escape(split)}([^a-z0-9]|$)", re.IGNORECASE)


def _read_json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"{label} 无法解析：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise DatasetError(f"{label} 顶层必须是对象：{path}")
    return payload


def _find_split_file(root: Path, split: str, manifest: dict[str, Any]) -> Path | None:
    exact = root / f"{split}.jsonl"
    if exact.is_file():
        return exact

    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, dict):
        pattern = _split_token(split)
        for filename in artifacts:
            if not isinstance(filename, str) or not filename.lower().endswith(".jsonl"):
                continue
            if not pattern.search(Path(filename).stem):
                continue
            candidate = root / filename
            if candidate.is_file():
                return candidate

    pattern = _split_token(split)
    matches = [
        path
        for path in sorted(root.glob("*.jsonl"))
        if path.is_file() and pattern.search(path.stem)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = "、".join(path.name for path in matches)
        raise DatasetError(f"数据集中有多个 {split} JSONL 文件，无法确定使用哪一个：{names}")

    nested = [
        path
        for path in sorted(root.rglob("*.jsonl"))
        if path.is_file() and pattern.search(path.stem)
    ]
    if len(nested) == 1:
        return nested[0]
    if len(nested) > 1:
        names = "、".join(str(path.relative_to(root)) for path in nested)
        raise DatasetError(f"数据集中有多个 {split} JSONL 文件，无法确定使用哪一个：{names}")
    return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"无法读取数据集文件：{path}（{exc}）") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(
                    f"数据集文件 {path.name} 第 {line_number} 行不是合法 JSON：{exc}"
                ) from exc
            if not isinstance(row, dict):
                raise DatasetError(f"数据集文件 {path.name} 第 {line_number} 行不是 JSON 对象")
            records.append(row)
    return records


def _load_groups(root: Path) -> dict[str, list[str]]:
    path = root / "groups.json"
    if not path.is_file():
        candidates = [item for item in root.rglob("groups.json") if item.is_file()]
        if len(candidates) != 1:
            return {}
        path = candidates[0]
    payload = _read_json_mapping(path, "groups.json")
    groups: dict[str, list[str]] = {}
    for split, values in payload.items():
        if isinstance(values, list):
            groups[str(split)] = [str(value) for value in values]
    return groups


def _normalize_splits(splits: str | Iterable[str] | None) -> list[str]:
    if splits is None:
        return list(DEFAULT_SPLITS)
    if isinstance(splits, str):
        return [splits]
    names = [str(item) for item in splits]
    if not names:
        raise DatasetError("splits 不能为空")
    return names


def dataset_path(
    name: str | None = None,
    *,
    catalog: str | Path | None = None,
    required: bool = True,
) -> str | None:
    """按名称返回数据集目录（绝对路径）。

    - ``name=None``/空串：当前 Notebook/任务选中的数据集的目录；
    - 共享数据集用 ``customer_id/数据集名称`` 引用（如 ``c_ab12cd34/my-set``）；
    - ``catalog``：显式指定清单文件（缺省读 ``WMG_DATASET_CATALOG``）；
    - ``required=False``：名称找不到时返回 ``None``（目录不可用仍会报错）。
    """
    entries = _load_catalog(catalog)
    if entries is None:
        return _fallback_dir(name, required=required)
    try:
        entry = _resolve(entries, name)
    except DatasetError:
        if required:
            raise
        return None
    directory = Path(entry["dir"]).expanduser()
    if not directory.is_dir():
        raise DatasetError(
            f"数据集目录不可用：{entry['name']} → {directory}"
            "（Docker 沙箱下新增或共享的数据集需要重启内核后才会挂载）"
        )
    return str(directory)


def dataset_info(
    name: str | None = None,
    *,
    catalog: str | Path | None = None,
) -> dict[str, Any]:
    """按名称返回数据集元信息（含 ``path``、所有者、引用与共享标记）。"""
    return _public_entry(_entry_for(name, catalog))


def list_datasets(catalog: str | Path | None = None) -> list[dict[str, Any]]:
    """列出当前执行可见的数据集（自己的 + 其他客户共享的）。"""
    entries = _load_catalog(catalog)
    if entries is None:
        try:
            return [_public_entry(_fallback_entry(None))]
        except DatasetError:
            return []
    return [_public_entry(entry) for entry in entries]


def load_dataset(
    name: str | None = None,
    *,
    splits: str | Iterable[str] | None = None,
    catalog: str | Path | None = None,
) -> Dataset:
    """按名称加载数据集，返回 ``Dataset``（manifest / groups / 各 split 记录）。

    - ``name``：自己的数据集名称，或共享数据集的 ``customer_id/数据集名称``；
      省略时读取当前 Notebook/任务选中的数据集；
    - ``splits``：要加载的 split，默认 ``("train", "valid", "test")``；
      数据集中不存在的 split 会被跳过（不报错）；
    - 记录按行解析为 JSON 对象（WMG 数据集通常是 ``fields`` / ``answers`` 结构，
      历史数据也可能是 ``messages`` 结构，加载器原样返回，不做格式转换）。

    典型用法::

        dataset = load_dataset("wmg-mail-v3full-v1")
        train_records, test_records = dataset.train, dataset.test

        shared = load_dataset("c_ab12cd34/wmg-mail-v3full-v1")
    """
    entry = _entry_for(name, catalog)
    root = Path(entry["dir"]).expanduser()
    if not root.is_dir():
        raise DatasetError(
            f"数据集目录不可用：{entry['name']} → {root}"
            "（Docker 沙箱下新增或共享的数据集需要重启内核后才会挂载）"
        )

    manifest_path = root / "dataset_manifest.json"
    manifest = (
        _read_json_mapping(manifest_path, "dataset_manifest.json")
        if manifest_path.is_file()
        else {}
    )
    groups = _load_groups(root)

    records: dict[str, list[dict[str, Any]]] = {}
    files: dict[str, str] = {}
    requested = _normalize_splits(splits)
    for split in requested:
        path = _find_split_file(root, split, manifest)
        if path is None:
            continue
        records[split] = _read_jsonl(path)
        files[split] = str(path)
    if not records:
        raise DatasetError(
            f"数据集 {entry['name']} 中没有找到可读取的 JSONL 文件"
            f"（查找 split：{', '.join(requested)}）：{root}"
        )

    return Dataset(
        name=entry["name"],
        path=str(root),
        owner_id=entry.get("owner_id") or "",
        owner_name=entry.get("owner_name") or "",
        shared=bool(entry.get("shared")),
        manifest=manifest,
        groups=groups,
        splits=records,
        files=files,
    )
