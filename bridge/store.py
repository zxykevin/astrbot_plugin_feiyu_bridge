from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class TaskStore:
    """Small synchronous SQLite repository used from AstrBot's event loop.

    Writes are short, transactional, and protected because the HTTP test harness
    may invoke the service from another thread. SQLite WAL is enabled for file
    databases; tests can use ``:memory:``.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    capability TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    task_content TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    trusted_qq_id TEXT NOT NULL,
                    qq_session_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    ended_at REAL,
                    deadline_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    client_id TEXT,
                    dsh_session_id TEXT,
                    execution_id TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    result_digest TEXT,
                    notify_on_terminal INTEGER NOT NULL DEFAULT 0,
                    notification_status TEXT NOT NULL DEFAULT 'not_required',
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    notification_next_at REAL,
                    notification_last_error TEXT,
                    notified_at REAL,
                    recovery_pending INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    UNIQUE(origin, idempotency_key)
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_owner_created "
                "ON tasks(trusted_qq_id, created_at DESC)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_created "
                "ON tasks(status, created_at DESC)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_notification "
                "ON tasks(notification_status, notification_next_at)"
            )
            self._db.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            # A process can die after claiming a notification but before send.
            self._db.execute(
                "UPDATE tasks SET notification_status='retry', "
                "notification_next_at=? WHERE notification_status='sending'",
                (time.time(),),
            )

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def loads(value: str | None) -> Any:
        return None if value is None else json.loads(value)

    def insert(self, values: dict[str, Any]) -> None:
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO tasks ({','.join(columns)}) VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )

    def update(self, task_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = time.time()
        assignments = ",".join(f"{column}=?" for column in values)
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id=?",
                (*values.values(), task_id),
            )

    def get(self, task_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()

    def get_by_idempotency(self, origin: str, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM tasks WHERE origin=? AND idempotency_key=?",
                (origin, key),
            ).fetchone()

    def all(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM tasks ORDER BY created_at"))

    def list_for_owner(
        self,
        owner: str,
        *,
        status: str | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        clauses = ["trusted_qq_id=?"]
        params: list[Any] = [owner]
        if status:
            clauses.append("status=?")
            params.append(status)
        if created_after is not None:
            clauses.append("created_at>=?")
            params.append(created_after)
        if created_before is not None:
            clauses.append("created_at<=?")
            params.append(created_before)
        params.append(max(1, min(100, int(limit))))
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT * FROM tasks WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY created_at DESC LIMIT ?",
                    tuple(params),
                )
            )

    def claim_notifications(self, *, now: float, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock, self._db:
            rows = list(
                self._db.execute(
                    "SELECT * FROM tasks WHERE notification_status IN ('pending','retry') "
                    "AND COALESCE(notification_next_at, 0)<=? "
                    "ORDER BY ended_at, created_at LIMIT ?",
                    (now, max(1, min(50, limit))),
                )
            )
            if rows:
                self._db.executemany(
                    "UPDATE tasks SET notification_status='sending', updated_at=? "
                    "WHERE task_id=? AND notification_status IN ('pending','retry')",
                    ((now, row["task_id"]) for row in rows),
                )
            return [self.get(row["task_id"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
