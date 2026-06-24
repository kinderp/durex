"""
Tests for Telegram remote control command routing.
"""

import tempfile
from pathlib import Path
import os
import unittest
from unittest import mock

import codex_queue
from telegram_bridge import TelegramBridgeConfig, TelegramBridgeError
from telegram_control import (
    TelegramControlError,
    TelegramControlBot,
    TelegramControlConfig,
    parse_add_command,
    path_is_allowed,
)


class FakeBridge:
    """Minimal Telegram bridge double used by control-bot tests."""

    def __init__(self, chat_id=123):
        """Create a fake bridge authorized for one chat id."""

        self.config = TelegramBridgeConfig(bot_token="fake", allowed_chat_id=chat_id)
        self.messages = []

    def send_message(self, text, reply_markup=None):
        """Record outgoing messages and return a deterministic message id."""

        self.messages.append(text)
        return len(self.messages)


class FlakyBridge(FakeBridge):
    """Bridge double that simulates one transient polling failure."""

    def __init__(self, chat_id=123):
        """Initialize the flaky bridge and reset its poll counter."""

        super().__init__(chat_id=chat_id)
        self.poll_calls = 0

    def poll_updates(self, timeout=20, allowed_updates=None):
        """Fail once, then return a status command, then stop the daemon."""

        self.poll_calls += 1
        if self.poll_calls == 1:
            raise TelegramBridgeError("temporary network failure")
        if self.poll_calls == 2:
            return [{"message": {"chat": {"id": 123}, "text": "/status"}}]
        raise KeyboardInterrupt


class TelegramControlTests(unittest.TestCase):
    """Regression coverage for Telegram remote-control routing and safety."""

    def setUp(self):
        """Use an isolated temporary SQLite database for each test."""

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = codex_queue.DB_PATH
        codex_queue.DB_PATH = str(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.restore_db_path)

    def restore_db_path(self):
        """Restore the global queue database path after a test."""

        codex_queue.DB_PATH = self.old_db_path

    def test_parse_add_command(self):
        """The /add grammar should parse title, workdir, priority and prompt."""

        parsed = parse_add_command(
            '/add --title "Fix tests" --workdir /tmp --priority 5\nRun tests and fix failures.'
        )

        self.assertEqual(parsed.title, "Fix tests")
        self.assertEqual(parsed.priority, 5)
        self.assertEqual(parsed.prompt, "Run tests and fix failures.")

    def test_parse_add_command_accepts_bot_suffix(self):
        """Bot-suffixed Telegram commands should work in group contexts."""

        parsed = parse_add_command('/add@DurexBot --title "Fix"\nRun tests.')

        self.assertEqual(parsed.title, "Fix")
        self.assertEqual(parsed.prompt, "Run tests.")

    def test_parse_add_command_accepts_prompt_option(self):
        """Single-line Telegram clients can pass the prompt with --prompt."""

        parsed = parse_add_command('/add --title "Fix" --prompt "Run tests."')

        self.assertEqual(parsed.title, "Fix")
        self.assertEqual(parsed.prompt, "Run tests.")

    def test_parse_add_command_accepts_double_dash_prompt(self):
        """Text after -- should be treated as the prompt body."""

        parsed = parse_add_command('/add --title "Fix" -- Run tests.')

        self.assertEqual(parsed.title, "Fix")
        self.assertEqual(parsed.prompt, "Run tests.")

    def test_parse_add_command_accepts_plain_inline_prompt(self):
        """Plain trailing text should work for mobile Telegram clients."""

        parsed = parse_add_command('/add --title "Fix" Run tests.')

        self.assertEqual(parsed.title, "Fix")
        self.assertEqual(parsed.prompt, "Run tests.")

    def test_parse_add_command_reports_syntax_error(self):
        """Argparse failures should become TelegramControlError exceptions."""

        with self.assertRaises(TelegramControlError):
            parse_add_command('/add --priority nope\nRun tests.')

    def test_parse_add_command_rejects_unknown_option(self):
        """Unknown options should not be silently treated as prompt text."""

        with self.assertRaises(TelegramControlError):
            parse_add_command('/add --unknown value')

    def test_path_is_allowed_accepts_child_directory(self):
        """Allowed workdir roots should authorize descendants only."""

        root = str(Path(self.tmp.name).resolve())
        child = str((Path(self.tmp.name) / "project").resolve())

        self.assertTrue(path_is_allowed(child, [root]))
        self.assertFalse(path_is_allowed("/etc", [root]))

    def test_handle_add_message_creates_task(self):
        """Authorized /add messages should enqueue a persistent task."""

        bridge = FakeBridge()
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )
        response = bot.handle_update(
            {
                "message": {
                    "chat": {"id": 123},
                    "text": f'/add --title "Remote task" --workdir {self.tmp.name}\nDo the work.',
                }
            }
        )

        self.assertEqual(response, "Task added: Remote task")
        self.assertEqual(bridge.messages[-1], "Task added: Remote task")

        codex_queue.init_db()
        with codex_queue.connect() as con:
            row = con.execute("SELECT title, prompt, workdir FROM tasks").fetchone()

        self.assertEqual(row[0], "Remote task")
        self.assertEqual(row[1], "Do the work.")
        self.assertEqual(row[2], str(Path(self.tmp.name).resolve()))

    def test_handle_add_message_rejects_disallowed_workdir(self):
        """Remote users must not enqueue tasks outside allowed workdir roots."""

        bridge = FakeBridge()
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )
        response = bot.handle_update(
            {
                "message": {
                    "chat": {"id": 123},
                    "text": '/add --title "Bad" --workdir /etc\nDo the work.',
                }
            }
        )

        self.assertTrue(response.startswith("Command rejected: Workdir is not allowed"))

    def test_ignores_unauthorized_chat(self):
        """Messages from other chats must be ignored without a response."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update({"message": {"chat": {"id": 999}, "text": "/status"}})

        self.assertIsNone(response)
        self.assertEqual(bridge.messages, [])

    def test_status_accepts_bot_suffix(self):
        """Status command parsing should support @BotName suffixes."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update({"message": {"chat": {"id": 123}, "text": "/status@DurexBot"}})

        self.assertIn("Durex status", response)

    def test_worker_telegram_approvals_are_rejected_for_control_mode(self):
        """Control mode must reject competing Telegram getUpdates consumers."""

        with mock.patch.dict(
            os.environ,
            {"DUREX_TELEGRAM_BOT_TOKEN": "token", "DUREX_TELEGRAM_CHAT_ID": "123"},
            clear=True,
        ):
            with self.assertRaises(TelegramBridgeError):
                TelegramControlBot.from_env(worker_telegram_approvals=True)

    def test_run_forever_retries_after_temporary_poll_error(self):
        """Transient Telegram polling errors should not kill the daemon loop."""

        bridge = FlakyBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                retry_base_seconds=0,
                retry_max_seconds=0,
            ),
        )

        with self.assertRaises(KeyboardInterrupt):
            bot.run_forever()

        self.assertEqual(bridge.poll_calls, 3)
        self.assertIn("temporary network failure", bot.worker_state.last_error)
        self.assertTrue(any(message.startswith("Durex status") for message in bridge.messages))


if __name__ == "__main__":
    unittest.main()
