"""
Tests for queue output parsing helpers.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import codex_queue
from codex_queue import extract_session_id
from telegram_bridge import extract_chat_ids_from_updates


class SessionIdExtractionTests(unittest.TestCase):
    """Regression coverage for parsing resumable Codex session identifiers."""

    def test_extract_session_id_returns_latest_candidate(self):
        """Retry output should keep the newest session id, not the first one."""

        old_session = "11111111-1111-1111-1111-111111111111"
        new_session = "22222222-2222-2222-2222-222222222222"
        output = f"""
Session: {old_session}
Some retry output
resuming work with {new_session}
"""

        self.assertEqual(extract_session_id(output), new_session)

    def test_extract_session_id_supports_session_id_label(self):
        """The explicit session_id label is part of the supported CLI surface."""

        session = "33333333-3333-3333-3333-333333333333"

        self.assertEqual(extract_session_id(f"session_id: {session}"), session)


class QueueLifecycleCharacterizationTests(unittest.TestCase):
    """Lock down finalization and runner-dispatch decisions before extraction."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = codex_queue.DB_PATH
        codex_queue.DB_PATH = str(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.restore_db_path)

    def restore_db_path(self):
        codex_queue.DB_PATH = self.old_db_path

    def add_task(self, max_attempts=3):
        codex_queue.add_task(
            title="characterized task",
            prompt="perform work",
            workdir=self.tmp.name,
            priority=1,
            max_attempts=max_attempts,
        )
        return codex_queue.get_next_task()

    def load_task(self, task_id):
        codex_queue.init_db()
        with codex_queue.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def test_successful_output_completes_task_and_keeps_latest_session(self):
        """Successful runs persist output and the final session candidate."""

        task = self.add_task()
        session = "44444444-4444-4444-4444-444444444444"

        codex_queue.finish_task_from_output(task, f"Session: {session}\ndone", 0)

        stored = self.load_task(task["id"])
        self.assertEqual(stored["status"], "COMPLETED")
        self.assertEqual(stored["session_id"], session)
        self.assertEqual(stored["output"], f"Session: {session}\ndone")
        self.assertIsNone(stored["last_error"])

    def test_usage_limit_suspends_task_without_consuming_retry_policy(self):
        """Quota output stores its reset timestamp and resumable next step."""

        task = self.add_task()
        reset_at = "2026-07-20T08:00:00+00:00"

        codex_queue.finish_task_from_output(
            task,
            f"Usage limit reached. Reset at {reset_at}",
            1,
        )

        stored = self.load_task(task["id"])
        self.assertEqual(stored["status"], "WAITING_LIMIT")
        self.assertEqual(stored["reset_at"], reset_at)
        self.assertEqual(stored["last_error"], "Usage limit reached")
        self.assertIn("exact point", stored["next_step"])

    def test_non_limit_failure_retries_then_fails_at_attempt_limit(self):
        """Failures return to pending until the configured maximum is reached."""

        task = self.add_task(max_attempts=2)
        codex_queue.update_task(task["id"], status="RUNNING", attempts=1)
        codex_queue.finish_task_from_output(task, "first failure", 1)
        stored = self.load_task(task["id"])
        self.assertEqual(stored["status"], "PENDING")

        codex_queue.update_task(task["id"], status="RUNNING", attempts=2)
        second_attempt = self.load_task(task["id"])
        codex_queue.finish_task_from_output(second_attempt, "second failure", 1)
        stored = self.load_task(task["id"])
        self.assertEqual(stored["status"], "FAILED")
        self.assertEqual(stored["last_error"], "second failure")

    def test_run_task_dispatches_only_the_selected_runner(self):
        """Runner mode remains the sole dispatch decision at this boundary."""

        task = self.add_task()
        with mock.patch.object(codex_queue, "run_codex_pty") as pty_run, mock.patch.object(
            codex_queue, "run_codex_subprocess"
        ) as subprocess_run:
            codex_queue.run_task(
                task,
                runner_mode="pty",
                telegram_enabled=True,
                telegram_verbosity="verbose",
                echo_output=False,
            )

        pty_run.assert_called_once_with(
            task,
            telegram_enabled=True,
            telegram_verbosity="verbose",
            echo_output=False,
        )
        subprocess_run.assert_not_called()


class TelegramCheckTests(unittest.TestCase):
    """Regression coverage for Telegram connectivity helper behavior."""

    def test_extract_chat_ids_from_updates_supports_messages_and_callbacks(self):
        """Chat discovery must handle normal messages and callback updates."""

        updates = [
            {"message": {"chat": {"id": 123}}},
            {"callback_query": {"message": {"chat": {"id": "456"}}}},
            {"edited_message": {"chat": {"id": 123}}},
            {"message": {"chat": {"id": "not-a-number"}}},
        ]

        self.assertEqual(extract_chat_ids_from_updates(updates), [123, 456])

    def test_telegram_check_uses_env_and_sends_test_message(self):
        """telegram-check should build its bridge from environment variables."""

        class FakeBridge:
            """Minimal bridge double that captures configuration and messages."""

            last_config = None

            def __init__(self, config):
                """Store the config passed by telegram_check."""

                FakeBridge.last_config = config

            def get_me(self):
                """Return bot metadata without calling Telegram."""

                return {"username": "DurexBot", "id": 42}

            def discover_chat_ids(self, timeout=0):
                """Return a deterministic discovered chat id."""

                return [123]

            def send_message(self, message):
                """Capture the test message and return a fake message id."""

                self.sent_message = message
                return 99

        env = {
            "DUREX_TELEGRAM_BOT_TOKEN": "token",
            "DUREX_TELEGRAM_CHAT_ID": "123",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            codex_queue, "TelegramApprovalBridge", FakeBridge
        ):
            codex_queue.telegram_check(
                discover_chat_id=True,
                send_test=True,
                message="hello",
                poll_timeout=0,
            )

        self.assertEqual(FakeBridge.last_config.bot_token, "token")
        self.assertEqual(FakeBridge.last_config.allowed_chat_id, 123)


if __name__ == "__main__":
    unittest.main()
