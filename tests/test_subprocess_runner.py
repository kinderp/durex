"""Tests for incremental subprocess output and event ordering."""

import sys
import unittest

from process_control import RunCancellation
from runtime_contracts import RunnerLifecycle, RunnerLifecycleEvent, RunnerOutputEvent
from subprocess_runner import run_subprocess_command


class SubprocessRunnerTests(unittest.TestCase):
    """Verify streaming while preserving the historical final-output format."""

    def test_stdout_stderr_and_split_utf8_emit_before_completion(self):
        events = []
        script = (
            "import os; "
            "os.write(1, b'out'); "
            "os.write(1, bytes([0xe2])); "
            "os.write(1, bytes([0x82, 0xac])); "
            "os.write(2, b'err')"
        )

        result = run_subprocess_command(
            [sys.executable, "-c", script],
            task_id=7,
            event_sink=events.append,
            run_id="subprocess-run",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output, "out\u20ac\nerr")
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(
            [event.state for event in events if isinstance(event, RunnerLifecycleEvent)],
            [RunnerLifecycle.STARTED, RunnerLifecycle.COMPLETED],
        )
        output = "".join(
            event.text for event in events if isinstance(event, RunnerOutputEvent)
        )
        self.assertIn("out\u20ac", output)
        self.assertIn("err", output)

    def test_nonzero_exit_emits_failed_lifecycle(self):
        events = []

        result = run_subprocess_command(
            [sys.executable, "-c", "raise SystemExit(7)"],
            task_id=8,
            event_sink=events.append,
            run_id="failed-run",
        )

        lifecycle = [event for event in events if isinstance(event, RunnerLifecycleEvent)]
        self.assertEqual(result.returncode, 7)
        self.assertEqual(lifecycle[-1].state, RunnerLifecycle.FAILED)
        self.assertEqual(lifecycle[-1].returncode, 7)

    def test_external_cancellation_stops_owned_process_group(self):
        """A cancellation request produces a distinct terminal lifecycle."""

        cancellation = RunCancellation()
        events = []

        def consume(event):
            events.append(event)
            if (
                isinstance(event, RunnerLifecycleEvent)
                and event.state == RunnerLifecycle.STARTED
            ):
                cancellation.request("remote operator")

        result = run_subprocess_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            task_id=9,
            event_sink=consume,
            run_id="cancelled-run",
            cancellation=cancellation,
        )

        lifecycle = [event for event in events if isinstance(event, RunnerLifecycleEvent)]
        self.assertEqual(result.lifecycle, RunnerLifecycle.CANCELLED)
        self.assertEqual(lifecycle[-1].state, RunnerLifecycle.CANCELLED)
        self.assertEqual(lifecycle[-1].detail, "remote operator")


if __name__ == "__main__":
    unittest.main()
