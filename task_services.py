#!/usr/bin/env python3
"""Task persistence and application services shared by Durex entry points."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Optional, Protocol


TASK_STATUSES = frozenset(
    {
        "PENDING",
        "RUNNING",
        "WAITING_LIMIT",
        "COMPLETED",
        "FAILED",
    }
)

TASK_COLUMNS = frozenset(
    {
        "title",
        "prompt",
        "workdir",
        "priority",
        "status",
        "session_id",
        "next_step",
        "reset_at",
        "attempts",
        "max_attempts",
        "last_error",
        "output",
        "created_at",
        "updated_at",
    }
)


@dataclass(frozen=True)
class TaskRecord(Mapping[str, object]):
    """Transport-neutral representation of one persisted task."""

    id: int
    title: str
    prompt: str
    workdir: str
    priority: int
    status: str
    session_id: Optional[str]
    next_step: Optional[str]
    reset_at: Optional[str]
    attempts: int
    max_attempts: int
    last_error: Optional[str]
    output: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRecord":
        """Build a task record from a complete SQLite task row."""

        return cls(**{field: row[field] for field in cls.__dataclass_fields__})

    def __getitem__(self, key: str) -> object:
        if key not in self.__dataclass_fields__:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)


class TaskRepositoryError(ValueError):
    """Raised when a repository operation violates the task contract."""


class TaskRepository(Protocol):
    """Persistence contract consumed by the task application service."""

    def initialize(self) -> None: ...

    def add(
        self,
        *,
        title: str,
        prompt: str,
        workdir: str,
        priority: int,
        max_attempts: int,
    ) -> int: ...

    def list_ordered(self) -> list[TaskRecord]: ...

    def next_runnable(self, now: str) -> Optional[TaskRecord]: ...

    def update(self, task_id: int, **fields: object) -> bool: ...

    def transition(
        self,
        task_id: int,
        expected_statuses: set[str] | frozenset[str],
        status: str,
        **fields: object,
    ) -> bool: ...

    def status_counts(self) -> dict[str, int]: ...

    def recent(self, limit: int) -> list[TaskRecord]: ...

    def get(self, task_id: int) -> Optional[TaskRecord]: ...

    def latest(self) -> Optional[TaskRecord]: ...


class SQLiteTaskRepository:
    """SQLite implementation of the Durex task repository."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        now: Callable[[], str],
    ) -> None:
        self._connect = connect
        self._now = now

    def initialize(self) -> None:
        """Create the task table when it does not exist."""

        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    workdir TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    session_id TEXT,
                    next_step TEXT,
                    reset_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    output TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def add(
        self,
        *,
        title: str,
        prompt: str,
        workdir: str,
        priority: int,
        max_attempts: int,
    ) -> int:
        """Insert one pending task and return its identifier."""

        now = self._now()
        with self._connect() as con:
            cursor = con.execute(
                """
                INSERT INTO tasks (
                    title, prompt, workdir, priority, status,
                    attempts, max_attempts, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
                """,
                (title, prompt, workdir, priority, max_attempts, now, now),
            )
            return int(cursor.lastrowid)

    def list_ordered(self) -> list[TaskRecord]:
        """Return all tasks in the established CLI display order."""

        return self._fetch_all(
            """
            SELECT *
            FROM tasks
            ORDER BY
                CASE status
                    WHEN 'RUNNING' THEN 1
                    WHEN 'WAITING_LIMIT' THEN 2
                    WHEN 'PENDING' THEN 3
                    WHEN 'FAILED' THEN 4
                    WHEN 'COMPLETED' THEN 5
                    ELSE 6
                END,
                priority ASC,
                id ASC
            """
        )

    def next_runnable(self, now: str) -> Optional[TaskRecord]:
        """Return the highest-priority task runnable at ``now``."""

        return self._fetch_one(
            """
            SELECT *
            FROM tasks
            WHERE status = 'PENDING'
               OR (
                    status = 'WAITING_LIMIT'
                    AND reset_at IS NOT NULL
                    AND reset_at <= ?
               )
            ORDER BY priority ASC, id ASC
            LIMIT 1
            """,
            (now,),
        )

    def update(self, task_id: int, **fields: object) -> bool:
        """Update task fields and return whether the task existed."""

        return self._update_where(task_id, fields, expected_statuses=None)

    def transition(
        self,
        task_id: int,
        expected_statuses: set[str] | frozenset[str],
        status: str,
        **fields: object,
    ) -> bool:
        """Atomically update a task only when its status is expected."""

        if not expected_statuses:
            raise TaskRepositoryError("At least one expected status is required.")
        unknown_expected = set(expected_statuses) - TASK_STATUSES
        if unknown_expected:
            raise TaskRepositoryError(f"Unknown expected task status: {sorted(unknown_expected)[0]}")
        if status not in TASK_STATUSES:
            raise TaskRepositoryError(f"Unknown target task status: {status}")
        fields["status"] = status
        return self._update_where(task_id, fields, expected_statuses=frozenset(expected_statuses))

    def status_counts(self) -> dict[str, int]:
        """Return persisted task counts grouped by status."""

        with self._connect() as con:
            rows = con.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
        return {str(status): int(count) for status, count in rows}

    def recent(self, limit: int) -> list[TaskRecord]:
        """Return the newest tasks first."""

        return self._fetch_all(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def get(self, task_id: int) -> Optional[TaskRecord]:
        """Return one task by identifier."""

        return self._fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def latest(self) -> Optional[TaskRecord]:
        """Return the most recently inserted task."""

        return self._fetch_one("SELECT * FROM tasks ORDER BY id DESC LIMIT 1")

    def _update_where(
        self,
        task_id: int,
        fields: Mapping[str, object],
        expected_statuses: Optional[frozenset[str]],
    ) -> bool:
        if not fields:
            return False
        unknown_fields = set(fields) - TASK_COLUMNS
        if unknown_fields:
            raise TaskRepositoryError(f"Unknown task field: {sorted(unknown_fields)[0]}")

        updates = dict(fields)
        updates["updated_at"] = self._now()
        columns = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        where = "id = ?"
        values.append(task_id)
        if expected_statuses is not None:
            placeholders = ", ".join("?" for _ in expected_statuses)
            where += f" AND status IN ({placeholders})"
            values.extend(sorted(expected_statuses))

        with self._connect() as con:
            cursor = con.execute(f"UPDATE tasks SET {columns} WHERE {where}", values)
            return cursor.rowcount == 1

    def _fetch_one(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> Optional[TaskRecord]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            row = con.execute(statement, parameters).fetchone()
        return TaskRecord.from_row(row) if row is not None else None

    def _fetch_all(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> list[TaskRecord]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(statement, parameters).fetchall()
        return [TaskRecord.from_row(row) for row in rows]


class TaskApplicationService:
    """Application operations shared by the CLI and Telegram adapters."""

    def __init__(self, repository: TaskRepository, now: Callable[[], str]) -> None:
        self.repository = repository
        self._now = now

    def initialize(self) -> None:
        self.repository.initialize()

    def add_task(
        self,
        title: str,
        prompt: str,
        workdir: str = ".",
        priority: int = 100,
        max_attempts: int = 3,
    ) -> int:
        self.initialize()
        return self.repository.add(
            title=title,
            prompt=prompt,
            workdir=str(Path(workdir).resolve()),
            priority=priority,
            max_attempts=max_attempts,
        )

    def list_tasks(self) -> list[TaskRecord]:
        self.initialize()
        return self.repository.list_ordered()

    def next_runnable_task(self) -> Optional[TaskRecord]:
        self.initialize()
        return self.repository.next_runnable(self._now())

    def update_task(self, task_id: int, **fields: object) -> bool:
        self.initialize()
        return self.repository.update(task_id, **fields)

    def transition_task(
        self,
        task_id: int,
        expected_statuses: set[str] | frozenset[str],
        status: str,
        **fields: object,
    ) -> bool:
        self.initialize()
        return self.repository.transition(task_id, expected_statuses, status, **fields)

    def task_counts(self) -> dict[str, int]:
        self.initialize()
        return self.repository.status_counts()

    def recent_tasks(self, limit: int) -> list[TaskRecord]:
        self.initialize()
        return self.repository.recent(limit)

    def task_detail(self, task_id: int) -> Optional[TaskRecord]:
        self.initialize()
        return self.repository.get(task_id)

    def task_output(self, task_id: Optional[int] = None) -> Optional[TaskRecord]:
        self.initialize()
        if task_id is None:
            return self.repository.latest()
        return self.repository.get(task_id)
