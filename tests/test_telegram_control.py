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
from voice_transcriber import StaticVoiceTranscriber, TranscriptionResult


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

    def get_file(self, file_id):
        """Return deterministic fake Telegram file metadata."""

        return {"file_path": f"voice/{file_id}.ogg"}

    def download_file(self, file_path, destination):
        """Write a small fake voice file and return its path."""

        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"fake voice")
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
            {"message": {"chat": {"id": 123}, "text": "/learn run abbia walker"}}
        )
        voice_response = bot.handle_update(
            {"message": {"chat": {"id": 123}, "voice": {"file_id": "voice-learned"}}}
        )

        self.assertEqual(learn_response, "Learned voice alias: 'abbia walker' -> run")
        self.assertIn("Worker started.", voice_response)
        self.assertIn('"run"', Path(alias_file).read_text(encoding="utf-8"))
        self.assertIn("abbia walker", Path(alias_file).read_text(encoding="utf-8"))

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
