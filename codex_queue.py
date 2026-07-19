#!/usr/bin/env python3
"""
codex_queue.py

Persistent task queue and runner for Codex CLI.

This file is the main command-line entry point for Durex.

Version focus
-------------
The original version executed Codex with subprocess.run(). That mode is still
supported because it is simple and reliable for non-interactive jobs.

The v0.2 direction adds a PTY runner. PTY mode runs Codex inside a
pseudo-terminal, detects approval prompts, applies an approval policy and can
optionally ask the user through Telegram.

Supported runner modes:

- subprocess: classic non-interactive execution;
- pty: interactive terminal bridge with approval handling.

The queue itself remains intentionally simple: SQLite stores tasks, attempts,
outputs and usage-limit reset timestamps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import time
from typing import Optional
import uuid

from approval_policy import default_policy
from pty_runner import PtyRunnerConfig, run_pty_command
from runner_events import PersistentRunnerEventSink
from subprocess_runner import run_subprocess_command
from task_services import (
    TASK_STATUSES,
    SQLiteTaskRepository,
    TaskApplicationService,
    TaskRecord,
)
from telegram_bridge import TelegramApprovalBridge, TelegramBridgeConfig, TelegramBridgeError
from telegram_dispatcher import ApprovalDecisionProvider, StandaloneTelegramApprovalRuntime


DB_PATH = "codex_tasks.db"
CODEX_BIN = "codex"
DEFAULT_CHECK_INTERVAL = 60
DEFAULT_RETRY_HOURS = 5


STATUSES = set(TASK_STATUSES)


def utc_now() -> dt.datetime:
    """
    Return the current UTC datetime.

    Returns:
        Timezone-aware datetime in UTC. Queue timestamps are stored in UTC so
        retry checks do not depend on the operator's local timezone.
    """

    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    """
    Return the current UTC time as an ISO-8601 string.

    Returns:
        ISO-8601 timestamp used by SQLite task rows.
    """

    return utc_now().isoformat()


def parse_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    """
    Parse an ISO datetime string, accepting the common trailing Z syntax.

    Args:
        value:
            Optional timestamp found in Codex output or database state.

    Returns:
        Parsed datetime when the value is valid, otherwise None. Invalid reset
        values are ignored so the queue can fall back to a conservative retry
        delay.
    """

    if not value:
        return None

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def connect() -> sqlite3.Connection:
    """
    Open the local SQLite database.

    Returns:
        sqlite3 connection to the configured DB_PATH. Callers own transaction
        scope through context managers.
    """

    return sqlite3.connect(DB_PATH)


def get_task_service() -> TaskApplicationService:
    """Build the task service for the currently configured database path."""

    repository = SQLiteTaskRepository(connect=connect, now=iso_now)
    return TaskApplicationService(repository=repository, now=iso_now)


def init_db() -> None:
    """
    Create the task table if needed.

    The schema is intentionally compact. Future versions can add an approval
    audit table without changing the basic queue behavior.
    """

    get_task_service().initialize()


def add_task(title: str, prompt: str, workdir: str = ".", priority: int = 100, max_attempts: int = 3) -> None:
    """
    Add one task to the persistent queue.

    Args:
        title:
            Human-readable task title.
        prompt:
            Prompt passed to ``codex exec`` when the task starts.
        workdir:
            Working directory used by the Codex process.
        priority:
            Lower values run earlier.
        max_attempts:
            Maximum non-limit failures before the task becomes FAILED.

    Returns:
        None. The function persists the task in SQLite.
    """

    get_task_service().add_task(
        title=title,
        prompt=prompt,
        workdir=workdir,
        priority=priority,
        max_attempts=max_attempts,
    )


def list_tasks() -> None:
    """
    Print a human-readable task list.

    Returns:
        None. This is a CLI presentation helper and writes to stdout.
    """

    rows = get_task_service().list_tasks()

    if not rows:
        print("No tasks found.")
        return

    for row in rows:
        print(
            f"[{row.id}] {row.title} | status={row.status} | priority={row.priority} "
            f"| attempts={row.attempts} | reset_at={row.reset_at} | workdir={row.workdir}"
        )


def get_next_task() -> Optional[TaskRecord]:
    """
    Return the next runnable task.

    Runnable means:
    - PENDING, or
    - WAITING_LIMIT with reset_at already passed.

    Returns:
        TaskRecord for the next runnable task, or None when the worker should
        sleep or exit.
    """

    return get_task_service().next_runnable_task()


def update_task(task_id: int, **fields: object) -> None:
    """
    Update one or more columns for a task.

    Args:
        task_id:
            Primary key of the task to update.
        **fields:
            Column names and replacement values. ``updated_at`` is always
            refreshed by this function.

    Returns:
        None. Empty updates are ignored.
    """

    if fields:
        get_task_service().update_task(task_id, **fields)


def extract_session_id(text: str) -> Optional[str]:
    """
    Try to extract a Codex session id from command output.

    This remains heuristic because CLI output can change over time.

    Args:
        text:
            Captured Codex stdout/stderr or PTY output.

    Returns:
        Latest session id candidate, or None when no known pattern matches.
    """

    patterns = [
        r"session[_ -]?id[:=]\s*([0-9a-fA-F-]{20,})",
        r"Session[: ]+([0-9a-fA-F-]{20,})",
        r"resum(?:e|ing).*?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    ]

    matches: list[tuple[int, str]] = []

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            matches.append((match.start(1), match.group(1)))

    if matches:
        return max(matches, key=lambda item: item[0])[1]

    return None


def extract_reset_at(text: str) -> Optional[str]:
    """
    Try to extract usage-limit reset time from Codex output.

    Args:
        text:
            Captured Codex output that may contain quota reset hints.

    Returns:
        ISO timestamp when a valid reset marker is found, otherwise None.
    """

    patterns = [
        r"resets_at[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
        r"reset_at[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
        r"try again after\s+([0-9T:\-+.Z]+)",
        r"reset[s]?\s+(?:at|on)\s+([0-9T:\-+.Z]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = parse_datetime(match.group(1))
            if parsed:
                return parsed.isoformat()

    return None


def looks_like_usage_limit(text: str) -> bool:
    """
    Detect common usage-limit or quota errors in output text.

    Args:
        text:
            Captured command output.

    Returns:
        True when common quota/rate-limit markers are present.
    """

    markers = [
        "usage limit",
        "rate limit",
        "quota",
        "too many requests",
        "429",
        "limit reached",
    ]
    lower = text.lower()
    return any(marker in lower for marker in markers)


def build_codex_command(task: TaskRecord) -> list[str]:
    """
    Build the Codex CLI command for a task.

    If a session_id exists, use Codex resume. Otherwise start from the original
    prompt.

    Args:
        task:
            SQLite task row.

    Returns:
        Command argument vector for subprocess or PTY execution.
    """

    if task["session_id"]:
        followup = task["next_step"] or "Continue from where you stopped. Keep the plan and complete the task."
        return [CODEX_BIN, "exec", "resume", task["session_id"], followup]

    return [CODEX_BIN, "exec", task["prompt"]]


def task_to_dict(task: TaskRecord) -> dict:
    """
    Convert a task record into a plain dictionary for legacy runner adapters.

    Args:
        task:
            Transport-neutral task returned by the queue layer.

    Returns:
        Plain dictionary preserving all row keys.
    """

    return {key: task[key] for key in task.keys()}


def finish_task_from_output(
    task: TaskRecord,
    output: str,
    returncode: int,
    task_service: Optional[TaskApplicationService] = None,
) -> None:
    """
    Convert runner output into final queue state.

    This function is shared by subprocess mode and PTY mode.

    Args:
        task:
            Task row captured before the run started.
        output:
            Complete command output captured by the selected runner.
        returncode:
            Process exit status from subprocess or PTY mode.
        task_service:
            Persistence boundary that owns this task lifecycle.

    Returns:
        None. The function updates SQLite and prints the state transition.
    """

    task_id = int(task["id"])
    tasks = task_service or get_task_service()
    found_session_id = extract_session_id(output) or task["session_id"]
    reset_at = extract_reset_at(output)

    if returncode == 0:
        tasks.update_task(
            task_id,
            status="COMPLETED",
            output=output,
            session_id=found_session_id,
            reset_at=None,
            last_error=None,
        )
        print(f"Task #{task_id} completed.")
        return

    if looks_like_usage_limit(output):
        if not reset_at:
            reset_at = (utc_now() + dt.timedelta(hours=DEFAULT_RETRY_HOURS)).isoformat()

        tasks.update_task(
            task_id,
            status="WAITING_LIMIT",
            output=output,
            session_id=found_session_id,
            reset_at=reset_at,
            next_step="Resume the work from the exact point where you stopped and complete the task.",
            last_error="Usage limit reached",
        )
        print(f"Task #{task_id} suspended because a usage limit was reached.")
        print(f"It will resume after: {reset_at}")
        return

    attempts_after_run = int(task["attempts"]) + 1
    if attempts_after_run < int(task["max_attempts"]):
        tasks.update_task(
            task_id,
            status="PENDING",
            output=output,
            session_id=found_session_id,
            last_error=output[-3000:],
        )
        print(f"Task #{task_id} failed, but it will be retried.")
        return

    tasks.update_task(
        task_id,
        status="FAILED",
        output=output,
        session_id=found_session_id,
        last_error=output[-3000:],
    )
    print(f"Task #{task_id} failed permanently.")


def run_codex_subprocess(
    task: TaskRecord,
    task_service: Optional[TaskApplicationService] = None,
) -> None:
    """
    Run one task using incremental subprocess output capture.

    Args:
        task:
            Runnable queue task.
        task_service:
            Persistence boundary that owns this task lifecycle.

    Returns:
        None. The task row is updated based on process output.
    """

    task_id = int(task["id"])
    tasks = task_service or get_task_service()
    cmd = build_codex_command(task)
    attempt = int(task["attempts"]) + 1
    run_id = uuid.uuid4().hex
    event_sink = PersistentRunnerEventSink(tasks, task_id, run_id, attempt)

    tasks.update_task(
        task_id,
        status="RUNNING",
        attempts=attempt,
        last_error=None,
    )

    print(f"\nStarting task #{task_id}: {task['title']}")
    print("Runner mode: subprocess")
    print("Working directory:", task["workdir"])
    print("Command:", " ".join(cmd))

    try:
        result = run_subprocess_command(
            cmd,
            task_id=task_id,
            cwd=task["workdir"],
            event_sink=event_sink,
            run_id=run_id,
        )
        finish_task_from_output(
            task,
            output=result.output,
            returncode=int(result.returncode),
            task_service=tasks,
        )
    except Exception as exc:
        error = finalize_failed_live_run(event_sink, exc)
        tasks.update_task(task_id, status="FAILED", last_error=error)
        print(f"Task #{task_id} error: {error}")


def finalize_failed_live_run(
    event_sink: PersistentRunnerEventSink,
    original_error: BaseException,
) -> str:
    """Close an open live run without hiding the runner's original error."""

    error = str(original_error)
    try:
        event_sink.fail_open_run()
    except Exception as finalization_error:
        error = f"{error}; live-output finalization failed: {finalization_error}"
    return error


def build_telegram_bridge(enabled: bool, verbosity: str) -> Optional[TelegramApprovalBridge]:
    """
    Create the Telegram bridge when requested.

    If Telegram is enabled but environment variables are missing, the runner
    raises a clear error instead of silently running without approvals.

    Args:
        enabled:
            Whether PTY approvals should use Telegram.
        verbosity:
            Telegram approval message verbosity.

    Returns:
        TelegramApprovalBridge when enabled, otherwise None.

    Raises:
        TelegramBridgeError:
            Raised by ``from_env`` when required environment variables are
            missing or invalid.
    """

    if not enabled:
        return None

    return TelegramApprovalBridge.from_env(verbosity=verbosity)


def telegram_check(discover_chat_id: bool, send_test: bool, message: str, poll_timeout: int) -> None:
    """
    Validate Telegram Bot API connectivity and optional chat routing.

    Args:
        discover_chat_id:
            Whether to poll recent updates and print discovered chat ids.
        send_test:
            Whether to send a configured test message.
        message:
            Message body for the test send.
        poll_timeout:
            Long-poll timeout used while discovering chat ids.

    Returns:
        None. Results are printed to stdout.

    Raises:
        TelegramBridgeError:
            Raised when environment variables are missing or Telegram rejects
            the request.
    """

    token = os.environ.get("DUREX_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("DUREX_TELEGRAM_CHAT_ID")
    if not token:
        raise TelegramBridgeError("Missing DUREX_TELEGRAM_BOT_TOKEN.")

    allowed_chat_id = 0
    if chat_id:
        try:
            allowed_chat_id = int(chat_id)
        except ValueError as exc:
            raise TelegramBridgeError("DUREX_TELEGRAM_CHAT_ID must be an integer.") from exc

    bridge = TelegramApprovalBridge(
        TelegramBridgeConfig(bot_token=token, allowed_chat_id=allowed_chat_id)
    )

    bot = bridge.get_me()
    username = bot.get("username", "<unknown>")
    bot_id = bot.get("id", "<unknown>")
    print(f"Telegram token OK: @{username} ({bot_id})")

    if discover_chat_id:
        print("Polling Telegram updates for chat ids...")
        chat_ids = bridge.discover_chat_ids(timeout=poll_timeout)
        if chat_ids:
            for discovered in chat_ids:
                print(f"Found chat id: {discovered}")
            if not chat_id:
                print(f"Set DUREX_TELEGRAM_CHAT_ID={chat_ids[-1]}")
        else:
            print("No chat ids found. Send any message to the bot, then run this command again.")

    if send_test:
        if not chat_id:
            raise TelegramBridgeError("Cannot send test message without DUREX_TELEGRAM_CHAT_ID.")
        message_id = bridge.send_message(message)
        print(f"Telegram test message sent: message_id={message_id}")


def run_codex_pty(
    task: TaskRecord,
    telegram_enabled: bool,
    telegram_verbosity: str,
    echo_output: bool,
    task_service: Optional[TaskApplicationService] = None,
    approval_provider: Optional[ApprovalDecisionProvider] = None,
) -> None:
    """
    Run one task using the PTY approval bridge.

    Args:
        task:
            Runnable queue task.
        telegram_enabled:
            Whether policy escalation may ask Telegram.
        telegram_verbosity:
            Message detail level for Telegram approvals.
        echo_output:
            Whether PTY output should be mirrored locally.
        task_service:
            Persistence boundary that owns this task lifecycle.
        approval_provider:
            Shared broker-backed approval provider. Control mode injects this
            so the runner does not create a second Telegram poller.

    Returns:
        None. The task row is updated based on PTY result.
    """

    task_id = int(task["id"])
    tasks = task_service or get_task_service()
    cmd = build_codex_command(task)
    attempt = int(task["attempts"]) + 1
    run_id = uuid.uuid4().hex
    event_sink = PersistentRunnerEventSink(tasks, task_id, run_id, attempt)

    tasks.update_task(
        task_id,
        status="RUNNING",
        attempts=attempt,
        last_error=None,
    )

    print(f"\nStarting task #{task_id}: {task['title']}")
    print("Runner mode: pty")
    print("Working directory:", task["workdir"])
    print("Command:", " ".join(cmd))

    try:
        runtime: Optional[StandaloneTelegramApprovalRuntime] = None
        active_provider = approval_provider if telegram_enabled else None
        if telegram_enabled and active_provider is None:
            telegram_transport = build_telegram_bridge(enabled=True, verbosity=telegram_verbosity)
            if telegram_transport is None:
                raise TelegramBridgeError("Telegram approval transport was not created.")
            runtime = StandaloneTelegramApprovalRuntime(telegram_transport)
            active_provider = runtime.start()

        try:
            result = run_pty_command(
                cmd=cmd,
                cwd=task["workdir"],
                task=task_to_dict(task),
                policy=default_policy(),
                approval_provider=active_provider,
                telegram_verbosity=telegram_verbosity,
                config=PtyRunnerConfig(echo_output=echo_output),
                event_sink=event_sink,
                run_id=run_id,
            )
        finally:
            if runtime is not None:
                runtime.close()
        finish_task_from_output(
            task,
            output=result.output,
            returncode=result.returncode,
            task_service=tasks,
        )
    except TelegramBridgeError as exc:
        error = finalize_failed_live_run(event_sink, exc)
        tasks.update_task(task_id, status="FAILED", last_error=error)
        print(f"Telegram configuration error for task #{task_id}: {error}")
    except Exception as exc:
        error = finalize_failed_live_run(event_sink, exc)
        tasks.update_task(task_id, status="FAILED", last_error=error)
        print(f"Task #{task_id} PTY error: {error}")


def run_task(
    task: TaskRecord,
    runner_mode: str,
    telegram_enabled: bool,
    telegram_verbosity: str,
    echo_output: bool,
    task_service: Optional[TaskApplicationService] = None,
    approval_provider: Optional[ApprovalDecisionProvider] = None,
) -> None:
    """
    Dispatch a task to the selected runner implementation.

    Args:
        task:
            Runnable queue task.
        runner_mode:
            ``subprocess`` for non-interactive execution or ``pty`` for
            interactive approval handling.
        telegram_enabled:
            Whether PTY escalation may use Telegram.
        telegram_verbosity:
            Telegram approval verbosity.
        echo_output:
            Whether PTY output is mirrored to stdout.
        task_service:
            Persistence boundary that owns this task lifecycle.
        approval_provider:
            Shared approval provider injected by a process-level dispatcher.

    Returns:
        None.
    """

    tasks = task_service or get_task_service()
    if runner_mode == "pty":
        run_codex_pty(
            task,
            telegram_enabled=telegram_enabled,
            telegram_verbosity=telegram_verbosity,
            echo_output=echo_output,
            task_service=tasks,
            approval_provider=approval_provider,
        )
        return

    run_codex_subprocess(task, task_service=tasks)


def worker_loop(
    check_interval: int = DEFAULT_CHECK_INTERVAL,
    stop_when_empty: bool = False,
    runner_mode: str = "subprocess",
    telegram_enabled: bool = False,
    telegram_verbosity: str = "normal",
    echo_output: bool = True,
) -> None:
    """
    Main worker loop.

    Args:
        check_interval:
            Sleep interval when no runnable task exists.
        stop_when_empty:
            Exit instead of sleeping when the queue is empty.
        runner_mode:
            Selected execution backend.
        telegram_enabled:
            Whether PTY approvals may call Telegram.
        telegram_verbosity:
            Telegram approval message verbosity.
        echo_output:
            Whether PTY output is printed locally.

    Returns:
        None. The loop returns only when ``stop_when_empty`` is true and no task
        is runnable.
    """

    tasks = get_task_service()
    tasks.initialize()

    while True:
        task = tasks.next_runnable_task()

        if not task:
            if stop_when_empty:
                print("No executable tasks found. Exiting.")
                return

            print(f"No task ready. Checking again in {check_interval} seconds.")
            time.sleep(check_interval)
            continue

        run_task(
            task,
            runner_mode=runner_mode,
            telegram_enabled=telegram_enabled,
            telegram_verbosity=telegram_verbosity,
            echo_output=echo_output,
            task_service=tasks,
        )


def seed_example_tasks(workdir: str = ".") -> None:
    """
    Insert example tasks useful for quick testing.

    Args:
        workdir:
            Working directory assigned to every seeded task.

    Returns:
        None. The function persists example queue rows.
    """

    add_task(
        title="Grade student A assignment",
        prompt="""
You are an impartial grader.

Read the project in the current directory.
Run the available tests.
Evaluate the assignment using this rubric:

- functional correctness: 0-4
- code quality: 0-2
- error handling: 0-2
- clarity and style: 0-2

Produce:
1. grade out of 10;
2. reasoning for each criterion;
3. short final evaluation;
4. possible improvement suggestions.

Save the result in report_student_A.md.
""",
        workdir=workdir,
        priority=1,
    )

    add_task(
        title="Generate missing automated tests",
        prompt="""
Analyze the project in the current directory.
Identify cases not covered by tests.
Add clear and repeatable automated tests.
Do not change the application logic.
At the end, run the tests and write a summary in test_report.md.
""",
        workdir=workdir,
        priority=2,
    )

    add_task(
        title="Create final report",
        prompt="""
Read all markdown reports in the current directory.
Create a report_final.md file with:
- student list;
- grade;
- strengths;
- issues;
- teaching suggestions.
""",
        workdir=workdir,
        priority=3,
    )


def main() -> None:
    """
    Command-line interface.

    Returns:
        None. Argparse dispatches subcommands and this function writes
        user-facing results to stdout.
    """

    parser = argparse.ArgumentParser(
        description="Durex: persistent Codex CLI task queue with optional PTY/Telegram approvals."
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize the SQLite database.")

    add = sub.add_parser("add", help="Add a task to the queue.")
    add.add_argument("--title", required=True, help="Task title.")
    add.add_argument("--prompt", required=True, help="Prompt to pass to Codex.")
    add.add_argument("--workdir", default=".", help="Working directory.")
    add.add_argument("--priority", type=int, default=100, help="Priority: lower number = earlier.")
    add.add_argument("--max-attempts", type=int, default=3, help="Maximum attempts.")

    seed = sub.add_parser("seed", help="Add example tasks.")
    seed.add_argument("--workdir", default=".", help="Working directory for example tasks.")

    sub.add_parser("list", help="Show tasks stored in the database.")

    run = sub.add_parser("run", help="Start the worker.")
    run.add_argument("--interval", type=int, default=DEFAULT_CHECK_INTERVAL, help="Seconds between checks.")
    run.add_argument("--stop-when-empty", action="store_true", help="Stop the worker if no task is ready.")
    run.add_argument(
        "--runner-mode",
        choices=["subprocess", "pty"],
        default="subprocess",
        help="Execution backend. Use pty for interactive approval handling.",
    )
    run.add_argument("--telegram", action="store_true", help="Enable Telegram approvals in PTY mode.")
    run.add_argument(
        "--telegram-verbosity",
        choices=["compact", "normal", "verbose"],
        default="normal",
        help="Amount of information sent in Telegram approval messages.",
    )
    run.add_argument("--no-echo", action="store_true", help="Do not mirror PTY output to local stdout.")

    telegram_check_parser = sub.add_parser(
        "telegram-check",
        help="Validate Telegram Bot API credentials and optional chat routing.",
    )
    telegram_check_parser.add_argument(
        "--discover-chat-id",
        action="store_true",
        help="Poll recent updates and print chat ids that messaged the bot.",
    )
    telegram_check_parser.add_argument(
        "--send-test",
        action="store_true",
        help="Send a test message to DUREX_TELEGRAM_CHAT_ID.",
    )
    telegram_check_parser.add_argument(
        "--message",
        default="Durex Telegram check OK.",
        help="Message used with --send-test.",
    )
    telegram_check_parser.add_argument(
        "--poll-timeout",
        type=int,
        default=0,
        help="Seconds to wait for updates when --discover-chat-id is used.",
    )

    control = sub.add_parser("telegram-control", help="Start Telegram remote control for the Durex queue.")
    control.add_argument(
        "--allowed-workdir",
        action="append",
        help=(
            "Allowed root for remote /add workdirs. Can be repeated. "
            "Defaults to DUREX_TELEGRAM_ALLOWED_WORKDIRS or the current directory."
        ),
    )
    control.add_argument(
        "--runner-mode",
        choices=["subprocess", "pty"],
        default="pty",
        help="Runner used when /run starts the worker.",
    )
    control.add_argument(
        "--worker-telegram-approvals",
        action="store_true",
        help="Route PTY worker approval buttons through the control dispatcher.",
    )
    control.add_argument(
        "--telegram-verbosity",
        choices=["compact", "normal", "verbose"],
        default="normal",
        help="Approval verbosity used when --worker-telegram-approvals is enabled.",
    )
    control.add_argument("--echo-output", action="store_true", help="Mirror worker PTY output locally.")

    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print("Database initialized.")
        return

    if args.command == "add":
        init_db()
        add_task(
            title=args.title,
            prompt=args.prompt,
            workdir=args.workdir,
            priority=args.priority,
            max_attempts=args.max_attempts,
        )
        print("Task added.")
        return

    if args.command == "seed":
        init_db()
        seed_example_tasks(args.workdir)
        print("Example tasks added.")
        return

    if args.command == "list":
        init_db()
        list_tasks()
        return

    if args.command == "run":
        worker_loop(
            check_interval=args.interval,
            stop_when_empty=args.stop_when_empty,
            runner_mode=args.runner_mode,
            telegram_enabled=args.telegram,
            telegram_verbosity=args.telegram_verbosity,
            echo_output=not args.no_echo,
        )
        return

    if args.command == "telegram-check":
        telegram_check(
            discover_chat_id=args.discover_chat_id,
            send_test=args.send_test,
            message=args.message,
            poll_timeout=args.poll_timeout,
        )
        return

    if args.command == "telegram-control":
        sys.modules.setdefault("codex_queue", sys.modules[__name__])
        from telegram_control import TelegramControlBot

        init_db()
        bot = TelegramControlBot.from_env(
            allowed_workdirs=args.allowed_workdir,
            runner_mode=args.runner_mode,
            worker_telegram_approvals=args.worker_telegram_approvals,
            telegram_verbosity=args.telegram_verbosity,
            echo_output=args.echo_output,
        )
        bot.run_forever()
        return


if __name__ == "__main__":
    main()
