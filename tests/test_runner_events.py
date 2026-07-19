"""Tests for event sequencing and the persistent live-output projection."""

import sqlite3
import tempfile
from pathlib import Path
import unittest

from runner_events import PersistentRunnerEventSink, RunnerEventEmitter
from runtime_contracts import RunnerInteractionState, RunnerLifecycle
from task_services import SQLiteTaskRepository, TaskApplicationService, TaskRepositoryError


class PersistentRunnerEventSinkTests(unittest.TestCase):
    """Verify normalization and lifecycle projection without runner coupling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "tasks.db"
        self.now = "2026-07-19T12:00:00+00:00"
        repository = SQLiteTaskRepository(
            connect=lambda: sqlite3.connect(self.db_path),
            now=lambda: self.now,
            live_output_max_chars=1_000,
            live_output_max_chunks=10,
        )
        self.tasks = TaskApplicationService(
            repository,
            now=lambda: self.now,
        )
        self.task_id = self.tasks.add_task("events", "prompt", self.tmp.name)

    def test_sink_normalizes_display_output_and_projects_terminal_state(self):
        sink = PersistentRunnerEventSink(self.tasks, self.task_id, "run-1", attempt=1)
        emitter = RunnerEventEmitter(self.task_id, sink=sink, run_id="run-1")

        emitter.lifecycle(RunnerLifecycle.STARTED)
        emitter.output("\x1b[31mred\x1b[0m\rTOKEN=private\x00\n")
        emitter.interaction(
            interaction_id="approval-1",
            state=RunnerInteractionState.REQUESTED,
            kind="approval",
            prompt="Proceed?",
        )
        emitter.lifecycle(RunnerLifecycle.COMPLETED, returncode=0)

        page = self.tasks.live_output(self.task_id, run_id="run-1")

        self.assertEqual(page.status, "completed")
        self.assertEqual(page.returncode, 0)
        self.assertEqual(page.chunks[0].text, "red\nTOKEN=<redacted>\n")
        self.assertEqual(page.last_event_sequence, 4)

    def test_sink_rejects_an_event_from_another_run(self):
        sink = PersistentRunnerEventSink(self.tasks, self.task_id, "run-1", attempt=1)
        other = RunnerEventEmitter(self.task_id, run_id="run-2")

        with self.assertRaisesRegex(ValueError, "does not match"):
            sink(other.lifecycle(RunnerLifecycle.STARTED))

    def test_sink_strips_ansi_sequences_split_across_events(self):
        """Terminal escape fragments must not leak into persisted display text."""

        sink = PersistentRunnerEventSink(self.tasks, self.task_id, "run-ansi", attempt=1)
        emitter = RunnerEventEmitter(self.task_id, sink=sink, run_id="run-ansi")

        emitter.lifecycle(RunnerLifecycle.STARTED)
        emitter.output("\x1b[3")
        emitter.output("1mred\x1b[0m")
        emitter.lifecycle(RunnerLifecycle.COMPLETED, returncode=0)

        page = self.tasks.live_output(self.task_id, run_id="run-ansi")

        self.assertEqual([chunk.text for chunk in page.chunks], ["red"])
        self.assertEqual(page.last_event_sequence, 4)

    def test_fail_open_run_is_idempotent(self):
        """Runner-side exceptions must not leave a live run marked started."""

        sink = PersistentRunnerEventSink(self.tasks, self.task_id, "run-error", attempt=1)
        emitter = RunnerEventEmitter(self.task_id, sink=sink, run_id="run-error")
        emitter.lifecycle(RunnerLifecycle.STARTED)
        emitter.output("partial")

        self.assertTrue(sink.fail_open_run())
        self.assertFalse(sink.fail_open_run())
        page = self.tasks.live_output(self.task_id, run_id="run-error")
        self.assertEqual(page.status, "failed")
        self.assertEqual(page.last_event_sequence, 3)

    def test_stale_claim_cannot_append_output_after_reassignment(self):
        """Live output uses the same fence as task heartbeat and finalization."""

        claim = self.tasks.claim_next_task(
            worker_id="worker-1",
            lease_id="lease-1",
            run_id="run-1",
            lease_expires_at="2026-07-19T12:01:00+00:00",
        )
        sink = PersistentRunnerEventSink(
            self.tasks,
            self.task_id,
            claim.run_id,
            attempt=claim.task.attempts,
            claim=claim,
        )
        emitter = RunnerEventEmitter(self.task_id, sink=sink, run_id=claim.run_id)
        emitter.lifecycle(RunnerLifecycle.STARTED)
        emitter.output("owned")

        self.now = "2026-07-19T12:01:00+00:00"
        self.tasks.recover_stale_task_claims()
        self.tasks.update_task(self.task_id, status="PENDING")
        current = self.tasks.claim_next_task(
            worker_id="worker-2",
            lease_id="lease-2",
            run_id="run-2",
            lease_expires_at="2026-07-19T12:02:00+00:00",
        )

        with self.assertRaisesRegex(TaskRepositoryError, "ownership was lost"):
            emitter.output("stale")

        self.assertEqual(current.lease_epoch, claim.lease_epoch + 1)
        page = self.tasks.live_output(self.task_id, run_id="run-1")
        self.assertEqual([chunk.text for chunk in page.chunks], ["owned"])


if __name__ == "__main__":
    unittest.main()
