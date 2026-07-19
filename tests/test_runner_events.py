"""Tests for event sequencing and the persistent live-output projection."""

import sqlite3
import tempfile
from pathlib import Path
import unittest

from runner_events import PersistentRunnerEventSink, RunnerEventEmitter
from runtime_contracts import RunnerInteractionState, RunnerLifecycle
from task_services import SQLiteTaskRepository, TaskApplicationService


class PersistentRunnerEventSinkTests(unittest.TestCase):
    """Verify normalization and lifecycle projection without runner coupling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "tasks.db"
        repository = SQLiteTaskRepository(
            connect=lambda: sqlite3.connect(self.db_path),
            now=lambda: "2026-07-19T12:00:00+00:00",
            live_output_max_chars=1_000,
            live_output_max_chunks=10,
        )
        self.tasks = TaskApplicationService(
            repository,
            now=lambda: "2026-07-19T12:00:00+00:00",
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


if __name__ == "__main__":
    unittest.main()
