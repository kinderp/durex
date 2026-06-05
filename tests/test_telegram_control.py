"""
Tests for Telegram remote control command routing.
"""

import tempfile
from pathlib import Path
import unittest

import codex_queue
from telegram_bridge import TelegramBridgeConfig
from telegram_control import (
    TelegramControlError,
    TelegramControlBot,
    TelegramControlConfig,
    parse_add_command,
    path_is_allowed,
)


class FakeBridge:
    def __init__(self, chat_id=123):
        self.config = TelegramBridgeConfig(bot_token="fake", allowed_chat_id=chat_id)
        self.messages = []

    def send_message(self, text, reply_markup=None):
        self.messages.append(text)
        return len(self.messages)


class TelegramControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = codex_queue.DB_PATH
        codex_queue.DB_PATH = str(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.restore_db_path)

    def restore_db_path(self):
        codex_queue.DB_PATH = self.old_db_path

    def test_parse_add_command(self):
        parsed = parse_add_command(
            '/add --title "Fix tests" --workdir /tmp --priority 5\nRun tests and fix failures.'
        )

        self.assertEqual(parsed.title, "Fix tests")
        self.assertEqual(parsed.priority, 5)
        self.assertEqual(parsed.prompt, "Run tests and fix failures.")

    def test_parse_add_command_accepts_bot_suffix(self):
        parsed = parse_add_command('/add@DurexBot --title "Fix"\nRun tests.')

        self.assertEqual(parsed.title, "Fix")
        self.assertEqual(parsed.prompt, "Run tests.")

    def test_parse_add_command_reports_syntax_error(self):
        with self.assertRaises(TelegramControlError):
            parse_add_command('/add --priority nope\nRun tests.')

    def test_path_is_allowed_accepts_child_directory(self):
        root = str(Path(self.tmp.name).resolve())
        child = str((Path(self.tmp.name) / "project").resolve())

        self.assertTrue(path_is_allowed(child, [root]))
        self.assertFalse(path_is_allowed("/etc", [root]))

    def test_handle_add_message_creates_task(self):
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
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update({"message": {"chat": {"id": 999}, "text": "/status"}})

        self.assertIsNone(response)
        self.assertEqual(bridge.messages, [])

    def test_status_accepts_bot_suffix(self):
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update({"message": {"chat": {"id": 123}, "text": "/status@DurexBot"}})

        self.assertIn("Durex status", response)


if __name__ == "__main__":
    unittest.main()
