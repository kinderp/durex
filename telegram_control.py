#!/usr/bin/env python3
"""
Telegram remote control for Durex.

This module exposes Durex queue operations through Telegram bot messages. It is
deliberately not a shell bridge: remote users can enqueue Codex tasks and start
the Durex worker, but arbitrary terminal input is left to a future policy layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import argparse
import os
from pathlib import Path
import shlex
import sqlite3
import threading
import time
from typing import Callable, Optional

import codex_queue
from telegram_bridge import TelegramApprovalBridge, TelegramBridgeConfig, TelegramBridgeError


DEFAULT_TASK_LIMIT = 10
MAX_TELEGRAM_MESSAGE_CHARS = 3900


@dataclass(frozen=True)
class AddCommand:
    """Parsed /add command payload."""

    title: str
    prompt: str
    workdir: str
    priority: int = 100
    max_attempts: int = 3


@dataclass(frozen=True)
class TelegramControlConfig:
    """Runtime settings for Telegram remote control."""

    allowed_workdirs: list[str]
    runner_mode: str = "pty"
    worker_telegram_approvals: bool = False
    telegram_verbosity: str = "normal"
    echo_output: bool = False
    poll_timeout_seconds: int = 20
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0


@dataclass
class WorkerState:
    """Small in-memory state for a background worker run."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: Optional[threading.Thread] = None
    stop_after_current: bool = False
    last_error: Optional[str] = None

    def is_running(self) -> bool:
        with self.lock:
            return self.thread is not None and self.thread.is_alive()


class TelegramControlError(ValueError):
    """Raised when a remote command is invalid or not allowed."""


class TelegramArgumentParser(argparse.ArgumentParser):
    """ArgumentParser variant that reports errors without exiting the daemon."""

    def error(self, message: str) -> None:
        raise TelegramControlError(message)


def truncate_message(text: str, max_chars: int = MAX_TELEGRAM_MESSAGE_CHARS) -> str:
    """Keep messages inside Telegram's message-size limit with room to spare."""

    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def normalize_allowed_workdirs(paths: list[str]) -> list[str]:
    """Return absolute allowed workdir roots."""

    return [str(Path(path).expanduser().resolve()) for path in paths]


def path_is_allowed(path: str, allowed_roots: list[str]) -> bool:
    """Return True when path is inside one of the configured allowed roots."""

    resolved = Path(path).expanduser().resolve()
    for root in allowed_roots:
        try:
            resolved.relative_to(Path(root))
            return True
        except ValueError:
            continue
    return False


def default_title(prompt: str) -> str:
    """Build a compact task title when /add omits one."""

    first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "Remote Codex task")
    return first_line[:80]


def base_command(command: str) -> str:
    """Return Telegram command without an optional @BotName suffix."""

    return command.split("@", 1)[0]


def parse_add_command(text: str, default_workdir: str = ".") -> AddCommand:
    """
    Parse /add command text.

    Supported form:

        /add --title "Fix tests" --workdir /repo --priority 10
        Prompt text for Codex...
    """

    header, separator, prompt = text.partition("\n")
    tokens = shlex.split(header)
    if not tokens or base_command(tokens[0]) != "/add":
        raise TelegramControlError("Expected /add command.")

    parser = TelegramArgumentParser(prog="/add", add_help=False)
    parser.add_argument("--title")
    parser.add_argument("--workdir", default=default_workdir)
    parser.add_argument("--priority", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)

    args = parser.parse_args(tokens[1:])

    prompt = prompt.strip() if separator else ""
    if not prompt:
        raise TelegramControlError("Missing prompt. Put it on the lines after /add.")

    title = args.title or default_title(prompt)
    return AddCommand(
        title=title,
        prompt=prompt,
        workdir=str(Path(args.workdir).expanduser().resolve()),
        priority=args.priority,
        max_attempts=args.max_attempts,
    )


def task_counts() -> dict[str, int]:
    """Return task counts by status."""

    codex_queue.init_db()
    with codex_queue.connect() as con:
        rows = con.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
    return {str(status): int(count) for status, count in rows}


def format_status(worker_running: bool, last_error: Optional[str]) -> str:
    """Build a compact status message."""

    counts = task_counts()
    parts = ["Durex status", f"Worker: {'running' if worker_running else 'idle'}"]
    for status in sorted(codex_queue.STATUSES):
        parts.append(f"{status}: {counts.get(status, 0)}")
    if last_error:
        parts.append(f"Last worker error: {last_error}")
    return "\n".join(parts)


def list_recent_tasks(limit: int = DEFAULT_TASK_LIMIT) -> str:
    """Return a Telegram-friendly task list."""

    codex_queue.init_db()
    with codex_queue.connect() as con:
        rows = con.execute(
            """
            SELECT id, title, status, priority, attempts
            FROM tasks
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        return "No tasks found."

    lines = ["Recent tasks"]
    for task_id, title, status, priority, attempts in rows:
        lines.append(f"#{task_id} {status} p={priority} attempts={attempts} - {title}")
    return "\n".join(lines)


def tail_task_output(task_id: Optional[int] = None, chars: int = 2500) -> str:
    """Return the tail of one task output, defaulting to the latest task."""

    codex_queue.init_db()
    with codex_queue.connect() as con:
        if task_id is None:
            row = con.execute(
                "SELECT id, title, output, last_error FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = con.execute(
                "SELECT id, title, output, last_error FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()

    if row is None:
        return "Task not found."

    output = row[2] or row[3] or ""
    if not output:
        return f"Task #{row[0]} has no output yet."
    return truncate_message(f"Task #{row[0]} - {row[1]}\n\n{output[-chars:]}")


def run_worker_until_empty(
    state: WorkerState,
    config: TelegramControlConfig,
    notify: Callable[[str], None],
) -> None:
    """Run ready queue tasks in a background thread until no task is runnable."""

    try:
        codex_queue.init_db()
        while True:
            with state.lock:
                if state.stop_after_current:
                    state.stop_after_current = False
                    notify("Worker stopped before starting another task.")
                    return

            task = codex_queue.get_next_task()
            if task is None:
                notify("No executable tasks found. Worker is idle.")
                return

            notify(f"Starting task #{task['id']}: {task['title']}")
            codex_queue.run_task(
                task,
                runner_mode=config.runner_mode,
                telegram_enabled=config.worker_telegram_approvals,
                telegram_verbosity=config.telegram_verbosity,
                echo_output=config.echo_output,
            )
    except Exception as exc:
        with state.lock:
            state.last_error = str(exc)
        notify(f"Worker error: {exc}")


class TelegramControlBot:
    """Telegram message-command router for Durex."""

    def __init__(self, bridge: TelegramApprovalBridge, config: TelegramControlConfig) -> None:
        self.bridge = bridge
        self.config = TelegramControlConfig(
            allowed_workdirs=normalize_allowed_workdirs(config.allowed_workdirs),
            runner_mode=config.runner_mode,
            worker_telegram_approvals=config.worker_telegram_approvals,
            telegram_verbosity=config.telegram_verbosity,
            echo_output=config.echo_output,
            poll_timeout_seconds=config.poll_timeout_seconds,
            retry_base_seconds=config.retry_base_seconds,
            retry_max_seconds=config.retry_max_seconds,
        )
        self.worker_state = WorkerState()

    @classmethod
    def from_env(
        cls,
        allowed_workdirs: Optional[list[str]] = None,
        runner_mode: str = "pty",
        worker_telegram_approvals: bool = False,
        telegram_verbosity: str = "normal",
        echo_output: bool = False,
    ) -> "TelegramControlBot":
        """Build a control bot from Durex Telegram environment variables."""

        token = os.environ.get("DUREX_TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("DUREX_TELEGRAM_CHAT_ID")
        if not token:
            raise TelegramBridgeError("Missing DUREX_TELEGRAM_BOT_TOKEN.")
        if not chat_id:
            raise TelegramBridgeError("Missing DUREX_TELEGRAM_CHAT_ID.")
        if worker_telegram_approvals:
            raise TelegramBridgeError(
                "--worker-telegram-approvals is not supported with telegram-control yet. "
                "It would create competing Telegram getUpdates consumers."
            )

        if allowed_workdirs is None:
            raw = os.environ.get("DUREX_TELEGRAM_ALLOWED_WORKDIRS", os.getcwd())
            allowed_workdirs = [path for path in raw.split(os.pathsep) if path]

        bridge = TelegramApprovalBridge(
            TelegramBridgeConfig(bot_token=token, allowed_chat_id=int(chat_id))
        )
        return cls(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=allowed_workdirs,
                runner_mode=runner_mode,
                worker_telegram_approvals=worker_telegram_approvals,
                telegram_verbosity=telegram_verbosity,
                echo_output=echo_output,
            ),
        )

    def send(self, text: str) -> None:
        """Send one control message to Telegram."""

        self.bridge.send_message(truncate_message(text))

    def start_worker(self) -> str:
        """Start the background Durex worker if it is not already running."""

        with self.worker_state.lock:
            if self.worker_state.thread is not None and self.worker_state.thread.is_alive():
                return "Worker is already running."

            self.worker_state.last_error = None
            self.worker_state.stop_after_current = False
            thread = threading.Thread(
                target=run_worker_until_empty,
                args=(self.worker_state, self.config, self.send),
                daemon=True,
            )
            self.worker_state.thread = thread
            thread.start()
            return "Worker started."

    def stop_worker_after_current(self) -> str:
        """Request worker stop before the next task starts."""

        with self.worker_state.lock:
            self.worker_state.stop_after_current = True
        return "Worker will stop before starting another task."

    def handle_text(self, text: str) -> str:
        """Handle one authorized Telegram message text."""

        stripped = text.strip()
        parts = stripped.split()
        command = base_command(parts[0]) if parts else ""

        if command in {"/help", "/start"}:
            return HELP_TEXT

        if command == "/status" and len(parts) == 1:
            return format_status(self.worker_state.is_running(), self.worker_state.last_error)

        if command == "/tasks":
            limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_TASK_LIMIT
            return list_recent_tasks(limit=limit)

        if command == "/tail":
            task_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            return tail_task_output(task_id=task_id)

        if command == "/add":
            add = parse_add_command(stripped)
            if not path_is_allowed(add.workdir, self.config.allowed_workdirs):
                allowed = "\n".join(self.config.allowed_workdirs)
                raise TelegramControlError(f"Workdir is not allowed: {add.workdir}\nAllowed roots:\n{allowed}")
            codex_queue.init_db()
            codex_queue.add_task(
                title=add.title,
                prompt=add.prompt,
                workdir=add.workdir,
                priority=add.priority,
                max_attempts=add.max_attempts,
            )
            return f"Task added: {add.title}"

        if command == "/run" and len(parts) == 1:
            return self.start_worker()

        if command in {"/stop", "/stop-worker"} and len(parts) == 1:
            return self.stop_worker_after_current()

        return "Unknown command. Send /help."

    def handle_update(self, update: dict) -> Optional[str]:
        """Process one Telegram update and return the response text, if any."""

        message = update.get("message")
        if not message:
            return None

        chat = message.get("chat", {})
        try:
            chat_id = int(chat.get("id", 0))
        except (TypeError, ValueError):
            return None

        if chat_id != self.bridge.config.allowed_chat_id:
            return None

        text = message.get("text")
        if not text:
            return None

        try:
            response = self.handle_text(text)
        except (TelegramControlError, ValueError, sqlite3.Error) as exc:
            response = f"Command rejected: {exc}"

        self.send(response)
        return response

    def run_forever(self) -> None:
        """Poll Telegram messages forever and route authorized commands."""

        self.send("Durex Telegram control is online. Send /help.")
        retry_delay = self.config.retry_base_seconds
        while True:
            try:
                updates = self.bridge.poll_updates(
                    timeout=self.config.poll_timeout_seconds,
                    allowed_updates=["message"],
                )
                retry_delay = self.config.retry_base_seconds
                for update in updates:
                    self.handle_update(update)
            except TelegramBridgeError as exc:
                with self.worker_state.lock:
                    self.worker_state.last_error = str(exc)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self.config.retry_max_seconds)


HELP_TEXT = """Durex Telegram control

/status - show queue and worker status
/tasks [limit] - list recent tasks
/add --title "Title" --workdir /allowed/path
Prompt text for Codex...
/run - start the Durex worker until the queue is empty
/tail [task_id] - show task output tail
/stop - stop worker before the next task starts

Remote control does not execute shell input directly."""


def _demo() -> None:
    bot = TelegramControlBot.from_env()
    bot.run_forever()


if __name__ == "__main__":
    _demo()
