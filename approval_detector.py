#!/usr/bin/env python3
"""
approval_detector.py

Text-based approval detector used by the PTY runner.

The PTY runner sees Codex exactly as a human would see it in a terminal.
That means it receives plain terminal output, ANSI colors, spinners and
multiline prompts. This module keeps the detection logic isolated so the
runner does not need to know anything about Codex prompt wording.

The detector is intentionally conservative:
- it only triggers when the recent terminal output contains an approval marker;
- it tries to extract a likely command from the nearby lines;
- if no command is found, it still creates an approval request with context.

This is not as stable as structured JSON events, but it works with interactive
terminal programs that ask questions such as "[y/N]" or "Approve?".
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CONTROL_RE = re.compile(r"[\r\f\v]")


@dataclass
class ApprovalRequest:
    """
    A normalized representation of a terminal approval request.

    command:
        The command we think Codex wants to run. It can be None when the
        detector sees a generic prompt but cannot confidently extract it.

    reason:
        A short explanation of why the detector thinks approval is needed.

    context:
        The recent terminal text around the approval prompt.
    """

    command: Optional[str]
    reason: str
    context: str


def strip_ansi(text: str) -> str:
    """
    Remove ANSI escape sequences and carriage-control characters.
    """

    text = ANSI_RE.sub("", text)
    text = CONTROL_RE.sub("\n", text)
    return text


def tail_lines(text: str, max_lines: int = 40) -> str:
    """
    Return only the last max_lines lines of text.
    """

    lines = strip_ansi(text).splitlines()
    return "\n".join(lines[-max_lines:])


def looks_like_approval_prompt(text: str) -> bool:
    """
    Detect whether recent terminal output looks like an approval prompt.
    """

    recent = tail_lines(text, 50).lower()

    patterns = [
        r"\[[yn]/[yn]\]",
        r"\(y/n\)",
        r"\(yes/no\)",
        r"\byes/no\b",
        r"\bapprove\b",
        r"\bapproval\b",
        r"\bproceed\b.*\?",
        r"\bcontinue\b.*\?",
        r"\brun\b.*\bcommand\b.*\?",
        r"\bexecute\b.*\bcommand\b.*\?",
        r"\ballow\b.*\?",
    ]

    return any(re.search(pattern, recent, re.IGNORECASE) for pattern in patterns)


def extract_command(text: str) -> Optional[str]:
    """
    Try to extract the command being requested from recent output.

    Because a PTY bridge only sees terminal text, there is no guaranteed schema.
    We therefore use several simple heuristics: labels, shell-like lines and
    fenced command blocks.
    """

    recent = tail_lines(text, 60)
    lines = [line.strip() for line in recent.splitlines() if line.strip()]
    joined = "\n".join(lines)

    fenced = re.search(r"```(?:bash|sh|shell)?\s*(.*?)```", joined, re.DOTALL | re.IGNORECASE)
    if fenced:
        command = fenced.group(1).strip()
        if command:
            return command

    label_patterns = [
        r"^(?:command|run|execute)\s*:\s*(.+)$",
        r"^(?:codex wants to run|codex would like to run)\s*:?\s*(.+)$",
    ]

    for line in reversed(lines):
        for pattern in label_patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                command = match.group(1).strip()
                if command:
                    return command

    for line in reversed(lines):
        if line.startswith("$ "):
            return line[2:].strip()

    shellish_prefixes = (
        "pytest", "python ", "python3 ", "npm ", "pnpm ", "yarn ", "git ",
        "ruff ", "mypy ", "docker ", "curl ", "wget ", "pip ", "pip3 ",
        "bash ", "sh ", "mv ", "cp ", "chmod ",
    )

    for line in reversed(lines):
        if line.startswith(shellish_prefixes):
            return line

    return None


def detect_approval_request(text: str) -> Optional[ApprovalRequest]:
    """
    Return an ApprovalRequest if recent output looks like an approval prompt.
    """

    if not looks_like_approval_prompt(text):
        return None

    context = tail_lines(text, 30)
    command = extract_command(context)

    return ApprovalRequest(
        command=command,
        reason="The terminal output appears to be waiting for human approval.",
        context=context,
    )
