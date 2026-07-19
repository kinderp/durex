"""
Regression tests for the PTY runner approval loop.

These tests use unittest so they can run with the Python standard library, and
pytest will also collect them when it is available.
"""

import os
import sys
import unittest
from unittest import mock

from approval_policy import default_policy
from process_control import RunCancellation
from pty_runner import PtyRunnerConfig, run_pty_command
from runtime_contracts import (
    RunnerInteractionEvent,
    RunnerInteractionState,
    RunnerLifecycle,
    RunnerLifecycleEvent,
    RunnerOutputEvent,
)
from telegram_bridge import TelegramApprovalDecision, TelegramDecisionAction


class StaticApprovalProvider:
    """Broker contract double that returns one configured decision."""

    def __init__(self, action):
        self.action = action
        self.requests = []

    def request_decision(self, approval):
        self.requests.append(approval)
        return TelegramApprovalDecision(
            request_id=approval.request_id,
            action=self.action,
            source="telegram",
        )


class PtyRunnerApprovalTests(unittest.TestCase):
    """Regression coverage for the PTY approval loop contract."""

    def test_single_prompt_is_handled_once(self):
        """One terminal prompt should create one audit event and one answer."""

        script = (
            "answer=input('Command: pytest -q\\nApprove this command? [y/N] '); "
            "print('approval result=' + answer)"
        )

        events = []
        result = run_pty_command(
            cmd=[sys.executable, "-c", script],
            cwd=os.getcwd(),
            policy=default_policy(),
            config=PtyRunnerConfig(echo_output=False),
            event_sink=events.append,
            run_id="test-run",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.approval_events), 1)
        self.assertEqual(result.approval_events[0].command, "pytest -q")
        self.assertIn("approval result=y", result.output)
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(event.run_id == "test-run" for event in events))
        self.assertEqual(
            [event.state for event in events if isinstance(event, RunnerLifecycleEvent)],
            [RunnerLifecycle.STARTED, RunnerLifecycle.COMPLETED],
        )
        interactions = [event for event in events if isinstance(event, RunnerInteractionEvent)]
        self.assertEqual(
            [event.state for event in interactions],
            [RunnerInteractionState.REQUESTED, RunnerInteractionState.RESOLVED],
        )
        self.assertEqual(interactions[-1].decision, "approve")
        self.assertEqual(
            "".join(event.text for event in events if isinstance(event, RunnerOutputEvent)),
            result.output,
        )

    def test_human_required_prompt_waits_on_decision_provider(self):
        """The PTY must consume the broker contract without polling Telegram."""

        script = (
            "answer=input('Command: git push origin feature\\nApprove this command? [y/N] '); "
            "print('approval result=' + answer)"
        )
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                provider = StaticApprovalProvider(TelegramDecisionAction.DENY)
                result = run_pty_command(
                    cmd=[sys.executable, "-c", script],
                    cwd=os.getcwd(),
                    policy=default_policy(),
                    approval_provider=provider,
                    config=PtyRunnerConfig(echo_output=False),
                )

                self.assertEqual(result.returncode, 0)
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(provider.requests[0].command, "git push origin feature")
                self.assertIn("approval result=n", result.output)

    def test_stop_decision_emits_cancelled_lifecycle(self):
        """A human stop is terminal and distinct from process failure."""

        script = "input('Command: git push\\nApprove this command? [y/N] ')"
        provider = StaticApprovalProvider(TelegramDecisionAction.STOP)
        events = []

        result = run_pty_command(
            cmd=[sys.executable, "-c", script],
            policy=default_policy(),
            approval_provider=provider,
            config=PtyRunnerConfig(echo_output=False),
            event_sink=events.append,
            run_id="stopped-run",
        )

        lifecycle = [event for event in events if isinstance(event, RunnerLifecycleEvent)]
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            [event.state for event in lifecycle],
            [RunnerLifecycle.STARTED, RunnerLifecycle.CANCELLED],
        )

    def test_external_cancellation_stops_owned_process_group(self):
        """The PTY runner exposes remote cancellation as a terminal state."""

        cancellation = RunCancellation()
        events = []

        def consume(event):
            events.append(event)
            if (
                isinstance(event, RunnerLifecycleEvent)
                and event.state == RunnerLifecycle.STARTED
            ):
                cancellation.request("remote operator")

        result = run_pty_command(
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
            config=PtyRunnerConfig(echo_output=False),
            event_sink=consume,
            run_id="externally-stopped-run",
            cancellation=cancellation,
        )

        lifecycle = [event for event in events if isinstance(event, RunnerLifecycleEvent)]
        self.assertEqual(result.lifecycle, RunnerLifecycle.CANCELLED)
        self.assertEqual(lifecycle[-1].state, RunnerLifecycle.CANCELLED)
        self.assertEqual(lifecycle[-1].detail, "remote operator")

    def test_post_exit_drain_has_total_deadline(self):
        """Continuous descendant output must not keep a completed task alive."""

        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0

        with mock.patch(
            "pty_runner.spawn_pty_process",
            return_value=(process, 99),
        ), mock.patch(
            "pty_runner.select.select",
            return_value=([99], [], []),
        ) as select_call, mock.patch(
            "pty_runner.os.read",
            return_value=b"x",
        ), mock.patch(
            "pty_runner.os.close",
        ), mock.patch(
            "pty_runner.time.monotonic",
            side_effect=(0.0, 0.2, 0.6),
        ):
            result = run_pty_command(
                cmd=["ignored"],
                config=PtyRunnerConfig(
                    read_timeout_seconds=1.0,
                    post_exit_drain_seconds=0.5,
                    echo_output=False,
                ),
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output, "xx")
        self.assertEqual(select_call.call_count, 2)
        self.assertAlmostEqual(select_call.call_args_list[1].args[3], 0.3)

    def test_split_utf8_bytes_emit_one_valid_character(self):
        """PTY byte boundaries must not introduce replacement characters."""

        process = mock.Mock()
        process.poll.side_effect = (None, 0, 0)
        process.wait.return_value = 0
        events = []

        with mock.patch(
            "pty_runner.spawn_pty_process",
            return_value=(process, 99),
        ), mock.patch(
            "pty_runner.select.select",
            side_effect=(([99], [], []), ([99], [], []), ([], [], [])),
        ), mock.patch(
            "pty_runner.os.read",
            side_effect=(b"\xe2", b"\x82\xac"),
        ), mock.patch(
            "pty_runner.os.close",
        ), mock.patch(
            "pty_runner.time.monotonic",
            side_effect=(0.0, 0.1),
        ):
            result = run_pty_command(
                cmd=["ignored"],
                config=PtyRunnerConfig(echo_output=False),
                event_sink=events.append,
                run_id="utf8-run",
            )

        output = "".join(
            event.text for event in events if isinstance(event, RunnerOutputEvent)
        )
        self.assertEqual(result.output, "\u20ac")
        self.assertEqual(output, "\u20ac")


if __name__ == "__main__":
    unittest.main()
