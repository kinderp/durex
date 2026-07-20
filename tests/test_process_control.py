"""Tests for owner-scoped cancellation and process-group termination."""

import signal
import subprocess
import unittest
from unittest import mock

from process_control import RunCancellation, terminate_process_group


class RunCancellationTests(unittest.TestCase):
    """Verify request ordering and bounded process cleanup."""

    def test_request_before_bind_invokes_terminator_once(self):
        cancellation = RunCancellation()
        terminator = mock.Mock()

        self.assertTrue(cancellation.request("operator request"))
        cancellation.bind_terminator(terminator)
        self.assertFalse(cancellation.request("duplicate"))

        terminator.assert_called_once_with()
        self.assertTrue(cancellation.requested)
        self.assertEqual(cancellation.reason, "operator request")

    @mock.patch("process_control.os.killpg")
    def test_terminate_process_group_uses_graceful_signal(self, killpg):
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        process.wait.return_value = -signal.SIGTERM

        returncode = terminate_process_group(process, timeout_seconds=2.0)

        killpg.assert_called_once_with(123, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=2.0)
        self.assertEqual(returncode, -signal.SIGTERM)

    @mock.patch("process_control.os.killpg")
    def test_terminate_process_group_forces_after_timeout(self, killpg):
        process = mock.Mock(pid=456)
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired("cmd", 2.0),
            -signal.SIGKILL,
        )

        returncode = terminate_process_group(process, timeout_seconds=2.0)

        self.assertEqual(
            killpg.call_args_list,
            [mock.call(456, signal.SIGTERM), mock.call(456, signal.SIGKILL)],
        )
        self.assertEqual(returncode, -signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
