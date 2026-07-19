"""
Tests for deterministic Italian and English voice command parsing.
"""

import unittest

from voice_commands import VoiceCommandError, parse_voice_command


class VoiceCommandTests(unittest.TestCase):
    """Regression tests for speech transcript to Durex command parsing."""

    def test_status_italian(self):
        """Italian status words should map to the status action."""

        command = parse_voice_command("stato")

        self.assertEqual(command.action, "status")

    def test_status_english(self):
        """English status words should map to the status action."""

        command = parse_voice_command("status")

        self.assertEqual(command.action, "status")

    def test_tasks_with_italian_limit(self):
        """Italian task list commands can include a spoken limit."""

        command = parse_voice_command("lista task cinque")

        self.assertEqual(command.action, "tasks")
        self.assertEqual(command.limit, 5)

    def test_tail_with_english_task_id(self):
        """English tail commands can target a spoken task id."""

        command = parse_voice_command("show output task five")

        self.assertEqual(command.action, "tail")
        self.assertEqual(command.task_id, 5)

    def test_run_italian(self):
        """Italian run commands should start the worker."""

        command = parse_voice_command("avvia worker")

        self.assertEqual(command.action, "run")

    def test_command_alias_maps_learned_phrase(self):
        """Learned phrases should map to simple command actions."""

        command = parse_voice_command("abbia walker", command_aliases={"abbia walker": "run"})

        self.assertEqual(command.action, "run")
        self.assertEqual(command.transcript, "abbia walker")

    def test_add_like_alias_runs_after_structured_parse_failure(self):
        """An exact safe alias should recover an incomplete add-like transcript."""

        command = parse_voice_command(
            "add task nonsense",
            command_aliases={"add task nonsense": "run"},
        )

        self.assertEqual(command.action, "run")

    def test_add_like_phrase_keeps_structured_error_without_alias(self):
        """Incomplete add phrases should retain their detailed error without an alias."""

        with self.assertRaisesRegex(VoiceCommandError, "missing title/titolo"):
            parse_voice_command("add task nonsense")

    def test_stop_english(self):
        """English stop commands should request cooperative worker stop."""

        command = parse_voice_command("stop worker")

        self.assertEqual(command.action, "stop")

    def test_add_italian_with_alias(self):
        """Italian add commands should extract title, alias workdir and prompt."""

        command = parse_voice_command(
            "aggiungi task titolo smoke test cartella durex priorita uno prompt leggi il readme",
            workdir_aliases={"durex": "/lab/durex"},
        )

        self.assertEqual(command.action, "add")
        self.assertEqual(command.title, "smoke test")
        self.assertEqual(command.workdir, "/lab/durex")
        self.assertEqual(command.priority, 1)
        self.assertEqual(command.prompt, "leggi il readme")

    def test_add_english_with_directory(self):
        """English add commands should extract title, directory and prompt."""

        command = parse_voice_command(
            "add task title smoke test directory /lab/durex priority two prompt read the readme",
        )

        self.assertEqual(command.action, "add")
        self.assertEqual(command.title, "smoke test")
        self.assertEqual(command.workdir, "/lab/durex")
        self.assertEqual(command.priority, 2)
        self.assertEqual(command.prompt, "read the readme")

    def test_add_rejects_missing_prompt(self):
        """Add commands without a prompt must be rejected."""

        with self.assertRaises(VoiceCommandError):
            parse_voice_command("add task title smoke test directory /lab/durex")

    def test_rejects_unknown_command(self):
        """Unrecognized transcripts should not execute anything."""

        with self.assertRaises(VoiceCommandError):
            parse_voice_command("do something with the project")


if __name__ == "__main__":
    unittest.main()
