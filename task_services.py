#!/usr/bin/env python3
"""Task persistence and application services shared by Durex entry points."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
from typing import Optional, Protocol


TASK_STATUSES = frozenset(
    {
        "PENDING",
        "RUNNING",
        "WAITING_LIMIT",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
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
        "active_run_id",
        "lease_id",
        "lease_owner",
        "lease_epoch",
        "lease_expires_at",
        "started_at",
        "heartbeat_at",
        "last_output_at",
        "cancel_requested_at",
        "terminal_reason",
    }
)

LIVE_RUN_STATUSES = frozenset({"started", "completed", "failed", "cancelled"})
DEFAULT_LIVE_OUTPUT_MAX_CHARS = 200_000
DEFAULT_LIVE_OUTPUT_MAX_CHUNKS = 1_000
DEFAULT_LIVE_OUTPUT_RUNS_PER_TASK = 3
CLAIM_FINISH_FIELDS = frozenset(
    {
        "session_id",
        "next_step",
        "reset_at",
        "last_error",
        "output",
    }
)


@dataclass(frozen=True)
class TaskRecord:
    """Transport-neutral task with read-only ``sqlite3.Row`` access semantics."""

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
    active_run_id: Optional[str]
    lease_id: Optional[str]
    lease_owner: Optional[str]
    lease_epoch: int
    lease_expires_at: Optional[str]
    started_at: Optional[str]
    heartbeat_at: Optional[str]
    last_output_at: Optional[str]
    cancel_requested_at: Optional[str]
    terminal_reason: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRecord":
        """Build a task record from a complete SQLite task row."""

        return cls(**{field: row[field] for field in cls.field_names()})

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Return fields in their stable persisted row order."""

        return tuple(cls.__dataclass_fields__)

    def keys(self) -> list[str]:
        """Return field names like ``sqlite3.Row.keys()``."""

        return list(self.field_names())

    def values(self) -> tuple[object, ...]:
        """Return field values in persisted row order."""

        return tuple(getattr(self, field) for field in self.field_names())

    def __getitem__(self, key: str | int | slice) -> object:
        if isinstance(key, str):
            field = key.lower()
            if field not in self.__dataclass_fields__:
                raise IndexError("No item with that key")
            return getattr(self, field)
        return self.values()[key]

    def __iter__(self) -> Iterator[object]:
        return iter(self.values())

    def __len__(self) -> int:
        return len(self.field_names())


@dataclass(frozen=True)
class LiveOutputChunk:
    """One normalized output chunk addressable by runner event sequence."""

    sequence: int
    text: str
    created_at: str


@dataclass(frozen=True)
class LiveOutputPage:
    """Bounded output page and cursor metadata for one task execution."""

    run_id: str
    task_id: int
    attempt: int
    status: str
    chunks: tuple[LiveOutputChunk, ...]
    first_available_sequence: Optional[int]
    last_event_sequence: int
    dropped_through_sequence: int
    dropped_chars: int
    has_older: bool
    has_more: bool
    started_at: str
    finished_at: Optional[str]
    returncode: Optional[int]


@dataclass(frozen=True)
class TaskClaim:
    """One atomically acquired, fenced task execution lease."""

    task: TaskRecord
    worker_id: str
    lease_id: str
    run_id: str
    lease_epoch: int
    lease_expires_at: str


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

    def claim_next(
        self,
        *,
        now: str,
        lease_expires_at: str,
        worker_id: str,
        lease_id: str,
        run_id: str,
    ) -> Optional[TaskClaim]: ...

    def heartbeat_claim(
        self,
        task_id: int,
        lease_id: str,
        lease_epoch: int,
        now: str,
        lease_expires_at: str,
    ) -> bool: ...

    def finish_claim(
        self,
        task_id: int,
        lease_id: str,
        lease_epoch: int,
        status: str,
        terminal_reason: str,
        **fields: object,
    ) -> bool: ...

    def request_claim_cancellation(
        self,
        task_id: int,
        lease_id: str,
        lease_epoch: int,
        reason: str,
    ) -> bool: ...

    def recover_stale_claims(self, now: str) -> list[int]: ...

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

    def start_run(
        self,
        task_id: int,
        run_id: str,
        attempt: int,
        lease_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
    ) -> bool: ...

    def append_run_output(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        text: str,
        lease_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
    ) -> bool: ...

    def finish_run(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        status: str,
        returncode: Optional[int],
        lease_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
    ) -> bool: ...

    def read_run_output(
        self,
        task_id: int,
        run_id: Optional[str] = None,
        after_sequence: Optional[int] = None,
        before_sequence: Optional[int] = None,
        limit: int = 100,
    ) -> Optional[LiveOutputPage]: ...


class SQLiteTaskRepository:
    """SQLite implementation of the Durex task repository."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        now: Callable[[], str],
        live_output_max_chars: int = DEFAULT_LIVE_OUTPUT_MAX_CHARS,
        live_output_max_chunks: int = DEFAULT_LIVE_OUTPUT_MAX_CHUNKS,
        live_output_runs_per_task: int = DEFAULT_LIVE_OUTPUT_RUNS_PER_TASK,
    ) -> None:
        if live_output_max_chars < 1:
            raise ValueError("live_output_max_chars must be positive")
        if live_output_max_chunks < 1:
            raise ValueError("live_output_max_chunks must be positive")
        if live_output_runs_per_task < 1:
            raise ValueError("live_output_runs_per_task must be positive")
        self._connect = connect
        self._now = now
        self._live_output_max_chars = live_output_max_chars
        self._live_output_max_chunks = live_output_max_chunks
        self._live_output_runs_per_task = live_output_runs_per_task

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
                    updated_at TEXT NOT NULL,
                    active_run_id TEXT,
                    lease_id TEXT,
                    lease_owner TEXT,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    last_output_at TEXT,
                    cancel_requested_at TEXT,
                    terminal_reason TEXT
                )
                """
            )
            self._ensure_task_lease_columns(con)
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS tasks_status_lease_idx
                ON tasks(status, lease_expires_at, priority, id)
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    returncode INTEGER,
                    last_event_sequence INTEGER NOT NULL DEFAULT 0,
                    dropped_through_sequence INTEGER NOT NULL DEFAULT 0,
                    dropped_chars INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS task_runs_task_started_idx
                ON task_runs(task_id, started_at DESC, run_id DESC)
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS task_output_chunks (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                )
                """
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS task_output_chunks_run_sequence_idx
                ON task_output_chunks(run_id, sequence)
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
                    WHEN 'CANCELLED' THEN 5
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

    def claim_next(
        self,
        *,
        now: str,
        lease_expires_at: str,
        worker_id: str,
        lease_id: str,
        run_id: str,
    ) -> Optional[TaskClaim]:
        """Atomically claim the highest-priority runnable task."""

        for name, value in (
            ("worker_id", worker_id),
            ("lease_id", lease_id),
            ("run_id", run_id),
        ):
            if not value:
                raise TaskRepositoryError(f"{name} must not be empty")
        if lease_expires_at <= now:
            raise TaskRepositoryError("lease_expires_at must be later than now")

        with self._connect() as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
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
            ).fetchone()
            if row is None:
                return None

            task_id = int(row["id"])
            lease_epoch = int(row["lease_epoch"]) + 1
            cursor = con.execute(
                """
                UPDATE tasks
                SET status = 'RUNNING', attempts = attempts + 1,
                    active_run_id = ?, lease_id = ?, lease_owner = ?,
                    lease_epoch = ?, lease_expires_at = ?, started_at = ?,
                    heartbeat_at = ?, last_output_at = NULL,
                    cancel_requested_at = NULL, terminal_reason = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND lease_epoch = ?
                  AND (
                    status = 'PENDING'
                    OR (
                        status = 'WAITING_LIMIT'
                        AND reset_at IS NOT NULL
                        AND reset_at <= ?
                    )
                  )
                """,
                (
                    run_id,
                    lease_id,
                    worker_id,
                    lease_epoch,
                    lease_expires_at,
                    now,
                    now,
                    now,
                    task_id,
                    lease_epoch - 1,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskRepositoryError("Task claim changed during its transaction")
            claimed = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

        task = TaskRecord.from_row(claimed)
        return TaskClaim(
            task=task,
            worker_id=worker_id,
            lease_id=lease_id,
            run_id=run_id,
            lease_epoch=lease_epoch,
            lease_expires_at=lease_expires_at,
        )

    def heartbeat_claim(
        self,
        task_id: int,
        lease_id: str,
        lease_epoch: int,
        now: str,
        lease_expires_at: str,
    ) -> bool:
        """Renew one active lease only when its fencing identity still matches."""

        if not lease_id:
            raise TaskRepositoryError("lease_id must not be empty")
        if lease_epoch < 1:
            raise TaskRepositoryError("lease_epoch must be positive")
        if lease_expires_at <= now:
            raise TaskRepositoryError("lease_expires_at must be later than now")

        with self._connect() as con:
            cursor = con.execute(
                """
                UPDATE tasks
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'RUNNING'
                  AND lease_id = ? AND lease_epoch = ?
                """,
                (now, lease_expires_at, now, task_id, lease_id, lease_epoch),
            )
            return cursor.rowcount == 1

    def finish_claim(
        self,
        task_id: int,
        lease_id: str,
        lease_epoch: int,
        status: str,
        terminal_reason: str,
        **fields: object,
    ) -> bool:
        """Finalize or release a task only for its current fenced lease."""

        allowed = TASK_STATUSES - {"RUNNING"}
        if status not in allowed:
            raise TaskRepositoryError(f"Unknown terminal or release status: {status}")
        if not lease_id:
            raise TaskRepositoryError("lease_id must not be empty")
        if lease_epoch < 1:
            raise TaskRepositoryError("lease_epoch must be positive")
        if not terminal_reason.strip():
            raise TaskRepositoryError("terminal_reason must not be empty")
        unknown_fields = set(fields) - CLAIM_FINISH_FIELDS
        if unknown_fields:
            raise TaskRepositoryError(
                f"Claim finalization cannot update field: {sorted(unknown_fields)[0]}"
            )

        updates = dict(fields)
        updates.update(
            {
                "status": status,
                "terminal_reason": terminal_reason,
                "lease_expires_at": None,
                "updated_at": self._now(),
            }
        )
        columns = ", ".join(f"{key} = ?" for key in updates)
        values = [
            *updates.values(),
            task_id,
            lease_id,
            lease_epoch,
        ]
        with self._connect() as con:
            cursor = con.execute(
                f"""
                UPDATE tasks SET {columns}
                WHERE id = ? AND status = 'RUNNING'
                  AND lease_id = ? AND lease_epoch = ?
                """,
                values,
            )
            return cursor.rowcount == 1

    def request_claim_cancellation(
        self,
        task_id: int,
        lease_id: str,
        lease_epoch: int,
        reason: str,
    ) -> bool:
        """Persist a cancellation request for the current fenced lease."""

        if not lease_id:
            raise TaskRepositoryError("lease_id must not be empty")
        if lease_epoch < 1:
            raise TaskRepositoryError("lease_epoch must be positive")
        if not reason.strip():
            raise TaskRepositoryError("Cancellation reason must not be empty")

        now = self._now()
        with self._connect() as con:
            cursor = con.execute(
                """
                UPDATE tasks
                SET cancel_requested_at = ?, terminal_reason = ?, updated_at = ?
                WHERE id = ? AND status = 'RUNNING'
                  AND lease_id = ? AND lease_epoch = ?
                  AND cancel_requested_at IS NULL
                """,
                (now, reason, now, task_id, lease_id, lease_epoch),
            )
            return cursor.rowcount == 1

    def recover_stale_claims(self, now: str) -> list[int]:
        """Fail expired RUNNING tasks conservatively without re-executing them."""

        reason = "Worker lease expired; process ownership could not be proven."
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """
                SELECT id, active_run_id
                FROM tasks
                WHERE status = 'RUNNING'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                ORDER BY id
                """,
                (now,),
            ).fetchall()
            task_ids = [int(row["id"]) for row in rows]
            if not task_ids:
                return []

            placeholders = ", ".join("?" for _ in task_ids)
            con.execute(
                f"""
                UPDATE tasks
                SET status = 'FAILED', lease_expires_at = NULL,
                    terminal_reason = ?, last_error = ?, updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'RUNNING'
                  AND lease_expires_at <= ?
                """,
                (reason, reason, now, *task_ids, now),
            )
            for row in rows:
                run_id = row["active_run_id"]
                if run_id:
                    con.execute(
                        """
                        UPDATE task_runs
                        SET status = 'failed', finished_at = ?, updated_at = ?,
                            last_event_sequence = last_event_sequence + 1
                        WHERE run_id = ? AND status = 'started'
                        """,
                        (now, now, str(run_id)),
                    )
            return task_ids

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

    def start_run(
        self,
        task_id: int,
        run_id: str,
        attempt: int,
        lease_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
    ) -> bool:
        """Create one idempotent live-output run and prune old finished runs."""

        if not run_id:
            raise TaskRepositoryError("run_id must not be empty")
        if attempt < 1:
            raise TaskRepositoryError("attempt must be positive")
        now = self._now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._validate_run_owner(
                con,
                task_id,
                run_id,
                lease_id,
                lease_epoch,
            )

            existing = con.execute(
                "SELECT task_id, attempt FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if int(existing[0]) != task_id or int(existing[1]) != attempt:
                    raise TaskRepositoryError("run_id is already assigned to another execution")
                return False

            con.execute(
                """
                INSERT INTO task_runs (
                    run_id, task_id, attempt, status, started_at, updated_at
                )
                VALUES (?, ?, ?, 'started', ?, ?)
                """,
                (run_id, task_id, attempt, now, now),
            )
            self._prune_finished_runs(con, task_id, current_run_id=run_id)
            return True

    def append_run_output(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        text: str,
        lease_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
    ) -> bool:
        """Append one ordered chunk and compact the run inside one transaction."""

        if sequence < 1:
            raise TaskRepositoryError("sequence must be positive")
        if not text:
            return False

        dropped_prefix_chars = max(0, len(text) - self._live_output_max_chars)
        bounded_text = text[-self._live_output_max_chars :]
        now = self._now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._validate_run_owner(
                con,
                task_id,
                run_id,
                lease_id,
                lease_epoch,
            )
            run = con.execute(
                """
                SELECT task_id, status, last_event_sequence,
                       dropped_through_sequence
                FROM task_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None or int(run[0]) != task_id:
                raise TaskRepositoryError("Output event does not match an active task run")
            if str(run[1]) != "started":
                raise TaskRepositoryError("Cannot append output to a finished task run")

            existing = con.execute(
                "SELECT text FROM task_output_chunks WHERE run_id = ? AND sequence = ?",
                (run_id, sequence),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != bounded_text:
                    raise TaskRepositoryError("Duplicate output sequence has different text")
                return False

            last_sequence = int(run[2])
            dropped_through = int(run[3])
            if sequence <= dropped_through:
                return False
            if sequence <= last_sequence:
                raise TaskRepositoryError("Output sequence is not monotonic")

            con.execute(
                """
                INSERT INTO task_output_chunks (
                    run_id, sequence, text, char_count, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, sequence, bounded_text, len(bounded_text), now),
            )
            con.execute(
                """
                UPDATE task_runs
                SET last_event_sequence = ?,
                    dropped_chars = dropped_chars + ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (sequence, dropped_prefix_chars, now, run_id),
            )
            con.execute(
                """
                UPDATE tasks
                SET last_output_at = ?, updated_at = ?
                WHERE id = ? AND status = 'RUNNING' AND active_run_id = ?
                """,
                (now, now, task_id, run_id),
            )
            self._compact_run_output(con, run_id)
            return True

    def finish_run(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        status: str,
        returncode: Optional[int],
        lease_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
    ) -> bool:
        """Finalize a run once while accepting an identical replay."""

        if status not in LIVE_RUN_STATUSES - {"started"}:
            raise TaskRepositoryError(f"Unknown terminal run status: {status}")
        if sequence < 1:
            raise TaskRepositoryError("sequence must be positive")
        now = self._now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._validate_run_owner(
                con,
                task_id,
                run_id,
                lease_id,
                lease_epoch,
            )
            run = con.execute(
                """
                SELECT task_id, status, returncode, last_event_sequence
                FROM task_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None or int(run[0]) != task_id:
                raise TaskRepositoryError("Lifecycle event does not match a task run")

            current_status = str(run[1])
            if current_status != "started":
                if (
                    current_status == status
                    and run[2] == returncode
                    and int(run[3]) == sequence
                ):
                    return False
                raise TaskRepositoryError("Task run is already finalized differently")
            if sequence <= int(run[3]):
                raise TaskRepositoryError("Lifecycle sequence is not monotonic")

            con.execute(
                """
                UPDATE task_runs
                SET status = ?, returncode = ?, last_event_sequence = ?,
                    finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'started'
                """,
                (status, returncode, sequence, now, now, run_id),
            )
            return True

    def read_run_output(
        self,
        task_id: int,
        run_id: Optional[str] = None,
        after_sequence: Optional[int] = None,
        before_sequence: Optional[int] = None,
        limit: int = 100,
    ) -> Optional[LiveOutputPage]:
        """Read one bounded output page using non-overlapping sequence cursors."""

        if after_sequence is not None and before_sequence is not None:
            raise TaskRepositoryError("Use either after_sequence or before_sequence, not both")
        if limit < 1 or limit > 1_000:
            raise TaskRepositoryError("limit must be between 1 and 1000")

        with self._connect() as con:
            con.row_factory = sqlite3.Row
            if run_id is None:
                run = con.execute(
                    """
                    SELECT * FROM task_runs
                    WHERE task_id = ?
                    ORDER BY started_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
            else:
                run = con.execute(
                    "SELECT * FROM task_runs WHERE task_id = ? AND run_id = ?",
                    (task_id, run_id),
                ).fetchone()
            if run is None:
                return None

            parameters: list[object] = [str(run["run_id"])]
            where = "run_id = ?"
            descending = after_sequence is None
            if after_sequence is not None:
                where += " AND sequence > ?"
                parameters.append(after_sequence)
                descending = False
            elif before_sequence is not None:
                where += " AND sequence < ?"
                parameters.append(before_sequence)
            order = "DESC" if descending else "ASC"
            parameters.append(limit)
            rows = con.execute(
                f"""
                SELECT sequence, text, created_at
                FROM task_output_chunks
                WHERE {where}
                ORDER BY sequence {order}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            if descending:
                rows = list(reversed(rows))

            bounds = con.execute(
                """
                SELECT MIN(sequence), MAX(sequence)
                FROM task_output_chunks
                WHERE run_id = ?
                """,
                (str(run["run_id"]),),
            ).fetchone()

        chunks = tuple(
            LiveOutputChunk(
                sequence=int(row["sequence"]),
                text=str(row["text"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )
        first_available = int(bounds[0]) if bounds[0] is not None else None
        last_available = int(bounds[1]) if bounds[1] is not None else None
        first_returned = chunks[0].sequence if chunks else None
        last_returned = chunks[-1].sequence if chunks else None
        dropped_through = int(run["dropped_through_sequence"])
        has_older = int(run["dropped_chars"]) > 0 or (
            first_available is not None
            and first_returned is not None
            and first_returned > first_available
        )
        has_more = (
            last_available is not None
            and last_returned is not None
            and last_returned < last_available
        )
        return LiveOutputPage(
            run_id=str(run["run_id"]),
            task_id=int(run["task_id"]),
            attempt=int(run["attempt"]),
            status=str(run["status"]),
            chunks=chunks,
            first_available_sequence=first_available,
            last_event_sequence=int(run["last_event_sequence"]),
            dropped_through_sequence=dropped_through,
            dropped_chars=int(run["dropped_chars"]),
            has_older=has_older,
            has_more=has_more,
            started_at=str(run["started_at"]),
            finished_at=run["finished_at"],
            returncode=run["returncode"],
        )

    def _compact_run_output(self, con: sqlite3.Connection, run_id: str) -> None:
        rows = con.execute(
            """
            SELECT sequence, char_count
            FROM task_output_chunks
            WHERE run_id = ?
            ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
        total_chars = sum(int(row[1]) for row in rows)
        removed: list[tuple[int, int]] = []
        while (
            len(rows) - len(removed) > self._live_output_max_chunks
            or total_chars > self._live_output_max_chars
        ):
            row = rows[len(removed)]
            removed.append((int(row[0]), int(row[1])))
            total_chars -= int(row[1])
        if not removed:
            return

        removed_sequences = [sequence for sequence, _chars in removed]
        placeholders = ", ".join("?" for _ in removed_sequences)
        con.execute(
            f"DELETE FROM task_output_chunks WHERE run_id = ? AND sequence IN ({placeholders})",
            [run_id, *removed_sequences],
        )
        con.execute(
            """
            UPDATE task_runs
            SET dropped_through_sequence = MAX(dropped_through_sequence, ?),
                dropped_chars = dropped_chars + ?
            WHERE run_id = ?
            """,
            (
                max(removed_sequences),
                sum(chars for _sequence, chars in removed),
                run_id,
            ),
        )

    def _prune_finished_runs(
        self,
        con: sqlite3.Connection,
        task_id: int,
        current_run_id: str,
    ) -> None:
        previous = con.execute(
            """
            SELECT run_id
            FROM task_runs
            WHERE task_id = ? AND run_id != ? AND status != 'started'
            ORDER BY started_at DESC, rowid DESC
            """,
            (task_id, current_run_id),
        ).fetchall()
        stale = previous[max(0, self._live_output_runs_per_task - 1) :]
        for row in stale:
            stale_run_id = str(row[0])
            con.execute("DELETE FROM task_output_chunks WHERE run_id = ?", (stale_run_id,))
            con.execute("DELETE FROM task_runs WHERE run_id = ?", (stale_run_id,))

    @staticmethod
    def _ensure_task_lease_columns(con: sqlite3.Connection) -> None:
        """Add local supervisor columns to databases created before issue #11."""

        existing = {
            str(row[1]) for row in con.execute("PRAGMA table_info(tasks)").fetchall()
        }
        definitions = {
            "active_run_id": "TEXT",
            "lease_id": "TEXT",
            "lease_owner": "TEXT",
            "lease_epoch": "INTEGER NOT NULL DEFAULT 0",
            "lease_expires_at": "TEXT",
            "started_at": "TEXT",
            "heartbeat_at": "TEXT",
            "last_output_at": "TEXT",
            "cancel_requested_at": "TEXT",
            "terminal_reason": "TEXT",
        }
        for column, definition in definitions.items():
            if column not in existing:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")

    @staticmethod
    def _validate_run_owner(
        con: sqlite3.Connection,
        task_id: int,
        run_id: str,
        lease_id: Optional[str],
        lease_epoch: Optional[int],
    ) -> None:
        """Fence live-run writes when a supervisor claim is supplied."""

        if (lease_id is None) != (lease_epoch is None):
            raise TaskRepositoryError("lease_id and lease_epoch must be supplied together")
        if lease_id is None:
            exists = con.execute(
                "SELECT 1 FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        else:
            exists = con.execute(
                """
                SELECT 1 FROM tasks
                WHERE id = ? AND status = 'RUNNING' AND active_run_id = ?
                  AND lease_id = ? AND lease_epoch = ?
                """,
                (task_id, run_id, lease_id, lease_epoch),
            ).fetchone()
        if exists is None:
            if lease_id is None:
                raise TaskRepositoryError(f"Unknown task id: {task_id}")
            raise TaskRepositoryError("Runner event claim ownership was lost")

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
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if not self._initialized:
                self.repository.initialize()
                self._initialized = True

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

    def claim_next_task(
        self,
        *,
        worker_id: str,
        lease_id: str,
        run_id: str,
        lease_expires_at: str,
    ) -> Optional[TaskClaim]:
        self.initialize()
        return self.repository.claim_next(
            now=self._now(),
            lease_expires_at=lease_expires_at,
            worker_id=worker_id,
            lease_id=lease_id,
            run_id=run_id,
        )

    def heartbeat_task_claim(
        self,
        claim: TaskClaim,
        lease_expires_at: str,
    ) -> bool:
        self.initialize()
        return self.repository.heartbeat_claim(
            claim.task.id,
            claim.lease_id,
            claim.lease_epoch,
            self._now(),
            lease_expires_at,
        )

    def finish_task_claim(
        self,
        claim: TaskClaim,
        status: str,
        terminal_reason: str,
        **fields: object,
    ) -> bool:
        self.initialize()
        return self.repository.finish_claim(
            claim.task.id,
            claim.lease_id,
            claim.lease_epoch,
            status,
            terminal_reason,
            **fields,
        )

    def request_task_cancellation(self, claim: TaskClaim, reason: str) -> bool:
        self.initialize()
        return self.repository.request_claim_cancellation(
            claim.task.id,
            claim.lease_id,
            claim.lease_epoch,
            reason,
        )

    def recover_stale_task_claims(self) -> list[int]:
        self.initialize()
        return self.repository.recover_stale_claims(self._now())

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

    def start_task_run(
        self,
        task_id: int,
        run_id: str,
        attempt: int,
        claim: Optional[TaskClaim] = None,
    ) -> bool:
        self.initialize()
        lease_id, lease_epoch = self._run_fence(task_id, run_id, claim)
        return self.repository.start_run(
            task_id,
            run_id,
            attempt,
            lease_id,
            lease_epoch,
        )

    def append_live_output(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        text: str,
        claim: Optional[TaskClaim] = None,
    ) -> bool:
        self.initialize()
        lease_id, lease_epoch = self._run_fence(task_id, run_id, claim)
        return self.repository.append_run_output(
            task_id,
            run_id,
            sequence,
            text,
            lease_id,
            lease_epoch,
        )

    def finish_task_run(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        status: str,
        returncode: Optional[int],
        claim: Optional[TaskClaim] = None,
    ) -> bool:
        self.initialize()
        lease_id, lease_epoch = self._run_fence(task_id, run_id, claim)
        return self.repository.finish_run(
            task_id,
            run_id,
            sequence,
            status,
            returncode,
            lease_id,
            lease_epoch,
        )

    @staticmethod
    def _run_fence(
        task_id: int,
        run_id: str,
        claim: Optional[TaskClaim],
    ) -> tuple[Optional[str], Optional[int]]:
        if claim is None:
            return None, None
        if claim.task.id != task_id or claim.run_id != run_id:
            raise TaskRepositoryError("Task claim does not match the live run")
        return claim.lease_id, claim.lease_epoch

    def live_output(
        self,
        task_id: int,
        run_id: Optional[str] = None,
        after_sequence: Optional[int] = None,
        before_sequence: Optional[int] = None,
        limit: int = 100,
    ) -> Optional[LiveOutputPage]:
        self.initialize()
        return self.repository.read_run_output(
            task_id,
            run_id=run_id,
            after_sequence=after_sequence,
            before_sequence=before_sequence,
            limit=limit,
        )
