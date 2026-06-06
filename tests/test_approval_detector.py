"""
Tests for approval_detector.py.

These tests focus on the most fragile part of the PTY bridge: turning terminal
text into a normalized ApprovalRequest.
"""

import unittest

from approval_detector import (
    detect_approval_request,
    extract_command,
    make_request_id,
    prompt_signature,
    redact_for_display,
    strip_ansi,
)


class ApprovalDetectorTests(unittest.TestCase):
    """Regression coverage for PTY text-to-approval conversion contracts."""

    def test_strip_ansi_removes_color_sequences(self):
        """ANSI decoration must not affect prompt detection or display text."""

        raw = "\x1b[31mApprove?\x1b[0m [y/N]"
        self.assertEqual(strip_ansi(raw), "Approve? [y/N]")

    def test_detects_basic_y_n_prompt_with_shell_command(self):
        """A common Codex command prompt should produce a normalized request."""

        text = """
Codex wants to run:
$ pytest -q

Approve this command? [y/N]
"""

        request = detect_approval_request(text)

        self.assertIsNotNone(request)
        self.assertEqual(request.command, "pytest -q")
        self.assertIn("approval", request.reason.lower())
        self.assertTrue(request.request_id)

    def test_extracts_command_from_label(self):
        """Explicit command labels are treated as high-confidence extraction."""

        text = """
Command: python -m pytest tests
Continue? (y/n)
"""

        self.assertEqual(extract_command(text), "python -m pytest tests")

    def test_extracts_command_from_fenced_block(self):
        """Fenced shell blocks take precedence because they preserve intent."""

        text = """
Codex proposes:
```bash
ruff check .
```
Approve? [y/N]
"""

        self.assertEqual(extract_command(text), "ruff check .")

    def test_returns_none_when_no_approval_prompt_exists(self):
        """Normal terminal output must not create approval noise."""

        text = "All tests passed. Nothing to approve here."
        self.assertIsNone(detect_approval_request(text))

    def test_request_id_ignores_unrelated_context_growth_when_command_exists(self):
        """Request ids must stay stable when the same prompt is redrawn."""

        first_context = """
Preparing work
Command: pytest -q
Approve this command? [y/N]
"""
        second_context = """
Preparing work
Still collecting terminal output
Command: pytest -q
Approve this command? [y/N]
"""

        first = make_request_id("pytest -q", first_context)
        second = make_request_id("pytest -q", second_context)

        self.assertEqual(first, second)

    def test_request_id_keeps_generic_unknown_command_prompts_distinct(self):
        """Generic prompts without commands still need distinct context ids."""

        first = make_request_id(
            None,
            """
Plan step: inspect repository
Continue? (y/n)
""",
        )
        second = make_request_id(
            None,
            """
Plan step: modify files
Continue? (y/n)
""",
        )

        self.assertNotEqual(first, second)

    def test_prompt_signature_uses_prompt_line_not_full_context_when_command_exists(self):
        """Fingerprints should depend on command and prompt, not old output."""

        context = """
one
two
Command: ruff check .
Approve this command? [y/N]
"""

        signature = prompt_signature("ruff check .", context)

        self.assertIn("ruff check .", signature)
        self.assertIn("Approve this command? [y/N]", signature)
        self.assertNotIn("one", signature)
        self.assertNotIn("two", signature)

    def test_redacts_obvious_secret_values(self):
        """Telegram-facing text should remove common secret patterns."""

        text = "curl example.com token=abc123 password=secret Bearer xyz"
        redacted = redact_for_display(text)

        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertIn("<redacted>", redacted)

    def test_request_id_is_stable_for_same_command_and_context(self):
        """Identical approval prompts should deduplicate to the same id."""

        context = "Command: pytest -q\nApprove? [y/N]"
        first = make_request_id("pytest -q", context)
        second = make_request_id("pytest -q", context)

        self.assertEqual(first, second)

    def test_request_id_changes_when_command_changes(self):
        """Different commands must not share an approval fingerprint."""

        context = "Approve? [y/N]"
        first = make_request_id("pytest -q", context)
        second = make_request_id("ruff check .", context)

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
