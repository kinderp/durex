#!/usr/bin/env python3
"""
approval_detector.py

Text-based approval detector used by the PTY runner.

Why this module exists
----------------------
A PTY bridge does not receive structured events. It receives terminal text.
That terminal text is the same kind of output a human sees on screen: ANSI
colors, carriage returns, progress spinners, partially redrawn lines and
multiline prompts.

This module isolates all text-detection heuristics so the PTY runner can stay
focused on process management:

- spawn Codex in a pseudo-terminal;
- read terminal output chunks;
- pass the rolling buffer to this detector;
- receive a normalized ApprovalRequest when user confirmation is needed.

Design principle
----------------
The detector should be conservative. False positives are annoying because they
send unnecessary Telegram messages. False negatives are also possible because
terminal text is not a stable API. The long-term roadmap therefore keeps a
structured event runner as a future improvement.

The detector does not decide whether to approve or deny anything. It only
recognizes that a human approval may be needed. The decision belongs to the
approval policy and, when needed, to the Telegram approval bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import time
from typing import Optional


# ANSI escape sequences are produced by terminal programs to color text,
# move the cursor, clear lines, show spinners, etc. They are useful on screen
# but make text matching unreliable, so we strip them before detection.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Carriage-control characters are frequently used by progress bars and
# spinners. Replacing them with newlines is simple and keeps the visible text.
CONTROL_RE = re.compile(r"[\r\f\v]")

# Very small whitespace normalizer used when generating fingerprints. It keeps
# request deduplication stable even if Codex redraws the same prompt with
# slightly different spacing.
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ApprovalRequest:
    """
    Normalized representation of a terminal approval request.

    The PTY runner should pass this object to the approval policy. If the policy
    returns ASK_TELEGRAM, the same object can be forwarded to the Telegram
    bridge.

    Attributes:
        request_id:
            Stable fingerprint derived from command and context. The PTY runner
            can use it to avoid sending the same approval request repeatedly.

        command:
            The command we think Codex wants to run. It can be None when the
            detector sees a generic prompt but cannot confidently extract the
            command.

        reason:
            Human-readable explanation of why the detector produced a request.

        context:
            Recent terminal text around the approval prompt. This is useful for
            normal and verbose Telegram messages.

        created_at:
            Unix timestamp created locally by the detector.
    """

    request_id: str
    command: Optional[str]
    reason: str
    context: str
    created_at: float = field(default_factory=time.time)


def strip_ansi(text: str) -> str:
    """
    Remove terminal-specific ANSI and control characters.

    Args:
        text:
            Raw terminal output captured from the PTY.

    Returns:
        A cleaner string that is easier to scan with regular expressions.
    """

    text = ANSI_RE.sub("", text)
    text = CONTROL_RE.sub("\n", text)
    return text


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison and fingerprinting.

    This function is intentionally not used for display because it removes
    line structure. It is only used where stable matching matters.
    """

    return WHITESPACE_RE.sub(" ", strip_ansi(text)).strip()


def tail_lines(text: str, max_lines: int = 40) -> str:
    """
    Return only the last max_lines lines of terminal output.

    Approval prompts are normally near the end of the buffer. Looking only at
    the tail reduces noise and keeps Telegram messages compact.
    """

    lines = strip_ansi(text).splitlines()
    return "\n".join(lines[-max_lines:])


def redact_for_display(text: Optional[str]) -> Optional[str]:
    """
    Redact obvious secret-looking fragments before sending text to Telegram.

    This is not a full data-loss-prevention system. It only handles common
    patterns such as token=..., password=..., api_key=... and bearer headers.
    The goal is to avoid accidentally forwarding obvious secrets to a chat.
    """

    if text is None:
        return None

    redacted = re.sub(
        r"(?i)(token|password|passwd|secret|api[_-]?key)\s*=\s*([^\s]+)",
        r"\1=<redacted>",
        text,
    )
    redacted = re.sub(
        r"(?i)(bearer)\s+([A-Za-z0-9._\-]+)",
        r"\1 <redacted>",
        redacted,
    )
    return redacted


def looks_like_approval_prompt(text: str) -> bool:
    """
    Detect whether recent terminal output looks like an approval prompt.

    The detector searches only the tail of the terminal buffer and looks for
    common confirmation wording. The list is intentionally broad enough to
    catch typical CLI prompts but not so broad that every question becomes an
    approval request.
    """

    recent = tail_lines(text, 50).lower()

    patterns = [
        r"\[[yn]/[yn]\]",
        r"\(y/n\)",
        r"\(yes/no\)",
        r"\byes/no\b",
        r"\bapprove\b.*\?",
        r"\bapproval\b.*\?",
        r"\bproceed\b.*\?",
        r"\bcontinue\b.*\?",
        r"\brun\b.*\bcommand\b.*\?",
        r"\bexecute\b.*\bcommand\b.*\?",
        r"\ballow\b.*\?",
        r"\bconfirm\b.*\?",
    ]

    return any(re.search(pattern, recent, re.IGNORECASE) for pattern in patterns)


def prompt_signature(command: Optional[str], context: str) -> str:
    """
    Return the stable part of an approval prompt for fingerprinting.

    The displayed context grows while a PTY process is running, so hashing the
    whole tail makes the same prompt look like different requests. We instead
    use the extracted command plus the last line that looks like the actual
    confirmation prompt.
    """

    prompt_patterns = [
        r"\[[yn]/[yn]\]",
        r"\(y/n\)",
        r"\(yes/no\)",
        r"\byes/no\b",
        r"\bapprove\b.*\?",
        r"\bapproval\b.*\?",
        r"\bproceed\b.*\?",
        r"\bcontinue\b.*\?",
        r"\brun\b.*\bcommand\b.*\?",
        r"\bexecute\b.*\bcommand\b.*\?",
        r"\ballow\b.*\?",
        r"\bconfirm\b.*\?",
    ]
    lines = [line.strip() for line in strip_ansi(context).splitlines() if line.strip()]
    prompt_line = ""

    for line in reversed(lines):
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in prompt_patterns):
            prompt_line = line
            break

    if not prompt_line:
        prompt_line = "\n".join(lines[-3:])

    if not command:
        stable_context = "\n".join(lines[-5:])
        return f"{normalize_text(stable_context)}\n{normalize_text(prompt_line)}"

    return f"{normalize_text(command or '')}\n{normalize_text(prompt_line)}"


def extract_command(text: str) -> Optional[str]:
    """
    Try to extract the command being requested from terminal output.

    Because a PTY bridge only sees terminal text, there is no guaranteed schema.
    The function uses several heuristics, from strongest to weakest:

    1. fenced shell blocks;
    2. explicit labels such as "Command:" or "Run:";
    3. shell prompt lines beginning with "$ ";
    4. lines beginning with common developer-tool commands.

    Returns None when no command can be extracted confidently.
    """

    recent = tail_lines(text, 60)
    lines = [line.strip() for line in recent.splitlines() if line.strip()]
    joined = "\n".join(lines)

    fenced = re.search(r"```(?:bash|sh|shell)?\s*(.*?)```", joined, re.DOTALL | re.IGNORECASE)
    if fenced:
        command = fenced.group(1).strip()
        if command:
            return redact_for_display(command)

    label_patterns = [
        r"^(?:command|run|execute)\s*:\s*(.+)$",
        r"^(?:codex wants to run|codex would like to run)\s*:?\s*(.+)$",
        r"^(?:proposed command|requested command)\s*:\s*(.+)$",
    ]

    for line in reversed(lines):
        for pattern in label_patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                command = match.group(1).strip()
                if command and command not in {":", "-"}:
                    return redact_for_display(command)

    for line in reversed(lines):
        if line.startswith("$ "):
            return redact_for_display(line[2:].strip())

    shellish_prefixes = (
        "pytest", "python ", "python3 ", "npm ", "pnpm ", "yarn ", "git ",
        "ruff ", "mypy ", "docker ", "curl ", "wget ", "pip ", "pip3 ",
        "bash ", "sh ", "mv ", "cp ", "chmod ", "make ", "go ", "cargo ",
    )

    for line in reversed(lines):
        if line.startswith(shellish_prefixes):
            return redact_for_display(line)

    return None


def make_request_id(command: Optional[str], context: str) -> str:
    """
    Build a stable short fingerprint for an approval request.

    Terminal prompts may be redrawn multiple times. The PTY runner can store the
    last request_id values it sent and avoid duplicate Telegram notifications.
    """

    base = prompt_signature(command, context)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def detect_approval_request(text: str) -> Optional[ApprovalRequest]:
    """
    Return an ApprovalRequest if recent output looks like an approval prompt.

    Args:
        text:
            Rolling terminal buffer collected by the PTY runner.

    Returns:
        ApprovalRequest when a prompt is detected, otherwise None.
    """

    if not looks_like_approval_prompt(text):
        return None

    context = redact_for_display(tail_lines(text, 30)) or ""
    command = extract_command(context)
    request_id = make_request_id(command, context)

    return ApprovalRequest(
        request_id=request_id,
        command=command,
        reason="The terminal output appears to be waiting for human approval.",
        context=context,
    )


def _demo() -> None:
    """
    Small manual demonstration.

    This is not a replacement for proper tests. It is useful when developing the
    detector locally:

        python3 approval_detector.py
    """

    sample = """
Codex wants to run:
$ pytest -q

Approve this command? [y/N]
"""

    request = detect_approval_request(sample)
    if request is None:
        print("No approval request detected.")
        return

    print("Approval request detected:")
    print(f"request_id: {request.request_id}")
    print(f"command: {request.command}")
    print(f"reason: {request.reason}")
    print("context:")
    print(request.context)


if __name__ == "__main__":
    _demo()
