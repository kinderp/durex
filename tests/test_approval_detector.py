"""
Tests for approval_detector.py.

These tests focus on the most fragile part of the PTY bridge: turning terminal
text into a normalized ApprovalRequest.
"""

from approval_detector import (
    detect_approval_request,
    extract_command,
    make_request_id,
    redact_for_display,
    strip_ansi,
)


def test_strip_ansi_removes_color_sequences():
    raw = "\x1b[31mApprove?\x1b[0m [y/N]"
    assert strip_ansi(raw) == "Approve? [y/N]"


def test_detects_basic_y_n_prompt_with_shell_command():
    text = """
Codex wants to run:
$ pytest -q

Approve this command? [y/N]
"""

    request = detect_approval_request(text)

    assert request is not None
    assert request.command == "pytest -q"
    assert "approval" in request.reason.lower()
    assert request.request_id


def test_extracts_command_from_label():
    text = """
Command: python -m pytest tests
Continue? (y/n)
"""

    assert extract_command(text) == "python -m pytest tests"


def test_extracts_command_from_fenced_block():
    text = """
Codex proposes:
```bash
ruff check .
```
Approve? [y/N]
"""

    assert extract_command(text) == "ruff check ."


def test_returns_none_when_no_approval_prompt_exists():
    text = "All tests passed. Nothing to approve here."
    assert detect_approval_request(text) is None


def test_redacts_obvious_secret_values():
    text = "curl example.com token=abc123 password=secret Bearer xyz"
    redacted = redact_for_display(text)

    assert "abc123" not in redacted
    assert "secret" not in redacted
    assert "xyz" not in redacted
    assert "<redacted>" in redacted


def test_request_id_is_stable_for_same_command_and_context():
    context = "Command: pytest -q\nApprove? [y/N]"
    first = make_request_id("pytest -q", context)
    second = make_request_id("pytest -q", context)

    assert first == second


def test_request_id_changes_when_command_changes():
    context = "Approve? [y/N]"
    first = make_request_id("pytest -q", context)
    second = make_request_id("ruff check .", context)

    assert first != second
