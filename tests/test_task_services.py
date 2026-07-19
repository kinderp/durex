"""Characterization tests for task persistence and application services."""

import sqlite3
import tempfile
from pathlib import Path
import unittest

from task_services import SQLiteTaskRepository, TaskApplicationService


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


if __name__ == "__main__":
    unittest.main()
