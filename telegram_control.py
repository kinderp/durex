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
import tempfile
import threading
import time
from typing import Callable, Optional

import codex_queue
from telegram_bridge import TelegramApprovalBridge, TelegramBridgeConfig, TelegramBridgeError
from voice_commands import VoiceCommand, VoiceCommandError, parse_voice_command
from voice_transcriber import VoiceTranscriber, VoiceTranscriptionError, build_voice_transcriber


DEFAULT_TASK_LIMIT = 10
MAX_TELEGRAM_MESSAGE_CHARS = 3900


@dataclass(frozen=True)
class AddCommand:
    """
    Parsed /add command payload.

    Attributes:
        title:
            Queue task title.
        prompt:
            Prompt body sent to Codex.
        workdir:
            Resolved working directory requested by the Telegram user.
        priority:
            Queue priority; lower values run earlier.
        max_attempts:
            Maximum retry attempts for non-limit failures.
    """

    title: str
    prompt: str
    workdir: str
    priority: int = 100
    max_attempts: int = 3


@dataclass(frozen=True)
class TelegramControlConfig:
    """
    Runtime settings for Telegram remote control.

    Attributes:
        allowed_workdirs:
            Absolute or user-supplied root directories accepted for remote
            ``/add`` commands.
        runner_mode:
            Worker backend started by ``/run``.
        worker_telegram_approvals:
            Reserved flag for future shared update dispatching.
        telegram_verbosity:
            Approval verbosity that will be passed to the worker once shared
            Telegram approvals are supported.
        echo_output:
            Whether PTY output should also be printed locally.
        poll_timeout_seconds:
            Telegram long-poll timeout for control messages.
        retry_base_seconds:
            Initial retry delay after transient Telegram failures.
        retry_max_seconds:
            Maximum retry delay after repeated Telegram failures.
    """

    allowed_workdirs: list[str]
    runner_mode: str = "pty"
    worker_telegram_approvals: bool = False
    telegram_verbosity: str = "normal"
    echo_output: bool = False
    poll_timeout_seconds: int = 20
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    voice_enabled: bool = False
    voice_provider: str = "faster_whisper"
    voice_model: str = "base"
    voice_language: Optional[str] = None
    voice_allowed_languages: tuple[str, ...] = ("it", "en")
    voice_workdir_aliases: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkerState:
    """
    Small in-memory state for a background worker run.

    Attributes:
        lock:
            Synchronizes access to thread state and stop/error flags.
        thread:
            Background worker thread, if one has been started.
        stop_after_current:
            Cooperative stop flag checked between queue tasks.
        last_error:
            Last worker or polling error exposed through ``/status``.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: Optional[threading.Thread] = None
    stop_after_current: bool = False
    last_error: Optional[str] = None

    def is_running(self) -> bool:
        """
        Return whether the background worker thread is currently alive.

        Returns:
            True when a worker thread exists and is still running.
        """

        with self.lock:
            return self.thread is not None and self.thread.is_alive()


class TelegramControlError(ValueError):
    """Raised when a remote command is invalid or not allowed."""


class TelegramArgumentParser(argparse.ArgumentParser):
    """ArgumentParser variant that reports errors without exiting the daemon."""

    def error(self, message: str) -> None:
        """
        Convert argparse parse errors into TelegramControlError.

        Args:
            message:
                Parser-generated error message.

        Returns:
            None.

        Raises:
            TelegramControlError:
                Always raised instead of exiting the daemon process.
        """

        raise TelegramControlError(message)


def truncate_message(text: str, max_chars: int = MAX_TELEGRAM_MESSAGE_CHARS) -> str:
    """
    Keep messages inside Telegram's message-size limit with room to spare.

    Args:
        text:
            Message body to send.
        max_chars:
            Maximum retained characters.

    Returns:
        Original text when it fits, otherwise the tail. The tail is preferred
        for task output because recent output is usually the actionable part.
    """

    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def normalize_allowed_workdirs(paths: list[str]) -> list[str]:
    """
    Return absolute allowed workdir roots.

    Args:
        paths:
            User-supplied or environment-derived workdir roots.

    Returns:
        Absolute, expanded paths used by authorization checks.
    """

    return [str(Path(path).expanduser().resolve()) for path in paths]


def path_is_allowed(path: str, allowed_roots: list[str]) -> bool:
    """
    Return True when path is inside one of the configured allowed roots.

    Args:
        path:
            Requested working directory.
        allowed_roots:
            Absolute allowed root directories.

    Returns:
        True when ``path`` resolves under an allowed root. This is the main
        filesystem boundary for Telegram-created tasks.
    """

    resolved = Path(path).expanduser().resolve()
    for root in allowed_roots:
        try:
            resolved.relative_to(Path(root))
            return True
        except ValueError:
            continue
    return False


def default_title(prompt: str) -> str:
    """
    Build a compact task title when /add omits one.

    Args:
        prompt:
            Telegram prompt body.

    Returns:
        First non-empty prompt line truncated for queue display.
    """

    first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "Remote Codex task")
    return first_line[:80]


def base_command(command: str) -> str:
    """
    Return Telegram command without an optional @BotName suffix.

    Args:
        command:
            Raw Telegram command token.

    Returns:
        Command name normalized for both private chats and bot-suffixed group
        commands.
    """

    return command.split("@", 1)[0]


def parse_add_command(text: str, default_workdir: str = ".") -> AddCommand:
    """
    Parse /add command text.

    Supported form:

        /add --title "Fix tests" --workdir /repo --priority 10
        Prompt text for Codex...

        /add --title "Fix tests" --workdir /repo --prompt "Run tests"

        /add --title "Fix tests" --workdir /repo -- Run tests

    Args:
        text:
            Full Telegram message text.
        default_workdir:
            Workdir used when the command omits ``--workdir``.

    Returns:
        Parsed AddCommand. Authorization is intentionally not performed here;
        callers must check the resolved workdir against allowed roots.

    Raises:
        TelegramControlError:
            Raised for syntax errors, non-/add commands or missing prompt body.
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
    parser.add_argument("--prompt")

    args, prompt_tokens = parser.parse_known_args(tokens[1:])

    prompt = prompt.strip() if separator else (args.prompt or "")
    if prompt_tokens:
        if prompt_tokens[0] == "--" and not prompt:
            prompt_tokens = prompt_tokens[1:]
            prompt = " ".join(prompt_tokens).strip()
        elif prompt:
            raise TelegramControlError(f"unexpected trailing argument: {prompt_tokens[0]}")
        elif prompt_tokens[0].startswith("-"):
            raise TelegramControlError(f"unrecognized argument: {prompt_tokens[0]}")
        else:
            prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        raise TelegramControlError("Missing prompt. Put it after --prompt, after --, or on the lines after /add.")

    title = args.title or default_title(prompt)
    return AddCommand(
        title=title,
        prompt=prompt,
        workdir=str(Path(args.workdir).expanduser().resolve()),
        priority=args.priority,
        max_attempts=args.max_attempts,
    )


def parse_bool_env(value: Optional[str]) -> bool:
    """
    Parse a boolean environment value.

    Args:
        value:
            Raw environment variable value.

    Returns:
        True for common enabled values.
    """

    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def parse_csv_env(value: Optional[str], default: tuple[str, ...]) -> tuple[str, ...]:
    """
    Parse a comma-separated environment value.

    Args:
        value:
            Raw environment variable value.
        default:
            Default tuple when the value is empty.

    Returns:
        Tuple of stripped non-empty values.
    """

    if not value:
        return default
    parsed = tuple(item.strip() for item in value.split(",") if item.strip())
    return parsed or default


def parse_workdir_aliases(value: Optional[str]) -> dict[str, str]:
    """
    Parse voice workdir aliases from an environment value.

    Format:
        ``durex=/lab/durex,lab durex=/lab/durex``

    Args:
        value:
            Raw alias environment variable.

    Returns:
        Mapping of spoken aliases to paths.
    """

    aliases: dict[str, str] = {}
    if not value:
        return aliases

    for item in value.split(","):
        if "=" not in item:
            continue
        alias, path = item.split("=", 1)
        alias = alias.strip()
        path = path.strip()
        if alias and path:
            aliases[alias] = path
    return aliases


def task_counts() -> dict[str, int]:
    """
    Return task counts by status.

    Returns:
        Mapping from queue status to number of tasks in that status.
    """

    codex_queue.init_db()
    with codex_queue.connect() as con:
        rows = con.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
    return {str(status): int(count) for status, count in rows}


def format_status(worker_running: bool, last_error: Optional[str]) -> str:
    """
    Build a compact status message.

    Args:
        worker_running:
            Current in-memory worker state.
        last_error:
            Last worker or polling error, if any.

    Returns:
        Telegram-friendly queue and worker status text.
    """

    counts = task_counts()
    parts = ["Durex status", f"Worker: {'running' if worker_running else 'idle'}"]
    for status in sorted(codex_queue.STATUSES):
        parts.append(f"{status}: {counts.get(status, 0)}")
    if last_error:
        parts.append(f"Last worker error: {last_error}")
    return "\n".join(parts)


def list_recent_tasks(limit: int = DEFAULT_TASK_LIMIT) -> str:
    """
    Return a Telegram-friendly task list.

    Args:
        limit:
            Maximum number of latest tasks to include.

    Returns:
        Plain-text list ordered by newest task first.
    """

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
    """
    Return the tail of one task output, defaulting to the latest task.

    Args:
        task_id:
            Optional task id. When omitted, the latest task is selected.
        chars:
            Maximum output characters before Telegram truncation.

    Returns:
        Task output/error tail or a human-readable not-found/no-output message.
    """

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
    """
    Run ready queue tasks in a background thread until no task is runnable.

    Args:
        state:
            Shared worker state and cooperative stop flag.
        config:
            Runtime control configuration.
        notify:
            Callback used to send status messages back to Telegram.

    Returns:
        None. Worker completion or errors are reported through ``notify`` and
        ``state``.
    """

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
    """
    Telegram message-command router for Durex.

    The bot maps authorized chat messages to queue operations. It deliberately
    does not expose arbitrary shell execution: every remote action either reads
    queue state, creates a queued Codex task, or starts/stops the worker.
    """

    def __init__(
        self,
        bridge: TelegramApprovalBridge,
        config: TelegramControlConfig,
        voice_transcriber: Optional[VoiceTranscriber] = None,
    ) -> None:
        """
        Initialize the Telegram control bot.

        Args:
            bridge:
                Telegram bridge used for polling and replies.
            config:
                Control runtime configuration.

        Returns:
            None.
        """

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
            voice_enabled=config.voice_enabled,
            voice_provider=config.voice_provider,
            voice_model=config.voice_model,
            voice_language=config.voice_language,
            voice_allowed_languages=config.voice_allowed_languages,
            voice_workdir_aliases=config.voice_workdir_aliases,
        )
        self.voice_transcriber = voice_transcriber
        self.worker_state = WorkerState()

    @classmethod
    def from_env(
        cls,
        allowed_workdirs: Optional[list[str]] = None,
        runner_mode: str = "pty",
        worker_telegram_approvals: bool = False,
        telegram_verbosity: str = "normal",
        echo_output: bool = False,
        voice_enabled: Optional[bool] = None,
    ) -> "TelegramControlBot":
        """
        Build a control bot from Durex Telegram environment variables.

        Args:
            allowed_workdirs:
                Optional allowed workdir roots. When omitted, the environment
                or current directory supplies the boundary.
            runner_mode:
                Worker backend used by remote ``/run``.
            worker_telegram_approvals:
                Reserved flag rejected until Telegram update dispatch is shared.
            telegram_verbosity:
                Future worker approval verbosity.
            echo_output:
                Whether worker PTY output is mirrored locally.

        Returns:
            Configured TelegramControlBot.

        Raises:
            TelegramBridgeError:
                Raised when required environment variables are missing or when
                unsupported shared Telegram approval mode is requested.
        """

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

        voice_is_enabled = parse_bool_env(os.environ.get("DUREX_VOICE_ENABLED")) if voice_enabled is None else voice_enabled
        voice_provider = os.environ.get("DUREX_VOICE_PROVIDER", "faster_whisper")
        voice_model = os.environ.get("DUREX_VOICE_MODEL", "base")
        voice_language_env = os.environ.get("DUREX_VOICE_LANGUAGE", "auto").strip()
        voice_language = None if voice_language_env in {"", "auto"} else voice_language_env
        voice_allowed_languages = parse_csv_env(os.environ.get("DUREX_VOICE_ALLOWED_LANGUAGES"), ("it", "en"))
        voice_workdir_aliases = parse_workdir_aliases(os.environ.get("DUREX_VOICE_WORKDIR_ALIASES"))

        bridge = TelegramApprovalBridge(
            TelegramBridgeConfig(bot_token=token, allowed_chat_id=int(chat_id))
        )
        voice_transcriber = None
        if voice_is_enabled:
            voice_transcriber = build_voice_transcriber(provider=voice_provider, model_name=voice_model)

        return cls(
            bridge=bridge,
            config=TelegramControlConfig(
                allowed_workdirs=allowed_workdirs,
                runner_mode=runner_mode,
                worker_telegram_approvals=worker_telegram_approvals,
                telegram_verbosity=telegram_verbosity,
                echo_output=echo_output,
                voice_enabled=voice_is_enabled,
                voice_provider=voice_provider,
                voice_model=voice_model,
                voice_language=voice_language,
                voice_allowed_languages=voice_allowed_languages,
                voice_workdir_aliases=voice_workdir_aliases,
            ),
            voice_transcriber=voice_transcriber,
        )

    def send(self, text: str) -> None:
        """
        Send one control message to Telegram.

        Args:
            text:
                Response text produced by command handling.

        Returns:
            None.
        """

        self.bridge.send_message(truncate_message(text))

    def start_worker(self) -> str:
        """
        Start the background Durex worker if it is not already running.

        Returns:
            Human-readable status message for Telegram.
        """

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
        """
        Request worker stop before the next task starts.

        Returns:
            Human-readable acknowledgement for Telegram.
        """

        with self.worker_state.lock:
            self.worker_state.stop_after_current = True
        return "Worker will stop before starting another task."

    def add_task_from_values(self, title: str, prompt: str, workdir: str, priority: int = 100, max_attempts: int = 3) -> str:
        """
        Add a task after enforcing the remote-control workdir boundary.

        Args:
            title:
                Queue task title.
            prompt:
                Codex prompt.
            workdir:
                Requested workdir.
            priority:
                Queue priority.
            max_attempts:
                Maximum retry attempts.

        Returns:
            Confirmation text.
        """

        resolved = str(Path(workdir).expanduser().resolve())
        if not path_is_allowed(resolved, self.config.allowed_workdirs):
            allowed = "\n".join(self.config.allowed_workdirs)
            raise TelegramControlError(f"Workdir is not allowed: {resolved}\nAllowed roots:\n{allowed}")
        codex_queue.init_db()
        codex_queue.add_task(
            title=title,
            prompt=prompt,
            workdir=resolved,
            priority=priority,
            max_attempts=max_attempts,
        )
        return f"Task added: {title}"

    def handle_voice_command(self, command: VoiceCommand) -> str:
        """
        Execute one parsed voice command through safe Durex operations.

        Args:
            command:
                Structured command from voice_commands.py.

        Returns:
            Telegram response text.
        """

        if command.action == "status":
            return format_status(self.worker_state.is_running(), self.worker_state.last_error)
        if command.action == "tasks":
            return list_recent_tasks(limit=command.limit or DEFAULT_TASK_LIMIT)
        if command.action == "tail":
            return tail_task_output(task_id=command.task_id)
        if command.action == "run":
            return self.start_worker()
        if command.action == "stop":
            return self.stop_worker_after_current()
        if command.action == "add":
            if not command.title or not command.workdir or not command.prompt:
                raise TelegramControlError("Voice add command is missing title, workdir or prompt.")
            return self.add_task_from_values(
                title=command.title,
                prompt=command.prompt,
                workdir=command.workdir,
                priority=command.priority,
            )
        raise TelegramControlError(f"Unsupported voice command action: {command.action}")

    def handle_text(self, text: str) -> str:
        """
        Handle one authorized Telegram message text.

        Args:
            text:
                Message text from the authorized Telegram chat.

        Returns:
            Response text that should be sent back to Telegram.

        Raises:
            TelegramControlError:
                Raised for rejected command payloads. ``handle_update`` catches
                it and converts it to a user-facing rejection message.
        """

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
            return self.add_task_from_values(
                title=add.title,
                prompt=add.prompt,
                workdir=add.workdir,
                priority=add.priority,
                max_attempts=add.max_attempts,
            )

        if command == "/run" and len(parts) == 1:
            return self.start_worker()

        if command in {"/stop", "/stop-worker"} and len(parts) == 1:
            return self.stop_worker_after_current()

        return "Unknown command. Send /help."

    def download_voice_message(self, voice: dict) -> str:
        """
        Download a Telegram voice attachment to a temporary file.

        Args:
            voice:
                Telegram voice attachment object.

        Returns:
            Local audio path.
        """

        file_id = voice.get("file_id")
        if not file_id:
            raise TelegramControlError("Voice message has no file_id.")
        file_info = self.bridge.get_file(str(file_id))
        file_path = file_info.get("file_path")
        if not file_path:
            raise TelegramControlError("Telegram did not return a voice file path.")

        suffix = Path(str(file_path)).suffix or ".ogg"
        destination = Path(tempfile.gettempdir()) / "durex_voice" / f"{file_id}{suffix}"
        return self.bridge.download_file(str(file_path), str(destination))

    def handle_voice(self, voice: dict) -> str:
        """
        Handle one authorized Telegram voice attachment.

        Args:
            voice:
                Telegram voice attachment object.

        Returns:
            Response text for Telegram.
        """

        if not self.config.voice_enabled:
            return "Voice commands are disabled. Set DUREX_VOICE_ENABLED=1 and install requirements-voice.txt."
        if self.voice_transcriber is None:
            raise TelegramControlError("Voice transcription is not configured.")

        audio_path = self.download_voice_message(voice)
        result = self.voice_transcriber.transcribe(audio_path, language=self.config.voice_language)
        if not result.text.strip():
            raise TelegramControlError("Voice transcription returned empty text.")
        if result.language and result.language not in self.config.voice_allowed_languages:
            allowed = ", ".join(self.config.voice_allowed_languages)
            raise TelegramControlError(f"Voice language not allowed: {result.language}. Allowed: {allowed}")

        command = parse_voice_command(result.text, workdir_aliases=self.config.voice_workdir_aliases)
        response = self.handle_voice_command(command)
        return f"Voice transcript: {result.text}\n\n{response}"

    def handle_update(self, update: dict) -> Optional[str]:
        """
        Process one Telegram update and return the response text, if any.

        Args:
            update:
                Raw Telegram update dictionary.

        Returns:
            Response text for handled authorized messages, otherwise None.
        """

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
        voice = message.get("voice")
        if not text and not voice:
            return None

        try:
            if text:
                response = self.handle_text(text)
            else:
                response = self.handle_voice(voice)
        except (TelegramControlError, VoiceCommandError, VoiceTranscriptionError, ValueError, sqlite3.Error) as exc:
            response = f"Command rejected: {exc}"

        self.send(response)
        return response

    def run_forever(self) -> None:
        """
        Poll Telegram messages forever and route authorized commands.

        Returns:
            None. The loop is intended to run until the process is interrupted.
        """

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

Voice commands are supported when DUREX_VOICE_ENABLED=1.
Remote control does not execute shell input directly."""


def _demo() -> None:
    """
    Run the Telegram control bot from environment configuration.

    Returns:
        None.
    """

    bot = TelegramControlBot.from_env()
    bot.run_forever()


if __name__ == "__main__":
    _demo()
