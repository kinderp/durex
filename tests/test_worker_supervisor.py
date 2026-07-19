"""Tests for durable worker ownership and operator stop semantics."""

import datetime as dt
import sqlite3
import tempfile
import threading
from pathlib import Path
import unittest

from runtime_contracts import RunnerOutputEvent
from task_services import SQLiteTaskRepository, TaskApplicationService
from worker_supervisor import DurableWorkerSupervisor


class DurableWorkerSupervisorTests(unittest.TestCase):
    """Exercise claims, event observation, cancellation, and recovery."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "tasks.db"
        self.now = dt.datetime(2026, 7, 19, 12, 0, tzinfo=dt.timezone.utc)
        repository = SQLiteTaskRepository(
            connect=lambda: sqlite3.connect(self.db_path),
            now=lambda: self.now.isoformat(),
        )
        self.tasks = TaskApplicationService(
            repository,
            now=lambda: self.now.isoformat(),
        )

    def add_task(self, title="supervised"):
        return self.tasks.add_task(title, "prompt", self.tmp.name, max_attempts=1)

    def test_run_claims_observes_and_finishes_one_task(self):
        task_id = self.add_task()
        notifications = []

        def execute(claim, _cancellation, observe):
            self.assertEqual(claim.task.status, "RUNNING")
            self.assertEqual(claim.task.attempts, 1)
            observe(
                RunnerOutputEvent(
                    task_id=task_id,
                    sequence=2,
                    text="chunk",
                    run_id=claim.run_id,
                )
            )
            snapshot = supervisor.snapshot()
            self.assertEqual(snapshot.current_task_id, task_id)
            self.assertEqual(snapshot.current_run_id, claim.run_id)
            self.assertIsNotNone(snapshot.last_output_at)
            self.tasks.finish_task_claim(
                claim,
                "COMPLETED",
                "test completed",
                output="done",
            )

        supervisor = DurableWorkerSupervisor(
            self.tasks,
            execute,
            notify=notifications.append,
            worker_id="worker-test",
            now=lambda: self.now,
        )

        supervisor.run(stop_when_empty=True, check_interval=0)

        self.assertEqual(self.tasks.task_detail(task_id).status, "COMPLETED")
        self.assertEqual(
            notifications,
            [
                "Starting task #1: supervised",
                "No executable tasks found. Worker is idle.",
            ],
        )
        self.assertFalse(supervisor.snapshot().running)

    def test_stop_current_persists_request_and_cancels_owned_run(self):
        task_id = self.add_task("cancel me")
        executing = threading.Event()

        def execute(_claim, cancellation, _observe):
            executing.set()
            self.assertTrue(cancellation.wait(2.0))

        supervisor = DurableWorkerSupervisor(
            self.tasks,
            execute,
            worker_id="worker-cancel",
            now=lambda: self.now,
        )
        self.assertTrue(supervisor.start(stop_when_empty=True, check_interval=0))
        self.assertTrue(executing.wait(2.0))

        self.assertTrue(supervisor.request_stop_current("remote operator"))
        supervisor._thread.join(timeout=2.0)

        task = self.tasks.task_detail(task_id)
        self.assertEqual(task.status, "CANCELLED")
        self.assertEqual(task.terminal_reason, "remote operator")
        self.assertFalse(supervisor.snapshot().running)

    def test_stop_after_current_does_not_claim_next_task(self):
        first_id = self.add_task("first")
        second_id = self.add_task("second")

        def execute(claim, _cancellation, _observe):
            self.tasks.finish_task_claim(claim, "COMPLETED", "done")
            supervisor.request_stop_after_current()

        supervisor = DurableWorkerSupervisor(
            self.tasks,
            execute,
            worker_id="worker-stop-after",
            now=lambda: self.now,
        )
        supervisor.run(stop_when_empty=True, check_interval=0)

        self.assertEqual(self.tasks.task_detail(first_id).status, "COMPLETED")
        self.assertEqual(self.tasks.task_detail(second_id).status, "PENDING")

    def test_executor_that_abandons_claim_fails_conservatively(self):
        task_id = self.add_task("abandoned")
        supervisor = DurableWorkerSupervisor(
            self.tasks,
            lambda _claim, _cancellation, _observe: None,
            worker_id="worker-abandon",
            now=lambda: self.now,
        )

        supervisor.run(stop_when_empty=True, check_interval=0)

        task = self.tasks.task_detail(task_id)
        self.assertEqual(task.status, "FAILED")
        self.assertIn("without finalizing", task.last_error)

    def test_heartbeat_error_cancels_execution_before_lease_expiry(self):
        """A failed renewal must stop work instead of silently losing its lease."""

        task_id = self.add_task("heartbeat failure")

        def execute(_claim, cancellation, _observe):
            self.assertTrue(cancellation.wait(1.0))

        supervisor = DurableWorkerSupervisor(
            self.tasks,
            execute,
            worker_id="worker-heartbeat",
            lease_seconds=0.2,
            heartbeat_seconds=0.05,
            now=lambda: self.now,
        )
        original_heartbeat = self.tasks.heartbeat_task_claim

        def fail_heartbeat(_claim, _lease_expires_at):
            raise sqlite3.OperationalError("database unavailable")

        self.tasks.heartbeat_task_claim = fail_heartbeat
        self.addCleanup(
            setattr,
            self.tasks,
            "heartbeat_task_claim",
            original_heartbeat,
        )

        supervisor.run(stop_when_empty=True, check_interval=0)

        task = self.tasks.task_detail(task_id)
        self.assertEqual(task.status, "CANCELLED")
        self.assertIn("heartbeat failed", task.terminal_reason)
        self.assertIn("database unavailable", supervisor.snapshot().last_error)

    def test_notification_error_does_not_abandon_claim(self):
        """Transport failure cannot interrupt worker ownership cleanup."""

        task_id = self.add_task("notification failure")

        def execute(claim, _cancellation, _observe):
            self.tasks.finish_task_claim(claim, "COMPLETED", "done")

        def fail_notification(_message):
            raise RuntimeError("telegram unavailable")

        supervisor = DurableWorkerSupervisor(
            self.tasks,
            execute,
            notify=fail_notification,
            worker_id="worker-notification",
            now=lambda: self.now,
        )

        supervisor.run(stop_when_empty=True, check_interval=0)

        self.assertEqual(self.tasks.task_detail(task_id).status, "COMPLETED")
        snapshot = supervisor.snapshot()
        self.assertIsNone(snapshot.current_task_id)
        self.assertIn("telegram unavailable", snapshot.last_error)


if __name__ == "__main__":
    unittest.main()
