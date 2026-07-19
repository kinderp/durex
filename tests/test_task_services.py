"""Characterization tests for task persistence and application services."""

import sqlite3
import tempfile
from pathlib import Path
import unittest

from task_services import SQLiteTaskRepository, TaskApplicationService, TaskRepositoryError


class TaskApplicationServiceTests(unittest.TestCase):
    """Lock down queue reads and compare-and-set state transitions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "tasks.db"
        self.now = "2026-07-19T12:00:00+00:00"
        repository = SQLiteTaskRepository(
            connect=lambda: sqlite3.connect(self.db_path),
            now=lambda: self.now,
        )
        self.service = TaskApplicationService(repository, now=lambda: self.now)

    def add_task(self, title, priority=100):
        return self.service.add_task(
            title=title,
            prompt=f"Prompt for {title}",
            workdir=self.tmp.name,
            priority=priority,
            max_attempts=3,
        )

    def limited_service(self, max_chars=10, max_chunks=2, runs_per_task=2):
        repository = SQLiteTaskRepository(
            connect=lambda: sqlite3.connect(self.db_path),
            now=lambda: self.now,
            live_output_max_chars=max_chars,
            live_output_max_chunks=max_chunks,
            live_output_runs_per_task=runs_per_task,
        )
        return TaskApplicationService(repository, now=lambda: self.now)

    def test_next_runnable_orders_by_priority_then_identifier(self):
        """Lower priority values win and insertion order breaks ties."""

        first = self.add_task("first", priority=5)
        self.add_task("later", priority=5)
        self.add_task("urgent", priority=1)

        self.assertEqual(self.service.next_runnable_task().title, "urgent")
        self.service.update_task(3, status="COMPLETED")
        self.assertEqual(self.service.next_runnable_task().id, first)

    def test_waiting_limit_becomes_runnable_only_after_reset(self):
        """Usage-limited work remains blocked until its persisted reset time."""

        task_id = self.add_task("limited", priority=1)
        self.service.update_task(
            task_id,
            status="WAITING_LIMIT",
            reset_at="2026-07-19T12:30:00+00:00",
        )

        self.assertIsNone(self.service.next_runnable_task())
        self.now = "2026-07-19T12:30:00+00:00"
        self.assertEqual(self.service.next_runnable_task().id, task_id)

    def test_transition_is_atomic_against_expected_status(self):
        """A stale caller cannot overwrite a task after its status changed."""

        task_id = self.add_task("claim")

        changed = self.service.transition_task(
            task_id,
            {"PENDING"},
            "RUNNING",
            attempts=1,
        )
        stale_change = self.service.transition_task(
            task_id,
            {"PENDING"},
            "FAILED",
            last_error="stale worker",
        )

        task = self.service.task_detail(task_id)
        self.assertTrue(changed)
        self.assertFalse(stale_change)
        self.assertEqual(task.status, "RUNNING")
        self.assertIsNone(task.last_error)

    def test_list_and_recent_preserve_distinct_ordering_contracts(self):
        """CLI status ordering and Telegram recency ordering remain distinct."""

        pending_id = self.add_task("pending", priority=1)
        completed_id = self.add_task("completed", priority=1)
        running_id = self.add_task("running", priority=99)
        self.service.update_task(completed_id, status="COMPLETED")
        self.service.update_task(running_id, status="RUNNING")

        self.assertEqual(
            [task.id for task in self.service.list_tasks()],
            [running_id, pending_id, completed_id],
        )
        self.assertEqual(
            [task.id for task in self.service.recent_tasks(2)],
            [running_id, completed_id],
        )

    def test_output_lookup_uses_latest_task_when_id_is_omitted(self):
        """Telegram tail without an id continues to select the latest task."""

        self.add_task("older")
        latest_id = self.add_task("latest")
        self.service.update_task(latest_id, output="latest output")

        task = self.service.task_output()

        self.assertEqual(task.id, latest_id)
        self.assertEqual(task.output, "latest output")

    def test_task_record_preserves_read_only_sqlite_row_access(self):
        """Compatibility shims should support keys, positions, slices and values."""

        task_id = self.add_task("row compatible", priority=7)
        task = self.service.task_detail(task_id)

        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["ID"], task_id)
        self.assertEqual(task[0], task_id)
        self.assertEqual(task[1], "row compatible")
        self.assertEqual(task[:3], (task_id, "row compatible", "Prompt for row compatible"))
        self.assertEqual(task.keys()[:3], ["id", "title", "prompt"])
        self.assertEqual(list(task)[:3], list(task[:3]))
        self.assertEqual(dict(task)["priority"], 7)
        with self.assertRaisesRegex(IndexError, "No item with that key"):
            task["missing"]

    def test_live_output_is_ordered_deduplicated_and_cursor_addressable(self):
        """Sequence cursors return new chunks once and reject late conflicts."""

        service = self.limited_service()
        task_id = service.add_task("live", "prompt")
        self.assertTrue(service.start_task_run(task_id, "run-1", attempt=1))
        self.assertFalse(service.start_task_run(task_id, "run-1", attempt=1))
        self.assertTrue(service.append_live_output(task_id, "run-1", 2, "12345"))
        self.assertTrue(service.append_live_output(task_id, "run-1", 4, "67890"))
        self.assertFalse(service.append_live_output(task_id, "run-1", 4, "67890"))

        first = service.live_output(task_id, run_id="run-1", limit=1)
        older = service.live_output(
            task_id,
            run_id="run-1",
            before_sequence=first.chunks[0].sequence,
        )
        refresh = service.live_output(
            task_id,
            run_id="run-1",
            after_sequence=older.chunks[-1].sequence,
        )

        self.assertEqual([chunk.sequence for chunk in first.chunks], [4])
        self.assertEqual([chunk.sequence for chunk in older.chunks], [2])
        self.assertTrue(older.has_more)
        self.assertEqual([chunk.sequence for chunk in refresh.chunks], [4])
        with self.assertRaisesRegex(TaskRepositoryError, "not monotonic"):
            service.append_live_output(task_id, "run-1", 3, "late")

    def test_live_output_compaction_tracks_dropped_cursor_and_characters(self):
        """Per-run storage retains a bounded suffix with explicit gap metadata."""

        service = self.limited_service(max_chars=10, max_chunks=2)
        task_id = service.add_task("bounded", "prompt")
        service.start_task_run(task_id, "run-bounded", attempt=1)
        service.append_live_output(task_id, "run-bounded", 2, "12345")
        service.append_live_output(task_id, "run-bounded", 4, "67890")
        service.append_live_output(task_id, "run-bounded", 6, "abc")

        page = service.live_output(task_id, run_id="run-bounded")

        self.assertEqual([(chunk.sequence, chunk.text) for chunk in page.chunks], [(4, "67890"), (6, "abc")])
        self.assertEqual(page.dropped_through_sequence, 2)
        self.assertEqual(page.dropped_chars, 5)
        self.assertTrue(page.has_older)

    def test_large_single_chunk_keeps_only_its_suffix(self):
        """One oversized event cannot bypass the per-run character limit."""

        service = self.limited_service(max_chars=5)
        task_id = service.add_task("large", "prompt")
        service.start_task_run(task_id, "run-large", attempt=1)
        service.append_live_output(task_id, "run-large", 2, "0123456789")

        page = service.live_output(task_id, run_id="run-large")

        self.assertEqual(page.chunks[0].text, "56789")
        self.assertEqual(page.dropped_chars, 5)
        self.assertTrue(page.has_older)
        self.assertFalse(service.append_live_output(task_id, "run-large", 2, "0123456789"))

    def test_run_finalization_is_idempotent_but_rejects_conflicts(self):
        """A terminal lifecycle replay cannot change the persisted outcome."""

        service = self.limited_service()
        task_id = service.add_task("finish", "prompt")
        service.start_task_run(task_id, "run-finish", attempt=1)
        service.append_live_output(task_id, "run-finish", 2, "done")

        self.assertTrue(
            service.finish_task_run(task_id, "run-finish", 3, "completed", 0)
        )
        self.assertFalse(
            service.finish_task_run(task_id, "run-finish", 3, "completed", 0)
        )
        with self.assertRaisesRegex(TaskRepositoryError, "finalized differently"):
            service.finish_task_run(task_id, "run-finish", 3, "failed", 1)

    def test_live_run_writes_lock_before_validation(self):
        """Lifecycle writes must serialize before reading mutable run state."""

        statements = []

        def connect():
            connection = sqlite3.connect(self.db_path)
            connection.set_trace_callback(statements.append)
            return connection

        service = TaskApplicationService(
            SQLiteTaskRepository(connect=connect, now=lambda: self.now),
            now=lambda: self.now,
        )
        task_id = service.add_task("transaction", "prompt")

        for operation in (
            lambda: service.start_task_run(task_id, "run-transaction", 1),
            lambda: service.append_live_output(
                task_id, "run-transaction", 2, "chunk"
            ),
            lambda: service.finish_task_run(
                task_id, "run-transaction", 3, "completed", 0
            ),
        ):
            statements.clear()
            operation()
            begin = next(
                statement for statement in statements if statement.startswith("BEGIN")
            )
            self.assertEqual(begin, "BEGIN IMMEDIATE")

    def test_finished_run_retention_survives_service_restart(self):
        """Only configured historical runs remain queryable after reopening SQLite."""

        service = self.limited_service(runs_per_task=2)
        task_id = service.add_task("retention", "prompt")
        for attempt in range(1, 4):
            run_id = f"run-{attempt}"
            service.start_task_run(task_id, run_id, attempt)
            service.append_live_output(task_id, run_id, 2, run_id)
            service.finish_task_run(task_id, run_id, 3, "completed", 0)

        restarted = self.limited_service(runs_per_task=2)

        self.assertIsNone(restarted.live_output(task_id, run_id="run-1"))
        self.assertEqual(restarted.live_output(task_id, run_id="run-2").chunks[0].text, "run-2")
        self.assertEqual(restarted.live_output(task_id).run_id, "run-3")

    def test_initialize_adds_live_output_tables_without_replacing_tasks(self):
        """The additive migration must preserve an existing task database."""

        task_id = self.add_task("legacy")
        with sqlite3.connect(self.db_path) as con:
            con.execute("DROP TABLE task_output_chunks")
            con.execute("DROP TABLE task_runs")

        migrated = self.limited_service()
        migrated.initialize()

        self.assertEqual(migrated.task_detail(task_id).title, "legacy")
        with sqlite3.connect(self.db_path) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("task_runs", tables)
        self.assertIn("task_output_chunks", tables)


if __name__ == "__main__":
    unittest.main()
