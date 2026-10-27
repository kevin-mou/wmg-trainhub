from __future__ import annotations

import sqlite3
from pathlib import Path

from trainhub.db import Database


def test_visibility_column_is_added_to_existing_database(tmp_path: Path):
    """存量库升级：旧 datasets 表没有 visibility，打开后应自动补列且默认 private。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE customers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            token TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_seen_at TEXT
        );
        CREATE TABLE datasets (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        INSERT INTO customers (id, name, token, status, created_at)
        VALUES ('c_old', 'Old Co', 'trh_old_token', 'active', '2026-01-01T00:00:00+00:00');
        INSERT INTO datasets (id, customer_id, name, status, size_bytes, file_count, created_at)
        VALUES ('ds_old', 'c_old', 'legacy', 'ready', 1, 1, '2026-01-01T00:00:00+00:00');
        """)
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        dataset = db.get_dataset("c_old", "ds_old")
        assert dataset is not None
        assert dataset["visibility"] == "private"
        # 旧数据默认不共享；显式切换后其他客户可见
        assert db.get_accessible_dataset("c_other", "ds_old") is None
        db.set_dataset_visibility("c_old", "ds_old", "shared")
        assert db.get_accessible_dataset("c_other", "ds_old") is not None
    finally:
        db.close()
