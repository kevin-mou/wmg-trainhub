from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .utils import utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_datasets_customer ON datasets(customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS dataset_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dataset_files_dataset ON dataset_files(dataset_id);

CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    cell_count INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notebooks_customer ON notebooks(customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    notebook_id TEXT,
    notebook_name TEXT,
    dataset_id TEXT,
    dataset_name TEXT,
    dataset_owner_id TEXT,
    dataset_owner_name TEXT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    kernel TEXT,
    parameters TEXT,
    timeout_seconds INTEGER,
    exit_code INTEGER,
    message TEXT,
    log_path TEXT,
    output_path TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    origin TEXT NOT NULL DEFAULT 'customer',
    control_job_id TEXT,
    code_archive_path TEXT,
    code_sha256 TEXT,
    entry TEXT,
    dataset_archive_path TEXT,
    dataset_sha256 TEXT,
    assets_archive_path TEXT,
    assets_sha256 TEXT,
    base_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_customer ON runs(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_notebook ON runs(customer_id, notebook_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    detail TEXT,
    ip TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_customer ON activity_logs(customer_id, created_at DESC);
"""

TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")


class Database:
    """单文件 SQLite 数据访问层。

    单实例部署：一个连接 + 进程内锁，足以满足并发写入（WAL 模式）。
    """

    # 平台控制面推送的执行任务统一挂在这个保留客户下：不参与客户列表与令牌登录，
    # 仅用于满足 runs.customer_id 的外键约束（TrainHub 不管理控制面的版本语义）。
    SYSTEM_CUSTOMER_ID = "sys_platform"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_schema()
        self.ensure_system_customer()

    def _migrate_schema(self) -> None:
        """存量库补列（新库由 _SCHEMA 直接建出）；重复执行安全。"""
        for table, name, sql_type in (
            ("runs", "origin", "TEXT NOT NULL DEFAULT 'customer'"),
            ("runs", "control_job_id", "TEXT"),
            ("runs", "code_archive_path", "TEXT"),
            ("runs", "code_sha256", "TEXT"),
            ("runs", "entry", "TEXT"),
            ("runs", "dataset_archive_path", "TEXT"),
            ("runs", "dataset_sha256", "TEXT"),
            ("runs", "assets_archive_path", "TEXT"),
            ("runs", "assets_sha256", "TEXT"),
            ("runs", "base_model", "TEXT"),
            ("runs", "dataset_owner_id", "TEXT"),
            ("runs", "dataset_owner_name", "TEXT"),
            ("datasets", "visibility", "TEXT NOT NULL DEFAULT 'private'"),
        ):
            try:
                self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            except sqlite3.OperationalError:
                continue

    def ensure_system_customer(self) -> None:
        existing = self._query_one(
            "SELECT id FROM customers WHERE id = ?", (self.SYSTEM_CUSTOMER_ID,)
        )
        if existing:
            return
        self._execute(
            "INSERT INTO customers (id, name, email, token, status, created_at) "
            "VALUES (?, ?, NULL, ?, 'active', ?)",
            (self.SYSTEM_CUSTOMER_ID, "平台执行任务", "sys_" + secrets.token_hex(16), utc_now()),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ customers

    def create_customer(self, name: str, email: str | None, token: str) -> dict[str, Any]:
        now = utc_now()
        for _ in range(5):
            customer_id = "c_" + secrets.token_hex(4)
            try:
                self._execute(
                    "INSERT INTO customers (id, name, email, token, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?)",
                    (customer_id, name, email, token, now),
                )
            except sqlite3.IntegrityError:
                continue
            return self.get_customer(customer_id)  # type: ignore[return-value]
        raise RuntimeError("无法生成唯一的客户 ID，请重试")

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        return self._query_one("SELECT * FROM customers WHERE id = ?", (customer_id,))

    def get_customer_by_token(self, token: str) -> dict[str, Any] | None:
        return self._query_one("SELECT * FROM customers WHERE token = ?", (token,))

    def list_customers(
        self, page: int, size: int, q: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["c.id <> ?"]
        params: list[Any] = [self.SYSTEM_CUSTOMER_ID]
        if q:
            clauses.append("(c.name LIKE ? OR c.id LIKE ? OR COALESCE(c.email, '') LIKE ?)")
            like = f"%{q.strip()}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}"
        total_row = self._query_one(f"SELECT COUNT(*) AS n FROM customers c {where}", params)
        total = int(total_row["n"]) if total_row else 0
        rows = self._query(
            f"""
            SELECT c.*,
                   (SELECT COUNT(*) FROM datasets d WHERE d.customer_id = c.id) AS dataset_count,
                   (SELECT COUNT(*) FROM notebooks n WHERE n.customer_id = c.id) AS notebook_count,
                   (SELECT COUNT(*) FROM runs r WHERE r.customer_id = c.id) AS run_count
            FROM customers c
            {where}
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, size, (page - 1) * size],
        )
        return rows, total

    def update_customer_status(self, customer_id: str, status: str) -> dict[str, Any] | None:
        self._execute("UPDATE customers SET status = ? WHERE id = ?", (status, customer_id))
        return self.get_customer(customer_id)

    def update_customer_token(self, customer_id: str, token: str) -> dict[str, Any] | None:
        self._execute("UPDATE customers SET token = ? WHERE id = ?", (token, customer_id))
        return self.get_customer(customer_id)

    def delete_customer(self, customer_id: str) -> dict[str, Any] | None:
        customer = self.get_customer(customer_id)
        if customer is None:
            return None
        self._execute("DELETE FROM activity_logs WHERE customer_id = ?", (customer_id,))
        self._execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        return customer

    def touch_customer(self, customer_id: str) -> None:
        """节流更新 last_seen_at：最多每分钟落一次写。"""
        threshold = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(
            timespec="seconds"
        )
        self._execute(
            "UPDATE customers SET last_seen_at = ? WHERE id = ? "
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (utc_now(), customer_id, threshold),
        )

    # ------------------------------------------------------------------ datasets

    def create_dataset_with_files(
        self,
        *,
        customer_id: str,
        name: str,
        description: str | None,
        files: Iterable[tuple[str, int, str]],
        size_bytes: int,
        file_count: int,
        dataset_id: str | None = None,
        visibility: str = "private",
    ) -> dict[str, Any]:
        dataset_id = dataset_id or ("ds_" + secrets.token_hex(6))
        now = utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO datasets "
                "(id, customer_id, name, description, status, size_bytes, file_count, "
                " visibility, created_at) "
                "VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?)",
                (
                    dataset_id,
                    customer_id,
                    name,
                    description,
                    size_bytes,
                    file_count,
                    visibility,
                    now,
                ),
            )
            self._conn.executemany(
                "INSERT INTO dataset_files (dataset_id, filename, size_bytes, storage_path) "
                "VALUES (?, ?, ?, ?)",
                [(dataset_id, filename, size, path) for filename, size, path in files],
            )
            self._conn.commit()
        return self.get_accessible_dataset(customer_id, dataset_id)  # type: ignore[return-value]

    def get_dataset(self, customer_id: str, dataset_id: str) -> dict[str, Any] | None:
        """仅按所有者读取（删除 / 共享开关等 owner 操作使用）。"""
        return self._query_one(
            "SELECT * FROM datasets WHERE id = ? AND customer_id = ?",
            (dataset_id, customer_id),
        )

    def get_accessible_dataset(self, customer_id: str, dataset_id: str) -> dict[str, Any] | None:
        """当前客户可见的数据集：自己的，或任何客户共享出来的。"""
        return self._query_one(
            "SELECT d.*, c.name AS owner_name "
            "FROM datasets d JOIN customers c ON c.id = d.customer_id "
            "WHERE d.id = ? AND (d.customer_id = ? OR d.visibility = 'shared')",
            (dataset_id, customer_id),
        )

    def set_dataset_visibility(
        self, customer_id: str, dataset_id: str, visibility: str
    ) -> dict[str, Any] | None:
        cursor = self._execute(
            "UPDATE datasets SET visibility = ? WHERE id = ? AND customer_id = ?",
            (visibility, dataset_id, customer_id),
        )
        if cursor.rowcount != 1:
            return None
        return self.get_accessible_dataset(customer_id, dataset_id)

    def get_dataset_files(self, dataset_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM dataset_files WHERE dataset_id = ? ORDER BY filename",
            (dataset_id,),
        )

    def list_datasets(
        self,
        customer_id: str,
        page: int,
        size: int,
        q: str | None = None,
        *,
        include_shared: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        if include_shared:
            where = ["(d.customer_id = ? OR d.visibility = 'shared')"]
        else:
            where = ["d.customer_id = ?"]
        params: list[Any] = [customer_id]
        if q:
            where.append("(d.name LIKE ? OR COALESCE(d.description, '') LIKE ? OR c.name LIKE ?)")
            like = f"%{q.strip()}%"
            params.extend([like, like, like])
        clause = " AND ".join(where)
        total_row = self._query_one(
            "SELECT COUNT(*) AS n FROM datasets d "
            "JOIN customers c ON c.id = d.customer_id "
            f"WHERE {clause}",
            params,
        )
        total = int(total_row["n"]) if total_row else 0
        rows = self._query(
            "SELECT d.*, c.name AS owner_name FROM datasets d "
            "JOIN customers c ON c.id = d.customer_id "
            f"WHERE {clause} "
            "ORDER BY (d.customer_id = ?) DESC, d.created_at DESC, d.id DESC "
            "LIMIT ? OFFSET ?",
            [*params, customer_id, size, (page - 1) * size],
        )
        return rows, total

    def delete_dataset(self, customer_id: str, dataset_id: str) -> dict[str, Any] | None:
        dataset = self.get_dataset(customer_id, dataset_id)
        if dataset is None:
            return None
        self._execute(
            "DELETE FROM datasets WHERE id = ? AND customer_id = ?", (dataset_id, customer_id)
        )
        return dataset

    # ------------------------------------------------------------------ notebooks

    def create_notebook(
        self,
        *,
        customer_id: str,
        name: str,
        filename: str,
        size_bytes: int,
        cell_count: int,
        storage_path: str,
        notebook_id: str | None = None,
    ) -> dict[str, Any]:
        notebook_id = notebook_id or ("nb_" + secrets.token_hex(6))
        now = utc_now()
        self._execute(
            "INSERT INTO notebooks "
            "(id, customer_id, name, filename, size_bytes, cell_count, storage_path, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                notebook_id,
                customer_id,
                name,
                filename,
                size_bytes,
                cell_count,
                storage_path,
                now,
                now,
            ),
        )
        return self.get_notebook(customer_id, notebook_id)  # type: ignore[return-value]

    def get_notebook(self, customer_id: str, notebook_id: str) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM notebooks WHERE id = ? AND customer_id = ?",
            (notebook_id, customer_id),
        )

    def get_notebook_any(self, notebook_id: str | None) -> dict[str, Any] | None:
        if not notebook_id:
            return None
        return self._query_one("SELECT * FROM notebooks WHERE id = ?", (notebook_id,))

    def list_notebooks(
        self, customer_id: str, page: int, size: int, q: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        where = ["customer_id = ?"]
        params: list[Any] = [customer_id]
        if q:
            where.append("(name LIKE ? OR filename LIKE ?)")
            like = f"%{q.strip()}%"
            params.extend([like, like])
        clause = " AND ".join(where)
        total_row = self._query_one(f"SELECT COUNT(*) AS n FROM notebooks WHERE {clause}", params)
        total = int(total_row["n"]) if total_row else 0
        rows = self._query(
            f"SELECT * FROM notebooks WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, size, (page - 1) * size],
        )
        return rows, total

    def delete_notebook(self, customer_id: str, notebook_id: str) -> dict[str, Any] | None:
        notebook = self.get_notebook(customer_id, notebook_id)
        if notebook is None:
            return None
        self._execute(
            "DELETE FROM notebooks WHERE id = ? AND customer_id = ?", (notebook_id, customer_id)
        )
        return notebook

    # ------------------------------------------------------------------ runs

    def create_run(
        self,
        *,
        customer_id: str,
        notebook: dict[str, Any],
        dataset: dict[str, Any] | None,
        name: str,
        kernel: str | None,
        parameters_json: str | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        run_id = "tr_" + secrets.token_hex(6)
        now = utc_now()
        self._execute(
            "INSERT INTO runs "
            "(id, customer_id, notebook_id, notebook_name, dataset_id, dataset_name, "
            " dataset_owner_id, dataset_owner_name, name, "
            " status, kernel, parameters, timeout_seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                run_id,
                customer_id,
                notebook["id"],
                notebook.get("name"),
                dataset["id"] if dataset else None,
                dataset.get("name") if dataset else None,
                dataset["customer_id"] if dataset else None,
                dataset.get("owner_name") if dataset else None,
                name,
                kernel,
                parameters_json,
                timeout_seconds,
                now,
            ),
        )
        return self.get_run_any(run_id)  # type: ignore[return-value]

    def create_execution_run(
        self,
        *,
        run_id: str,
        name: str,
        kernel: str | None,
        parameters_json: str | None,
        timeout_seconds: int,
        control_job_id: str | None,
        code_archive_path: str,
        code_sha256: str,
        entry: str | None,
        dataset_archive_path: str | None,
        dataset_sha256: str | None,
        assets_archive_path: str | None,
        assets_sha256: str | None,
        base_model: str | None,
    ) -> dict[str, Any]:
        """平台控制面推送的闭环执行（不涉及 notebook 版本语义）。"""
        now = utc_now()
        self._execute(
            "INSERT INTO runs "
            "(id, customer_id, notebook_id, notebook_name, dataset_id, dataset_name, name, "
            " status, kernel, parameters, timeout_seconds, created_at, origin, control_job_id, "
            " code_archive_path, code_sha256, entry, dataset_archive_path, dataset_sha256, "
            " assets_archive_path, assets_sha256, base_model) "
            "VALUES (?, ?, NULL, ?, NULL, NULL, ?, 'queued', ?, ?, ?, ?, 'platform', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                self.SYSTEM_CUSTOMER_ID,
                Path(code_archive_path).name,
                name,
                kernel,
                parameters_json,
                timeout_seconds,
                now,
                control_job_id or None,
                code_archive_path,
                code_sha256,
                entry or None,
                dataset_archive_path,
                dataset_sha256,
                assets_archive_path,
                assets_sha256,
                base_model or None,
            ),
        )
        return self.get_run_any(run_id)  # type: ignore[return-value]

    def global_run_status_counts(self) -> dict[str, int]:
        rows = self._query(
            "SELECT status, COUNT(*) AS n FROM runs WHERE status IN ('queued','running') "
            "GROUP BY status"
        )
        counts = {"queued": 0, "running": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def list_active_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._query(
            "SELECT id, name, status, origin, control_job_id, started_at, created_at "
            "FROM runs WHERE status IN ('queued','running') ORDER BY created_at LIMIT ?",
            (int(limit),),
        )

    def get_run(self, customer_id: str, run_id: str) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM runs WHERE id = ? AND customer_id = ?", (run_id, customer_id)
        )

    def get_run_any(self, run_id: str) -> dict[str, Any] | None:
        return self._query_one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def latest_run_for_notebook(self, customer_id: str, notebook_id: str) -> dict[str, Any] | None:
        """该 Notebook 最近一次客户运行：只用于提交表单回填，不进入平台策略。"""
        return self._query_one(
            "SELECT * FROM runs WHERE customer_id = ? AND notebook_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (customer_id, notebook_id),
        )

    def list_runs(
        self,
        customer_id: str,
        page: int,
        size: int,
        status: str | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where = ["customer_id = ?"]
        params: list[Any] = [customer_id]
        if status:
            where.append("status = ?")
            params.append(status)
        if q:
            where.append("(name LIKE ? OR COALESCE(notebook_name, '') LIKE ?)")
            like = f"%{q.strip()}%"
            params.extend([like, like])
        clause = " AND ".join(where)
        total_row = self._query_one(f"SELECT COUNT(*) AS n FROM runs WHERE {clause}", params)
        total = int(total_row["n"]) if total_row else 0
        rows = self._query(
            f"SELECT * FROM runs WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, size, (page - 1) * size],
        )
        return rows, total

    def list_runs_admin(
        self,
        page: int,
        size: int,
        status: str | None = None,
        customer_id: str | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """平台管理面：跨客户列出训练任务（带客户名）。"""
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("r.status = ?")
            params.append(status)
        if customer_id:
            where.append("r.customer_id = ?")
            params.append(customer_id)
        if q:
            like = f"%{q.strip()}%"
            where.append(
                "(r.name LIKE ? OR COALESCE(r.notebook_name, '') LIKE ? "
                "OR c.name LIKE ? OR c.id LIKE ?)"
            )
            params.extend([like, like, like, like])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        base = "FROM runs r JOIN customers c ON c.id = r.customer_id"
        total_row = self._query_one(f"SELECT COUNT(*) AS n {base} {clause}", params)
        total = int(total_row["n"]) if total_row else 0
        rows = self._query(
            f"SELECT r.*, c.name AS customer_name, c.email AS customer_email {base} {clause} "
            "ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
            [*params, size, (page - 1) * size],
        )
        return rows, total

    def list_queued_run_ids(self) -> list[str]:
        rows = self._query("SELECT id FROM runs WHERE status = 'queued' ORDER BY created_at, id")
        return [row["id"] for row in rows]

    def mark_run_running(self, run_id: str) -> bool:
        cursor = self._execute(
            "UPDATE runs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (utc_now(), run_id),
        )
        return cursor.rowcount == 1

    def cancel_queued_run(self, run_id: str) -> bool:
        cursor = self._execute(
            "UPDATE runs SET status = 'cancelled', finished_at = ?, message = '训练已取消' "
            "WHERE id = ? AND status = 'queued'",
            (utc_now(), run_id),
        )
        return cursor.rowcount == 1

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        exit_code: int | None,
        message: str | None,
        log_path: str | None,
        output_path: str | None,
    ) -> None:
        self._execute(
            "UPDATE runs SET status = ?, exit_code = ?, message = ?, log_path = ?, "
            "output_path = ?, finished_at = ? WHERE id = ?",
            (status, exit_code, message, log_path, output_path, utc_now(), run_id),
        )

    def recover_interrupted_runs(self, message: str) -> int:
        cursor = self._execute(
            "UPDATE runs SET status = 'failed', exit_code = 1, message = ?, finished_at = ? "
            "WHERE status = 'running'",
            (message, utc_now()),
        )
        return cursor.rowcount

    def delete_run(self, customer_id: str, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(customer_id, run_id)
        if run is None:
            return None
        self._execute("DELETE FROM runs WHERE id = ? AND customer_id = ?", (run_id, customer_id))
        return run

    def count_active_runs(
        self,
        *,
        customer_id: str | None = None,
        notebook_id: str | None = None,
        dataset_id: str | None = None,
    ) -> int:
        clauses = ["status IN ('queued', 'running')"]
        params: list[Any] = []
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(customer_id)
        if notebook_id:
            clauses.append("notebook_id = ?")
            params.append(notebook_id)
        if dataset_id:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        row = self._query_one(
            f"SELECT COUNT(*) AS n FROM runs WHERE {' AND '.join(clauses)}", params
        )
        return int(row["n"]) if row else 0

    def run_status_counts(self, customer_id: str) -> dict[str, int]:
        rows = self._query(
            "SELECT status, COUNT(*) AS n FROM runs WHERE customer_id = ? GROUP BY status",
            (customer_id,),
        )
        return {row["status"]: int(row["n"]) for row in rows}

    def recent_runs(self, customer_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM runs WHERE customer_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (customer_id, limit),
        )

    def active_runs(self, customer_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM runs WHERE customer_id = ? AND status IN ('queued', 'running') "
            "ORDER BY created_at",
            (customer_id,),
        )

    def total_bytes(self, customer_id: str) -> int:
        row = self._query_one(
            "SELECT "
            " (SELECT COALESCE(SUM(size_bytes), 0) FROM datasets WHERE customer_id = ?) + "
            " (SELECT COALESCE(SUM(size_bytes), 0) FROM notebooks WHERE customer_id = ?) AS n",
            (customer_id, customer_id),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ activity

    def add_activity(
        self,
        customer_id: str,
        action: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: str | None = None,
        ip: str | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO activity_logs (customer_id, action, target_type, target_id, detail, ip, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                action,
                target_type,
                target_id,
                (detail or "")[:500] or None,
                ip,
                utc_now(),
            ),
        )

    def list_activity(
        self, customer_id: str, page: int, size: int
    ) -> tuple[list[dict[str, Any]], int]:
        total_row = self._query_one(
            "SELECT COUNT(*) AS n FROM activity_logs WHERE customer_id = ?", (customer_id,)
        )
        total = int(total_row["n"]) if total_row else 0
        rows = self._query(
            "SELECT * FROM activity_logs WHERE customer_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (customer_id, size, (page - 1) * size),
        )
        return rows, total

    # ------------------------------------------------------------------ admin

    def admin_overview(self) -> dict[str, Any]:
        def count(table: str) -> int:
            row = self._query_one(f"SELECT COUNT(*) AS n FROM {table}")
            return int(row["n"]) if row else 0

        active_runs = self._query_one(
            "SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued', 'running')"
        )
        customers = self._query_one(
            "SELECT COUNT(*) AS n FROM customers WHERE id <> ?", (self.SYSTEM_CUSTOMER_ID,)
        )
        return {
            "customers": int(customers["n"]) if customers else 0,
            "datasets": count("datasets"),
            "notebooks": count("notebooks"),
            "runs": count("runs"),
            "activeRuns": int(active_runs["n"]) if active_runs else 0,
        }
