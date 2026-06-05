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


class PtyRunnerApprovalTests(unittest.TestCase):
    def test_single_prompt_is_handled_once(self):
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


if __name__ == "__main__":
    unittest.main()
