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
    load_voice_command_aliases,
    parse_add_command,
    path_is_allowed,
    save_voice_command_alias,
)
from voice_transcriber import StaticVoiceTranscriber, TranscriptionResult


class FakeBridge:
    """Minimal Telegram bridge double used by control-bot tests."""

    def __init__(self, chat_id=123):
        """Create a fake bridge authorized for one chat id."""

        self.config = TelegramBridgeConfig(bot_token="fake", allowed_chat_id=chat_id)
        self.messages = []
        self.reply_markups = []
        self.callback_answers = []
        self.downloaded_files = []

    def send_message(self, text, reply_markup=None):
        """Record outgoing messages and return a deterministic message id."""

        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return len(self.messages)

    def answer_callback_query(self, callback_query_id, text=None):
        """Record callback acknowledgements."""

        self.callback_answers.append((callback_query_id, text))

    def get_file(self, file_id):
        """Return deterministic fake Telegram file metadata."""

        return {"file_path": f"voice/{file_id}.ogg"}

    def download_file(self, file_path, destination, max_bytes=None):
        """Write a small fake voice file and return its path."""

        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"fake voice")
        self.downloaded_files.append(destination)
        return destination


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


class FailingDownloadBridge(FakeBridge):
    """Bridge double that fails after receiving a temporary destination."""

    def download_file(self, file_path, destination, max_bytes=None):
        """Record the destination and simulate a Telegram download failure."""

        self.downloaded_files.append(destination)
        raise TelegramBridgeError("download failed")


class MappingVoiceTranscriber:
    """Voice transcriber double that can vary output by requested language."""

    provider_name = "mapping"

    def __init__(self, responses):
        """Store language-keyed transcription responses."""

        self.responses = responses
        self.calls = []

    def transcribe(self, audio_path, language=None):
        """Return the response configured for the requested language."""

        self.calls.append(language)
        text, detected_language = self.responses[language]
        return TranscriptionResult(
            text=text,
            language=detected_language,
            provider=self.provider_name,
            confidence=1.0,
        )


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

    def test_parse_add_command_rejects_unknown_option_after_prompt(self):
        """Unknown trailing options after --prompt should not be ignored."""

        with self.assertRaises(TelegramControlError):
            parse_add_command('/add --title "Fix" --prompt "Run tests." --unknown value')

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

    def test_remote_add_rejects_oversized_priority(self):
        """SQLite-sized validation should reject oversized Telegram priority values."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )
        huge = "9" * 100

        response = bot.handle_update(
            {
                "message": {
                    "chat": {"id": 123},
                    "text": f"/add --workdir {self.tmp.name} --priority {huge} --prompt test",
                }
            }
        )

        self.assertIn("Command rejected: Priority must be between", response)

    def test_remote_add_rejects_oversized_max_attempts(self):
        """SQLite-sized validation should reject oversized retry counts."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )
        huge = "9" * 100

        response = bot.handle_update(
            {
                "message": {
                    "chat": {"id": 123},
                    "text": f"/add --workdir {self.tmp.name} --max-attempts {huge} --prompt test",
                }
            }
        )

        self.assertIn("Command rejected: Max attempts must be between 1", response)

    def test_remote_tasks_limit_is_bounded(self):
        """Task lists should reject oversized values and accept the documented maximum."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        accepted = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/tasks 50"}}
        )
        rejected = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/tasks 51"}}
        )
        huge = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": f"/tasks {'9' * 100}"}}
        )

        self.assertEqual(accepted, "No tasks found.")
        self.assertIn("Task limit must be between 1 and 50", rejected)
        self.assertIn("Task limit must be between 1 and 50", huge)

    def test_remote_tail_rejects_oversized_task_id(self):
        """Task output lookup should reject ids outside SQLite's integer range."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": f"/tail {'9' * 100}"}}
        )

        self.assertIn("Command rejected: Task id must be between 1", response)

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

    def test_ignores_unauthorized_callback(self):
        """Inline controls from another chat must not trigger worker actions."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update(
            {
                "callback_query": {
                    "id": "unauthorized-callback",
                    "message": {"chat": {"id": 999}},
                    "data": "durexcontrol:run",
                }
            }
        )

        self.assertIsNone(response)
        self.assertFalse(bot.worker_state.is_running())
        self.assertEqual(bridge.messages, [])
        self.assertEqual(bridge.callback_answers, [])

    def test_status_accepts_bot_suffix(self):
        """Status command parsing should support @BotName suffixes."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update({"message": {"chat": {"id": 123}, "text": "/status@DurexBot"}})

        self.assertIn("Durex status", response)

    def test_voice_message_is_rejected_when_disabled(self):
        """Voice attachments should be ignored unless voice commands are enabled."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-1"}}}
        )

        self.assertIn("Voice commands are disabled", response)

    def test_failed_voice_download_removes_temporary_file(self):
        """A failed Telegram download should not leave its temporary file behind."""

        bridge = FailingDownloadBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name], voice_enabled=True),
            voice_transcriber=StaticVoiceTranscriber("stato", language="it"),
        )

        with self.assertRaisesRegex(TelegramBridgeError, "download failed"):
            bot.download_voice_message({"file_id": "voice-download-failure"})

        self.assertFalse(Path(bridge.downloaded_files[-1]).exists())

    def test_failed_voice_download_returns_command_error(self):
        """Telegram download errors should be reported without escaping the router."""

        bridge = FailingDownloadBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name], voice_enabled=True),
            voice_transcriber=StaticVoiceTranscriber("stato", language="it"),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-download-failure"}}}
        )

        self.assertEqual(response, "Command rejected: download failed")
        self.assertEqual(bridge.messages[-1], response)

    def test_voice_metadata_rejects_oversized_duration_before_download(self):
        """Voice duration limits should reject work before downloading audio."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_max_duration_seconds=10,
            ),
            voice_transcriber=StaticVoiceTranscriber("stato", language="it"),
        )

        response = bot.handle_update(
            {
                "message": {
                    "chat": {"id": 123},
                    "voice": {"file_id": "voice-long", "duration": 11},
                }
            }
        )

        self.assertIn("configured 10-second limit", response)
        self.assertEqual(bridge.downloaded_files, [])

    def test_voice_metadata_rejects_oversized_file_before_download(self):
        """Voice byte limits should reject declared oversized attachments."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_max_file_bytes=5,
            ),
            voice_transcriber=StaticVoiceTranscriber("stato", language="it"),
        )

        response = bot.handle_update(
            {
                "message": {
                    "chat": {"id": 123},
                    "voice": {"file_id": "voice-large", "file_size": 6},
                }
            }
        )

        self.assertIn("configured 5-byte limit", response)
        self.assertEqual(bridge.downloaded_files, [])

    def test_voice_status_command(self):
        """A transcribed Italian status voice command should route to status."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name], voice_enabled=True),
            voice_transcriber=StaticVoiceTranscriber("stato", language="it"),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-1"}}}
        )

        self.assertIn("Voice transcript: stato", response)
        self.assertIn("Durex status", response)
        self.assertFalse(Path(bridge.downloaded_files[-1]).exists())

    def test_voice_add_command_uses_alias_and_creates_task(self):
        """A transcribed add command should use aliases and enqueue a task."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_workdir_aliases={"temp": self.tmp.name},
            ),
            voice_transcriber=StaticVoiceTranscriber(
                "aggiungi task titolo prova vocale cartella temp priorita uno prompt leggi readme",
                language="it",
            ),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-2"}}}
        )

        self.assertIn("Task added: prova vocale", response)
        with codex_queue.connect() as con:
            row = con.execute("SELECT title, prompt, workdir, priority FROM tasks").fetchone()
        self.assertEqual(row[0], "prova vocale")
        self.assertEqual(row[1], "leggi readme")
        self.assertEqual(row[2], str(Path(self.tmp.name).resolve()))
        self.assertEqual(row[3], 1)

    def test_learn_command_persists_voice_alias(self):
        """Text /learn should save a safe voice alias and activate it immediately."""

        alias_file = str(Path(self.tmp.name) / "voice_aliases.json")
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_aliases_file=alias_file,
            ),
            voice_transcriber=StaticVoiceTranscriber("abbia walker", language="it"),
        )

        learn_response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/learn status abbia walker"}}
        )
        voice_response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-learned"}}}
        )

        self.assertEqual(learn_response, "Learned voice alias: 'abbia walker' -> status")
        self.assertIn("Durex status", voice_response)
        self.assertIn('"status"', Path(alias_file).read_text(encoding="utf-8"))
        self.assertIn("abbia walker", Path(alias_file).read_text(encoding="utf-8"))

    def test_reassigned_voice_alias_remains_stable_after_reload(self):
        """Relearning a phrase should replace its previous persisted action."""

        alias_file = str(Path(self.tmp.name) / "voice_aliases.json")

        save_voice_command_alias(alias_file, "status", "same phrase")
        save_voice_command_alias(alias_file, "run", "same phrase")

        persisted = Path(alias_file).read_text(encoding="utf-8")
        reloaded = load_voice_command_aliases(alias_file)
        self.assertEqual(persisted.count("same phrase"), 1)
        self.assertNotIn('"status"', persisted)
        self.assertEqual(reloaded["same phrase"], "run")

    def test_learn_command_rejects_add_alias(self):
        """Learned aliases should not target structured add commands."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/learn add crea roba"}}
        )

        self.assertIn("Unsupported learn action", response)

    def test_learn_command_rejects_conflicting_builtin_phrase(self):
        """Text learning should not claim a built-in phrase changed actions."""

        alias_file = str(Path(self.tmp.name) / "voice_aliases.json")
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_aliases_file=alias_file,
            ),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/learn run status"}}
        )

        self.assertIn("already maps to built-in action 'status'", response)
        self.assertFalse(Path(alias_file).exists())

    def test_learn_callback_rejects_conflicting_builtin_phrase(self):
        """Inline learning should apply the same built-in collision rule."""

        alias_file = str(Path(self.tmp.name) / "voice_aliases.json")
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_aliases_file=alias_file,
            ),
        )
        keyboard = bot.build_voice_learn_keyboard("status")
        callback = keyboard["inline_keyboard"][1][1]["callback_data"]

        response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-conflict",
                    "message": {"chat": {"id": 123}},
                    "data": callback,
                }
            }
        )

        self.assertIn("already maps to built-in action 'status'", response)
        self.assertFalse(Path(alias_file).exists())

    def test_learn_command_allows_same_builtin_action(self):
        """Learning a phrase for its existing action should remain a safe no-op."""

        alias_file = str(Path(self.tmp.name) / "voice_aliases.json")
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_aliases_file=alias_file,
            ),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/learn status status"}}
        )

        self.assertEqual(response, "Learned voice alias: 'status' -> status")
        self.assertEqual(load_voice_command_aliases(alias_file)["status"], "status")

    def test_tasks_command_sends_task_buttons_and_detail_callback(self):
        """Task list should expose inline buttons for task details."""

        codex_queue.init_db()
        codex_queue.add_task(
            title="Button task",
            prompt="Do work",
            workdir=self.tmp.name,
            priority=10,
        )
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update({"message": {"chat": {"id": 123}, "text": "/tasks"}})
        keyboard = bridge.reply_markups[-1]
        detail_callback = keyboard["inline_keyboard"][0][0]["callback_data"]
        detail_response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-detail",
                    "message": {"chat": {"id": 123}},
                    "data": detail_callback,
                }
            }
        )

        self.assertIn("Button task", response)
        self.assertEqual(detail_callback, "durextask:1:details")
        self.assertIn("Task #1", detail_response)
        self.assertIn("Title: Button task", detail_response)
        self.assertEqual(bridge.callback_answers[-1], ("callback-detail", "Task #1"))
        self.assertIsNotNone(bridge.reply_markups[-1])

    def test_task_tail_callback_returns_output(self):
        """Task detail tail button should return task output."""

        codex_queue.init_db()
        codex_queue.add_task(
            title="Output task",
            prompt="Do work",
            workdir=self.tmp.name,
            priority=10,
        )
        with codex_queue.connect() as con:
            con.execute("UPDATE tasks SET output = ? WHERE id = 1", ("hello output",))
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name]),
        )

        response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-tail",
                    "message": {"chat": {"id": 123}},
                    "data": "durextask:1:tail",
                }
            }
        )

        self.assertIn("hello output", response)
        self.assertEqual(bridge.callback_answers[-1], ("callback-tail", "Output"))

    def test_add_wizard_creates_task_with_buttons_and_text_prompt(self):
        """Guided add wizard should create a task without a complex voice command."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                workdir_choices={"temp": self.tmp.name},
            ),
        )

        start_response = bot.handle_update({"message": {"chat": {"id": 123}, "text": "/add-wizard"}})
        workdir_callback = bridge.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"]
        priority_response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-workdir",
                    "message": {"chat": {"id": 123}},
                    "data": workdir_callback,
                }
            }
        )
        token = workdir_callback.split(":")[1]
        preset_response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-preset",
                    "message": {"chat": {"id": 123}},
                    "data": f"durexadd:{token}:preset:10",
                }
            }
        )
        increment_response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-inc",
                    "message": {"chat": {"id": 123}},
                    "data": f"durexadd:{token}:inc:5",
                }
            }
        )
        prompt_request = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-prompt",
                    "message": {"chat": {"id": 123}},
                    "data": f"durexadd:{token}:prompt",
                }
            }
        )
        confirm_response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "Read the README and summarize Durex."}}
        )
        create_response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-create",
                    "message": {"chat": {"id": 123}},
                    "data": f"durexadd:{token}:create",
                }
            }
        )

        self.assertEqual(start_response, "New task: choose workdir.")
        self.assertIn("Current priority: 100", priority_response)
        self.assertIn("Current priority: 10", preset_response)
        self.assertIn("Current priority: 15", increment_response)
        self.assertEqual(prompt_request, "Send the task prompt as a text message or voice message.")
        self.assertIn("Create task?", confirm_response)
        self.assertEqual(create_response, "Task added: Read the README and summarize Durex.")
        with codex_queue.connect() as con:
            row = con.execute("SELECT title, prompt, workdir, priority FROM tasks").fetchone()
        self.assertEqual(row[0], "Read the README and summarize Durex.")
        self.assertEqual(row[1], "Read the README and summarize Durex.")
        self.assertEqual(row[2], str(Path(self.tmp.name).resolve()))
        self.assertEqual(row[3], 15)

    def test_add_wizard_accepts_voice_prompt(self):
        """Guided add wizard should accept a voice message as the task prompt."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                workdir_choices={"temp": self.tmp.name},
            ),
            voice_transcriber=StaticVoiceTranscriber("leggi il readme", language="it"),
        )

        bot.handle_update({"message": {"chat": {"id": 123}, "text": "/add-wizard"}})
        workdir_callback = bridge.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"]
        token = workdir_callback.split(":")[1]
        bot.handle_update(
            {"callback_query": {"id": "callback-workdir", "message": {"chat": {"id": 123}}, "data": workdir_callback}}
        )
        bot.handle_update(
            {"callback_query": {"id": "callback-prompt", "message": {"chat": {"id": 123}}, "data": f"durexadd:{token}:prompt"}}
        )
        confirm_response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-prompt"}}}
        )

        self.assertIn("Prompt: leggi il readme", confirm_response)

    def test_auto_voice_prompt_accepts_italian_detection(self):
        """Automatic wizard prompts should accept detected Italian without a hint."""

        bridge = FakeBridge(chat_id=123)
        transcriber = MappingVoiceTranscriber({None: ("leggi il readme", "it")})
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_language=None,
                voice_allowed_languages=("it", "en"),
            ),
            voice_transcriber=transcriber,
        )

        transcript = bot.transcribe_prompt_voice({"file_id": "voice-prompt-it"})

        self.assertEqual(transcript, "leggi il readme")
        self.assertEqual(transcriber.calls, [None])

    def test_auto_voice_prompt_accepts_english_detection(self):
        """Automatic wizard prompts should accept detected English without an Italian hint."""

        bridge = FakeBridge(chat_id=123)
        transcriber = MappingVoiceTranscriber({None: ("read the readme", "en")})
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_language=None,
                voice_allowed_languages=("it", "en"),
            ),
            voice_transcriber=transcriber,
        )

        transcript = bot.transcribe_prompt_voice({"file_id": "voice-prompt-en"})

        self.assertEqual(transcript, "read the readme")
        self.assertEqual(transcriber.calls, [None])

    def test_auto_voice_prompt_rejects_disallowed_detection(self):
        """Automatic wizard prompts should reject languages outside the allow list."""

        bridge = FakeBridge(chat_id=123)
        transcriber = MappingVoiceTranscriber({None: ("bonjour", "fr")})
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_language=None,
                voice_allowed_languages=("it", "en"),
            ),
            voice_transcriber=transcriber,
        )

        with self.assertRaisesRegex(TelegramControlError, "language fr is not allowed"):
            bot.transcribe_prompt_voice({"file_id": "voice-prompt-fr"})

        self.assertFalse(Path(bridge.downloaded_files[-1]).exists())

    def test_explicit_voice_prompt_language_remains_a_hint(self):
        """An explicitly configured prompt language should be passed to Whisper."""

        bridge = FakeBridge(chat_id=123)
        transcriber = MappingVoiceTranscriber({"en": ("read the readme", "en")})
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_language="en",
                voice_allowed_languages=("it", "en"),
            ),
            voice_transcriber=transcriber,
        )

        transcript = bot.transcribe_prompt_voice({"file_id": "voice-prompt-explicit"})

        self.assertEqual(transcript, "read the readme")
        self.assertEqual(transcriber.calls, ["en"])

    def test_config_view_toggles_voice_debug(self):
        """Config view should expose checkbox-style toggles."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(allowed_workdirs=[self.tmp.name], voice_debug=False),
        )

        response = bot.handle_update({"message": {"chat": {"id": 123}, "text": "/config"}})
        callback = bridge.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"]
        toggled = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-config",
                    "message": {"chat": {"id": 123}},
                    "data": callback,
                }
            }
        )

        self.assertEqual(response, "Durex config")
        self.assertIn("Voice debug: ON", bridge.reply_markups[-1]["inline_keyboard"][0][0]["text"])
        self.assertEqual(toggled, "Durex config")
        self.assertTrue(bot.config.voice_debug)

    def test_from_env_loads_configured_workdir_choices(self):
        """telegram-control should read workdir choices from YAML config."""

        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "telegram_control:",
                    "  allowed_workdirs:",
                    f"    - {self.tmp.name}",
                    "  workdir_choices:",
                    f"    temp: {self.tmp.name}",
                    "  voice:",
                    "    enabled: false",
                ]
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(
            os.environ,
            {
                "DUREX_TELEGRAM_BOT_TOKEN": "token",
                "DUREX_TELEGRAM_CHAT_ID": "123",
                "DUREX_CONFIG": str(config_path),
            },
            clear=True,
        ):
            bot = TelegramControlBot.from_env()

        self.assertEqual(bot.config.allowed_workdirs, [str(Path(self.tmp.name).resolve())])
        self.assertEqual(bot.config.workdir_choices, {"temp": str(Path(self.tmp.name).resolve())})

    def test_from_env_voice_environment_values_override_yaml_booleans(self):
        """Explicit environment booleans should override YAML in both directions."""

        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "telegram_control:",
                    "  voice:",
                    "    enabled: false",
                    "    debug: true",
                    "    max_file_bytes: 1024",
                    "    max_duration_seconds: 30",
                ]
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(
            os.environ,
            {
                "DUREX_TELEGRAM_BOT_TOKEN": "token",
                "DUREX_TELEGRAM_CHAT_ID": "123",
                "DUREX_CONFIG": str(config_path),
                "DUREX_VOICE_ENABLED": "1",
                "DUREX_VOICE_DEBUG": "0",
                "DUREX_VOICE_MAX_FILE_BYTES": "2048",
                "DUREX_VOICE_MAX_DURATION_SECONDS": "60",
            },
            clear=True,
        ):
            bot = TelegramControlBot.from_env()

        self.assertTrue(bot.config.voice_enabled)
        self.assertFalse(bot.config.voice_debug)
        self.assertEqual(bot.config.voice_max_file_bytes, 2048)
        self.assertEqual(bot.config.voice_max_duration_seconds, 60)

    def test_failed_voice_command_can_be_learned_with_inline_button(self):
        """Voice failures should offer inline buttons that save the selected alias."""

        alias_file = str(Path(self.tmp.name) / "voice_aliases.json")
        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_aliases_file=alias_file,
            ),
            voice_transcriber=StaticVoiceTranscriber("abbia walker", language="it"),
        )

        failed_response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-learn-button"}}}
        )
        keyboard = bridge.reply_markups[-1]
        status_callback = keyboard["inline_keyboard"][0][0]["callback_data"]
        learn_response = bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-1",
                    "message": {"chat": {"id": 123}},
                    "data": status_callback,
                }
            }
        )
        voice_response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-learned-button"}}}
        )

        self.assertIn("Learn candidate: abbia walker", failed_response)
        self.assertIn("Learned voice alias: 'abbia walker' -> status", learn_response)
        self.assertIn("Durex status", voice_response)
        self.assertEqual(bridge.callback_answers, [("callback-1", "Learned")])

    def test_voice_message_reports_unrecognized_transcript_with_detected_language(self):
        """Unrecognized transcripts should report the text instead of failing on language first."""

        bridge = FakeBridge(chat_id=123)
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_allowed_languages=("it", "en"),
            ),
            voice_transcriber=StaticVoiceTranscriber("bonjour", language="fr"),
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-3"}}}
        )

        self.assertIn("Voice command not recognized", response)
        self.assertIn("it: bonjour", response)
        self.assertIn("en: bonjour", response)
        self.assertFalse(Path(bridge.downloaded_files[-1]).exists())

    def test_voice_auto_mode_forces_supported_language_before_free_detection(self):
        """Auto mode should probe supported languages before free language detection."""

        bridge = FakeBridge(chat_id=123)
        transcriber = MappingVoiceTranscriber(
            {
                "it": ("avvia worker", "it"),
                "en": ("start worker", "en"),
                None: ("\u0623\u0628\u0648\u064a\u0627 \u0648\u0627\u0631\u0643\u0631", "ar"),
            }
        )
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_allowed_languages=("it", "en"),
            ),
            voice_transcriber=transcriber,
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-4"}}}
        )

        self.assertIn("Voice transcript: avvia worker", response)
        self.assertIn("Worker started.", response)
        self.assertEqual(transcriber.calls, ["it"])

    def test_voice_tasks_accepts_language_detection_drift(self):
        """Task-list commands should not be blocked by wrong automatic language detection."""

        bridge = FakeBridge(chat_id=123)
        transcriber = MappingVoiceTranscriber(
            {
                "it": ("qualcosa non valido", "it"),
                "en": ("lista task", "en"),
                None: ("lista task", "es"),
            }
        )
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_allowed_languages=("it", "en"),
            ),
            voice_transcriber=transcriber,
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-5"}}}
        )

        self.assertIn("Voice transcript: lista task", response)
        self.assertIn("No tasks found.", response)
        self.assertEqual(transcriber.calls, ["it", "en"])

    def test_voice_debug_includes_transcription_attempts(self):
        """Debug mode should expose the transcripts tried by language."""

        bridge = FakeBridge(chat_id=123)
        transcriber = MappingVoiceTranscriber(
            {
                "it": ("qualcosa non valido", "it"),
                "en": ("status", "en"),
                None: ("status", "en"),
            }
        )
        bot = TelegramControlBot(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=[self.tmp.name],
                voice_enabled=True,
                voice_allowed_languages=("it", "en"),
                voice_debug=True,
            ),
            voice_transcriber=transcriber,
        )

        response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-debug"}}}
        )

        self.assertIn("Voice attempts:", response)
        self.assertIn("it: qualcosa non valido", response)
        self.assertIn("en: status -> status", response)
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
