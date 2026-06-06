"""
Tests for queue output parsing helpers.
"""

import os
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
