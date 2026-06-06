#!/usr/bin/env python3
"""
pty_runner.py

PTY-based command runner for Durex v0.2.

Why this module exists
----------------------
The original queue runner uses subprocess.run(), which is excellent for simple
non-interactive commands but cannot respond to interactive terminal prompts in
real time.

Codex may ask for approval while it is running. A PTY runner lets Durex:

1. spawn Codex inside a pseudo-terminal;
2. read terminal output incrementally;
3. detect approval prompts;
4. classify the detected command with a policy;
5. ask Telegram when needed;
6. write the final decision back into Codex's terminal input.

This module connects the components created for v0.2:

- approval_detector.py
- approval_policy.py
- telegram_bridge.py

Important limitation
--------------------
PTY output is text, not a stable API. The runner is therefore pragmatic rather
than perfect. Future structured-event support should reuse the same normalized
request and decision concepts while avoiding terminal text parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import os
import pty
import select
import signal
import subprocess
import time
from typing import Optional, Sequence

from approval_detector import ApprovalRequest, detect_approval_request
from approval_policy import ApprovalPolicy, PolicyAction, PolicyDecision, default_policy
from telegram_bridge import (
    TelegramApprovalBridge,
    TelegramApprovalDecision,
    TelegramApprovalRequest,
    TelegramDecisionAction,
)


@dataclass(frozen=True)
class PtyRunnerConfig:
    """
    Runtime options for the PTY runner.

    read_timeout_seconds:
        Maximum time select() waits for new terminal output before checking
        whether the child process has exited.

    max_buffer_chars:
        Maximum number of terminal characters kept in the rolling buffer. The
        full output is stored separately; the rolling buffer is only used for
        prompt detection.

    echo_output:
        If true, chunks read from the PTY are also printed to the local stdout.
        This is useful while developing and while running Durex manually.
    """

    read_timeout_seconds: float = 0.5
    max_buffer_chars: int = 20000
    echo_output: bool = True


@dataclass(frozen=True)
class ApprovalAuditEvent:
    """
    One approval-related event produced by the PTY runner.

    These objects are returned in PtyRunResult. A future version can persist
    them in SQLite or write them to an audit log file.

    Attributes:
        request_id:
            Detector fingerprint for the prompt that was handled.
        command:
            Command associated with the prompt, when extraction succeeded.
        policy_action:
            Policy action selected before any Telegram interaction.
        final_action:
            Action actually applied to the terminal prompt.
        source:
            Origin of the final action, such as policy, telegram, timeout or
            system_no_telegram.
        reason:
            Human-facing policy reason.
        matched_rule:
            Policy rule pattern that matched, when any.
        created_at:
            Local Unix timestamp for the audit event.
    """

    request_id: str
    command: Optional[str]
    policy_action: str
    final_action: str
    source: str
    reason: str
    matched_rule: Optional[str]
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class PtyRunResult:
    """
    Normalized result returned by run_pty_command().

    returncode:
        Process exit code. Negative values can represent signal-based exits.

    output:
        Full terminal output captured from the PTY.

    approval_events:
        List of approval decisions taken during the command.
    """

    returncode: int
    output: str
    approval_events: list[ApprovalAuditEvent]


class PtyRunnerError(RuntimeError):
    """
    Raised when the PTY runner cannot start or manage the child process.
    """


def trim_buffer(buffer: str, max_chars: int) -> str:
    """
    Keep only the last max_chars characters of a rolling terminal buffer.

    Args:
        buffer:
            Current rolling terminal text.
        max_chars:
            Maximum number of characters kept for prompt detection.

    Returns:
        The original buffer when it is small enough, otherwise its tail.
    """

    if len(buffer) <= max_chars:
        return buffer
    return buffer[-max_chars:]


def write_to_pty(master_fd: int, text: str) -> None:
    """
    Write text into the PTY master file descriptor.

    The child process receives this as if the user typed it in the terminal.

    Args:
        master_fd:
            PTY master file descriptor owned by the parent process.
        text:
            Text to deliver to the child process through terminal input.

    Returns:
        None.
    """

    os.write(master_fd, text.encode("utf-8", errors="replace"))


def action_to_terminal_input(action: TelegramDecisionAction | PolicyAction) -> Optional[str]:
    """
    Convert a policy or Telegram decision into terminal input.

    Approval prompts usually accept a positive or negative confirmation. The PTY
    runner sends a newline after the character because that is what a user would
    normally type.

    Args:
        action:
            Final decision from either the local policy engine or Telegram.

    Returns:
        Terminal input to write into the PTY, or None for actions that do not
        map to direct prompt input.
    """

    if action in (TelegramDecisionAction.APPROVE, PolicyAction.AUTO_ALLOW):
        return "y\n"
    if action in (TelegramDecisionAction.DENY, PolicyAction.AUTO_DENY):
        return "n\n"
    return None


def build_telegram_request(
    task: Optional[dict],
    approval: ApprovalRequest,
    verbosity: str,
) -> TelegramApprovalRequest:
    """
    Convert detector ApprovalRequest into TelegramApprovalRequest.

    The PTY runner can be used both from codex_queue.py and standalone tests.
    For that reason task is optional and accessed as a plain dictionary.

    Args:
        task:
            Optional queue task metadata.
        approval:
            Detector-level approval request.
        verbosity:
            Telegram message verbosity requested by the caller.

    Returns:
        TelegramApprovalRequest with task metadata attached for user-facing
        approval messages.
    """

    task = task or {}
    return TelegramApprovalRequest(
        request_id=approval.request_id,
        task_id=task.get("id"),
        task_title=task.get("title", "Manual PTY command"),
        workdir=task.get("workdir", os.getcwd()),
        command=approval.command,
        reason=approval.reason,
        context=approval.context,
        verbosity=verbosity,
    )


def record_audit_event(
    events: list[ApprovalAuditEvent],
    approval: ApprovalRequest,
    policy_decision: PolicyDecision,
    final_action: str,
    source: str,
) -> None:
    """
    Append one normalized approval event to the in-memory audit list.

    Args:
        events:
            Mutable audit list owned by the current PTY run.
        approval:
            Detector request that triggered the decision.
        policy_decision:
            Local policy classification result.
        final_action:
            Action finally applied to the child terminal.
        source:
            Source of that final action.

    Returns:
        None. The function mutates ``events`` in place.
    """

    events.append(
        ApprovalAuditEvent(
            request_id=approval.request_id,
            command=approval.command,
            policy_action=policy_decision.action.value,
            final_action=final_action,
            source=source,
            reason=policy_decision.reason,
            matched_rule=policy_decision.matched_rule,
        )
    )


def terminate_process(process: subprocess.Popen, timeout_seconds: float = 5.0) -> int:
    """
    Try to stop the child process gracefully, then force-stop if needed.

    This is used when Telegram returns the STOP action.

    Args:
        process:
            Child process attached to the PTY.
        timeout_seconds:
            Grace period after terminate() before kill() is used.

    Returns:
        Final process return code.
    """

    if process.poll() is not None:
        return int(process.returncode or 0)

    process.terminate()
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        code = process.poll()
        if code is not None:
            return int(code)
        time.sleep(0.1)

    process.kill()
    return int(process.wait())


def spawn_pty_process(cmd: Sequence[str], cwd: Optional[str] = None) -> tuple[subprocess.Popen, int]:
    """
    Spawn a command connected to a new pseudo-terminal.

    Returns:
        (process, master_fd)

    master_fd is the file descriptor the parent uses to read terminal output and
    write terminal input.

    Args:
        cmd:
            Command argument vector to execute.
        cwd:
            Optional working directory for the child process.

    Returns:
        Tuple containing the subprocess object and the PTY master file
        descriptor.

    Raises:
        Exception:
            Propagates subprocess startup errors after closing both PTY file
            descriptors.
    """

    master_fd, slave_fd = pty.openpty()

    try:
        process = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            text=False,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise

    os.close(slave_fd)
    return process, master_fd


def handle_approval_request(
    master_fd: int,
    approval: ApprovalRequest,
    policy: ApprovalPolicy,
    telegram_bridge: Optional[TelegramApprovalBridge],
    task: Optional[dict],
    telegram_verbosity: str,
    audit_events: list[ApprovalAuditEvent],
) -> bool:
    """
    Apply policy and optionally ask Telegram for one approval request.

    Returns:
        True when the caller should continue reading the PTY.
        False when the caller should stop the task process.

    Args:
        master_fd:
            PTY master file descriptor used to answer prompts.
        approval:
            Detector request that needs a decision.
        policy:
            Local policy engine.
        telegram_bridge:
            Optional Telegram bridge for human-in-the-loop decisions.
        task:
            Optional queue metadata for Telegram message context.
        telegram_verbosity:
            Message detail level for Telegram approval requests.
        audit_events:
            Mutable audit list for the current PTY run.
    """

    policy_decision = policy.classify_command(approval.command)

    if policy_decision.action == PolicyAction.AUTO_ALLOW:
        write_to_pty(master_fd, "y\n")
        record_audit_event(
            audit_events,
            approval,
            policy_decision,
            final_action="approve",
            source="policy",
        )
        return True

    if policy_decision.action == PolicyAction.AUTO_DENY:
        write_to_pty(master_fd, "n\n")
        record_audit_event(
            audit_events,
            approval,
            policy_decision,
            final_action="deny",
            source="policy",
        )
        return True

    if telegram_bridge is None:
        # In PTY mode without Telegram, the conservative behavior is denial.
        write_to_pty(master_fd, "n\n")
        record_audit_event(
            audit_events,
            approval,
            policy_decision,
            final_action="deny",
            source="system_no_telegram",
        )
        return True

    telegram_request = build_telegram_request(task, approval, verbosity=telegram_verbosity)
    telegram_bridge.send_approval_request(telegram_request)
    telegram_decision = telegram_bridge.wait_for_decision(telegram_request)

    if telegram_decision.action == TelegramDecisionAction.STOP:
        record_audit_event(
            audit_events,
            approval,
            policy_decision,
            final_action="stop",
            source=telegram_decision.source,
        )
        return False

    terminal_input = action_to_terminal_input(telegram_decision.action)
    if terminal_input is None:
        # Unknown or non-final actions should not approve by accident.
        terminal_input = "n\n"
        final_action = "deny"
    else:
        final_action = "approve" if terminal_input.startswith("y") else "deny"

    write_to_pty(master_fd, terminal_input)
    record_audit_event(
        audit_events,
        approval,
        policy_decision,
        final_action=final_action,
        source=telegram_decision.source,
    )
    return True


def run_pty_command(
    cmd: Sequence[str],
    cwd: Optional[str] = None,
    task: Optional[dict] = None,
    policy: Optional[ApprovalPolicy] = None,
    telegram_bridge: Optional[TelegramApprovalBridge] = None,
    telegram_verbosity: str = "normal",
    config: Optional[PtyRunnerConfig] = None,
) -> PtyRunResult:
    """
    Run a command in a pseudo-terminal and handle approval prompts.

    Args:
        cmd:
            Command argument list, for example ["codex", "exec", prompt].

        cwd:
            Working directory for the child process.

        task:
            Optional task metadata from codex_queue.py. It is used only for
            Telegram messages and audit events.

        policy:
            Approval policy. If None, default_policy() is used.

        telegram_bridge:
            Optional TelegramApprovalBridge. If omitted and a prompt requires
            human approval, the runner denies conservatively.

        telegram_verbosity:
            compact, normal or verbose.

        config:
            PTY runner options.

    Returns:
        PtyRunResult with returncode, output and approval_events.
    """

    policy = policy or default_policy()
    config = config or PtyRunnerConfig()

    process, master_fd = spawn_pty_process(cmd, cwd=cwd)
    output_parts: list[str] = []
    rolling_buffer = ""
    seen_request_ids: set[str] = set()
    audit_events: list[ApprovalAuditEvent] = []

    try:
        while True:
            readable, _, _ = select.select([master_fd], [], [], config.read_timeout_seconds)

            if readable:
                try:
                    raw = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise

                if not raw:
                    break

                chunk = raw.decode("utf-8", errors="replace")
                output_parts.append(chunk)
                rolling_buffer = trim_buffer(rolling_buffer + chunk, config.max_buffer_chars)

                if config.echo_output:
                    print(chunk, end="", flush=True)

                approval = detect_approval_request(rolling_buffer)
                if approval is not None and approval.request_id not in seen_request_ids:
                    seen_request_ids.add(approval.request_id)
                    should_continue = handle_approval_request(
                        master_fd=master_fd,
                        approval=approval,
                        policy=policy,
                        telegram_bridge=telegram_bridge,
                        task=task,
                        telegram_verbosity=telegram_verbosity,
                        audit_events=audit_events,
                    )
                    if not should_continue:
                        returncode = terminate_process(process)
                        return PtyRunResult(
                            returncode=returncode,
                            output="".join(output_parts),
                            approval_events=audit_events,
                        )
                    rolling_buffer = ""

            if process.poll() is not None:
                break

        returncode = int(process.wait())
        return PtyRunResult(
            returncode=returncode,
            output="".join(output_parts),
            approval_events=audit_events,
        )

    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass


def _demo() -> None:
    """
    Manual local demo.

    This demo runs a tiny Python command that asks for confirmation. It does not
    require Codex. It is useful to test the PTY loop, detector and policy path.

    Run:
        python3 pty_runner.py
    """

    cmd = [
        "python3",
        "-c",
        (
            "print('Command: pytest -q'); "
            "answer=input('Approve this command? [y/N] '); "
            "print('answer=' + answer)"
        ),
    ]

    result = run_pty_command(
        cmd=cmd,
        cwd=os.getcwd(),
        task={"id": 0, "title": "PTY runner demo", "workdir": os.getcwd()},
        policy=default_policy(),
        telegram_bridge=None,
        config=PtyRunnerConfig(echo_output=True),
    )

    print("\n--- PTY result ---")
    print(f"returncode: {result.returncode}")
    print(f"approval_events: {result.approval_events}")


if __name__ == "__main__":
    _demo()
