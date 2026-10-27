from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .utils import parse_iso


def customer_public(row: dict[str, Any], *, include_token: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "email": row.get("email"),
        "status": row["status"],
        "createdAt": row["created_at"],
        "lastSeenAt": row.get("last_seen_at"),
    }
    if include_token:
        data["token"] = row["token"]
    if "dataset_count" in row:
        data["datasetCount"] = row["dataset_count"]
        data["notebookCount"] = row.get("notebook_count", 0)
        data["runCount"] = row.get("run_count", 0)
    return data


def dataset_public(
    row: dict[str, Any],
    files: list[dict[str, Any]] | None = None,
    *,
    viewer_customer_id: str | None = None,
) -> dict[str, Any]:
    owner_id = row.get("customer_id")
    data = {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description"),
        "status": row["status"],
        "sizeBytes": row.get("size_bytes", 0),
        "fileCount": row.get("file_count", 0),
        "shared": (row.get("visibility") or "private") == "shared",
        "ownerId": owner_id,
        "ownerName": row.get("owner_name"),
        "createdAt": row["created_at"],
    }
    if viewer_customer_id is not None:
        data["isOwner"] = owner_id == viewer_customer_id
    if files is not None:
        data["files"] = [
            {"filename": item["filename"], "sizeBytes": item["size_bytes"]} for item in files
        ]
    return data


def notebook_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "filename": row["filename"],
        "sizeBytes": row.get("size_bytes", 0),
        "cellCount": row.get("cell_count", 0),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def run_public(row: dict[str, Any], *, has_logs: bool | None = None) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if row.get("parameters"):
        try:
            parameters = json.loads(row["parameters"])
        except (TypeError, ValueError):
            parameters = {}
    duration: float | None = None
    started = parse_iso(row.get("started_at"))
    if started is not None:
        finished = parse_iso(row.get("finished_at")) or datetime.now(timezone.utc)
        duration = max(0.0, (finished - started).total_seconds())
    data = {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "kernel": row.get("kernel"),
        "notebookId": row.get("notebook_id"),
        "notebookName": row.get("notebook_name"),
        "datasetId": row.get("dataset_id"),
        "datasetName": row.get("dataset_name"),
        "datasetOwnerId": row.get("dataset_owner_id"),
        "datasetOwnerName": (
            row.get("dataset_owner_name")
            if row.get("dataset_owner_id") and row.get("dataset_owner_id") != row.get("customer_id")
            else None
        ),
        "datasetShared": bool(
            row.get("dataset_owner_id") and row.get("dataset_owner_id") != row.get("customer_id")
        ),
        "parameters": parameters,
        "timeoutSeconds": row.get("timeout_seconds"),
        "exitCode": row.get("exit_code"),
        "message": row.get("message"),
        "createdAt": row["created_at"],
        "startedAt": row.get("started_at"),
        "finishedAt": row.get("finished_at"),
        "durationSeconds": duration,
    }
    if has_logs is not None:
        data["hasLogs"] = has_logs
    return data


def activity_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "action": row["action"],
        "targetType": row.get("target_type"),
        "targetId": row.get("target_id"),
        "detail": row.get("detail"),
        "ip": row.get("ip"),
        "createdAt": row["created_at"],
    }
