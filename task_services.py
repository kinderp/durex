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

LIVE_RUN_STATUSES = frozenset({"started", "completed", "failed", "cancelled"})
DEFAULT_LIVE_OUTPUT_MAX_CHARS = 200_000
DEFAULT_LIVE_OUTPUT_MAX_CHUNKS = 1_000
DEFAULT_LIVE_OUTPUT_RUNS_PER_TASK = 3


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

    def start_run(self, task_id: int, run_id: str, attempt: int) -> bool: ...

    def append_run_output(self, task_id: int, run_id: str, sequence: int, text: str) -> bool: ...

    def finish_run(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        status: str,
        returncode: Optional[int],
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
                    updated_at TEXT NOT NULL
                )
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

    def start_run(self, task_id: int, run_id: str, attempt: int) -> bool:
        """Create one idempotent live-output run and prune old finished runs."""

        if not run_id:
            raise TaskRepositoryError("run_id must not be empty")
        if attempt < 1:
            raise TaskRepositoryError("attempt must be positive")
        now = self._now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task_exists = con.execute(
                "SELECT 1 FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task_exists is None:
                raise TaskRepositoryError(f"Unknown task id: {task_id}")

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
            self._compact_run_output(con, run_id)
            return True

    def finish_run(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        status: str,
        returncode: Optional[int],
    ) -> bool:
        """Finalize a run once while accepting an identical replay."""

        if status not in LIVE_RUN_STATUSES - {"started"}:
            raise TaskRepositoryError(f"Unknown terminal run status: {status}")
        if sequence < 1:
            raise TaskRepositoryError("sequence must be positive")
        now = self._now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
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

    def start_task_run(self, task_id: int, run_id: str, attempt: int) -> bool:
        self.initialize()
        return self.repository.start_run(task_id, run_id, attempt)

    def append_live_output(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        text: str,
    ) -> bool:
        self.initialize()
        return self.repository.append_run_output(task_id, run_id, sequence, text)

    def finish_task_run(
        self,
        task_id: int,
        run_id: str,
        sequence: int,
        status: str,
        returncode: Optional[int],
    ) -> bool:
        self.initialize()
        return self.repository.finish_run(
            task_id,
            run_id,
            sequence,
            status,
            returncode,
        )

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
