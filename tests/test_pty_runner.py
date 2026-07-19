"""
Regression tests for the PTY runner approval loop.

These tests use unittest so they can run with the Python standard library, and
pytest will also collect them when it is available.
"""

import os
import sys
import unittest

from approval_policy import default_policy
from pty_runner import PtyRunnerConfig, run_pty_command
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

        result = run_pty_command(
            cmd=[sys.executable, "-c", script],
            cwd=os.getcwd(),
            policy=default_policy(),
            config=PtyRunnerConfig(echo_output=False),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.approval_events), 1)
        self.assertEqual(result.approval_events[0].command, "pytest -q")
        self.assertIn("approval result=y", result.output)

    def test_human_required_prompt_waits_on_decision_provider(self):
        """The PTY must consume the broker contract without polling Telegram."""

        script = (
            "answer=input('Command: git push origin feature\\nApprove this command? [y/N] '); "
            "print('approval result=' + answer)"
        )
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


if __name__ == "__main__":
    unittest.main()
