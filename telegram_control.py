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
import secrets
import json
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
import threading
import time
from typing import Callable, Optional

import codex_queue
from telegram_bridge import (
    DEFAULT_TELEGRAM_FILE_MAX_BYTES,
    TelegramApprovalBridge,
    TelegramBridgeConfig,
    TelegramBridgeError,
)
from voice_commands import ALIASABLE_ACTIONS, VoiceCommand, VoiceCommandError, normalize_transcript, parse_voice_command
from voice_transcriber import VoiceTranscriber, VoiceTranscriptionError, build_voice_transcriber


DEFAULT_TASK_LIMIT = 10
MAX_TELEGRAM_MESSAGE_CHARS = 3900
DEFAULT_VOICE_ALIASES_FILE = ".durex_voice_aliases.json"
DEFAULT_CONFIG_FILE = "config.yaml"


LEARN_ACTION_ALIASES = {
    "status": "status",
    "stato": "status",
    "tasks": "tasks",
    "task": "tasks",
    "lista": "tasks",
    "tail": "tail",
    "output": "tail",
    "run": "run",
    "start": "run",
    "avvia": "run",
    "stop": "stop",
    "ferma": "stop",
}

DEFAULT_VOICE_MAX_DURATION_SECONDS = 300


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
    voice_command_aliases: dict[str, str] = field(default_factory=dict)
    voice_aliases_file: str = DEFAULT_VOICE_ALIASES_FILE
    voice_debug: bool = False
    voice_max_file_bytes: int = DEFAULT_TELEGRAM_FILE_MAX_BYTES
    voice_max_duration_seconds: int = DEFAULT_VOICE_MAX_DURATION_SECONDS
    workdir_choices: dict[str, str] = field(default_factory=dict)


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


@dataclass
class AddWizardState:
    """
    In-memory state for a guided add-task flow.

    Attributes:
        token:
            Short callback token.
        workdir:
            Selected task workdir.
        priority:
            Selected queue priority.
        prompt:
            Captured prompt text.
        phase:
            Current wizard phase.
    """

    token: str
    workdir: Optional[str] = None
    priority: int = 100
    prompt: Optional[str] = None
    phase: str = "workdir"


class TelegramControlError(ValueError):
    """Raised when a remote command is invalid or not allowed."""


class VoiceCommandNotRecognized(TelegramControlError):
    """Raised when voice transcription produced no recognized command."""

    def __init__(self, attempts: list[str], candidates: list[str]) -> None:
        self.attempts = attempts
        self.candidates = candidates
        super().__init__(
            "Voice command not recognized after transcription attempts: "
            + "; ".join(attempts)
            + ". Tap a Learn button to map one transcript to a safe action."
        )


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


def parse_positive_int_setting(value: object, default: int, setting: str) -> int:
    """Return a positive integer configuration value or raise a clear error."""

    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TelegramControlError(f"{setting} must be a positive integer.") from exc
    if parsed <= 0:
        raise TelegramControlError(f"{setting} must be a positive integer.")
    return parsed


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


def load_yaml_config(path: str) -> dict:
    """
    Load an optional YAML configuration file.

    Args:
        path:
            YAML file path.

    Returns:
        Parsed dictionary, or an empty dictionary when the file is absent.

    Raises:
        TelegramControlError:
            Raised when the file exists but PyYAML is unavailable or parsing
            fails.
    """

    config_path = Path(path).expanduser()
    if not config_path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:
        raise TelegramControlError(
            f"Config file {config_path} requires PyYAML. Install it or remove DUREX_CONFIG."
        ) from exc
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TelegramControlError(f"Could not parse config file {config_path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def parse_workdir_choices(value: object) -> dict[str, str]:
    """
    Parse configured workdir choices.

    Supported shapes:
        ``{"durex": "/lab/durex"}``
        ``[{"label": "durex", "path": "/lab/durex"}]``

    Args:
        value:
            Raw config value.

    Returns:
        Mapping of button labels to workdir paths.
    """

    choices: dict[str, str] = {}
    if isinstance(value, dict):
        for label, path in value.items():
            if label and path:
                choices[str(label)] = str(path)
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or item.get("name")
            path = item.get("path")
            if label and path:
                choices[str(label)] = str(path)
    return choices


def merge_workdir_choices(*choices_maps: dict[str, str]) -> dict[str, str]:
    """
    Merge workdir choices preserving later overrides.

    Args:
        choices_maps:
            Workdir choice mappings in increasing priority.

    Returns:
        Merged mapping.
    """

    merged: dict[str, str] = {}
    for choices in choices_maps:
        merged.update(choices)
    return merged


def normalize_learn_action(value: str) -> str:
    """
    Normalize a /learn action label to a safe voice command action.

    Args:
        value:
            User-provided action label.

    Returns:
        Canonical action name.

    Raises:
        TelegramControlError:
            Raised when the action cannot be learned safely.
    """

    action = LEARN_ACTION_ALIASES.get(normalize_transcript(value))
    if action not in ALIASABLE_ACTIONS:
        allowed = ", ".join(sorted(ALIASABLE_ACTIONS))
        raise TelegramControlError(f"Unsupported learn action: {value}. Allowed: {allowed}")
    return action


def parse_learn_command(text: str) -> tuple[str, str]:
    """
    Parse a text /learn command.

    Args:
        text:
            Full Telegram message text.

    Returns:
        Tuple of canonical action and spoken phrase.
    """

    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3 or base_command(parts[0]) != "/learn":
        raise TelegramControlError("Usage: /learn <status|tasks|tail|run|stop> <spoken phrase>")

    action = normalize_learn_action(parts[1])
    phrase = normalize_transcript(parts[2])
    if not phrase:
        raise TelegramControlError("Missing spoken phrase for /learn.")
    return action, phrase


def load_voice_command_aliases(path: str) -> dict[str, str]:
    """
    Load voice command aliases from a JSON file.

    Supported JSON shape:
        ``{"run": ["abbia walker"], "tasks": ["lista tasche"]}``

    Args:
        path:
            Alias JSON path.

    Returns:
        Mapping of normalized phrase to canonical action.
    """

    alias_path = Path(path).expanduser()
    if not alias_path.exists():
        return {}

    try:
        raw = json.loads(alias_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TelegramControlError(f"Could not read voice aliases file {alias_path}: {exc}") from exc

    aliases: dict[str, str] = {}
    if isinstance(raw, dict):
        for action, phrases in raw.items():
            canonical = normalize_learn_action(str(action))
            if isinstance(phrases, str):
                phrases = [phrases]
            if not isinstance(phrases, list):
                continue
            for phrase in phrases:
                normalized = normalize_transcript(str(phrase))
                if normalized:
                    aliases[normalized] = canonical
    return aliases


def save_voice_command_alias(path: str, action: str, phrase: str) -> None:
    """
    Persist one learned voice command alias.

    Args:
        path:
            Alias JSON path.
        action:
            Canonical action name.
        phrase:
            Normalized spoken phrase.
    """

    alias_path = Path(path).expanduser()
    data: dict[str, list[str]] = {}
    if alias_path.exists():
        try:
            raw = json.loads(alias_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TelegramControlError(f"Could not read voice aliases file {alias_path}: {exc}") from exc
        if isinstance(raw, dict):
            for raw_action, phrases in raw.items():
                canonical = normalize_learn_action(str(raw_action))
                if isinstance(phrases, str):
                    phrases = [phrases]
                if isinstance(phrases, list):
                    data[canonical] = [normalize_transcript(str(item)) for item in phrases if normalize_transcript(str(item))]

    data.setdefault(action, [])
    if phrase not in data[action]:
        data[action].append(phrase)

    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def unique_normalized_phrases(values: list[str]) -> list[str]:
    """
    Return unique normalized non-empty phrases preserving order.

    Args:
        values:
            Candidate transcript strings.

    Returns:
        Deduplicated normalized phrases.
    """

    phrases: list[str] = []
    seen: set[str] = set()
    for value in values:
        phrase = normalize_transcript(value)
        if phrase and phrase not in seen:
            seen.add(phrase)
            phrases.append(phrase)
    return phrases


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


def recent_task_rows(limit: int = DEFAULT_TASK_LIMIT) -> list[sqlite3.Row]:
    """
    Return recent task rows for interactive Telegram views.

    Args:
        limit:
            Maximum number of rows.

    Returns:
        SQLite rows ordered newest first.
    """

    codex_queue.init_db()
    with codex_queue.connect() as con:
        con.row_factory = sqlite3.Row
        return con.execute(
            """
            SELECT id, title, status, priority, attempts, max_attempts, workdir, last_error
            FROM tasks
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def format_recent_tasks_view(rows: list[sqlite3.Row]) -> str:
    """
    Format recent task rows for Telegram.

    Args:
        rows:
            Recent task rows.

    Returns:
        Telegram message text.
    """

    if not rows:
        return "No tasks found."
    lines = ["Recent tasks"]
    for row in rows:
        lines.append(f"#{row['id']} {row['status']} p={row['priority']} - {row['title']}")
    return "\n".join(lines)


def build_tasks_keyboard(rows: list[sqlite3.Row]) -> dict:
    """
    Build task-list inline keyboard.

    Args:
        rows:
            Recent task rows.

    Returns:
        Telegram reply markup.
    """

    task_buttons = [[{"text": f"Task #{row['id']}", "callback_data": f"durextask:{row['id']}:details"}] for row in rows[:8]]
    task_buttons.append(
        [
            {"text": "Refresh", "callback_data": "durextasks:refresh"},
            {"text": "Run", "callback_data": "durexcontrol:run"},
            {"text": "Stop", "callback_data": "durexcontrol:stop"},
        ]
    )
    return {"inline_keyboard": task_buttons}


def task_detail(task_id: int) -> tuple[str, dict]:
    """
    Return task detail text and keyboard.

    Args:
        task_id:
            Task id.

    Returns:
        Tuple of text and reply markup.
    """

    codex_queue.init_db()
    with codex_queue.connect() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT id, title, status, priority, attempts, max_attempts, workdir,
                   reset_at, session_id, last_error
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        raise TelegramControlError("Task not found.")

    lines = [
        f"Task #{row['id']}",
        f"Title: {row['title']}",
        f"Status: {row['status']}",
        f"Priority: {row['priority']}",
        f"Attempts: {row['attempts']}/{row['max_attempts']}",
        f"Workdir: {row['workdir']}",
    ]
    if row["reset_at"]:
        lines.append(f"Reset at: {row['reset_at']}")
    if row["session_id"]:
        lines.append(f"Session: {row['session_id']}")
    if row["last_error"]:
        lines.append(f"Last error: {row['last_error']}")

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Tail Output", "callback_data": f"durextask:{task_id}:tail"},
                {"text": "Back", "callback_data": "durextasks:refresh"},
            ],
            [
                {"text": "Run", "callback_data": "durexcontrol:run"},
                {"text": "Stop", "callback_data": "durexcontrol:stop"},
            ],
        ]
    }
    return "\n".join(lines), keyboard


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
        normalized_allowed_workdirs = normalize_allowed_workdirs(config.allowed_workdirs)
        workdir_choices = config.workdir_choices or {Path(path).name or path: path for path in normalized_allowed_workdirs}
        self.config = TelegramControlConfig(
            allowed_workdirs=normalized_allowed_workdirs,
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
            voice_command_aliases=config.voice_command_aliases,
            voice_aliases_file=config.voice_aliases_file,
            voice_debug=config.voice_debug,
            voice_max_file_bytes=config.voice_max_file_bytes,
            voice_max_duration_seconds=config.voice_max_duration_seconds,
            workdir_choices={label: str(Path(path).expanduser().resolve()) for label, path in workdir_choices.items()},
        )
        self.voice_transcriber = voice_transcriber
        self.worker_state = WorkerState()
        self.pending_voice_learns: dict[str, str] = {}
        self.next_reply_markup: Optional[dict] = None
        self.add_wizards: dict[str, AddWizardState] = {}
        self.active_add_wizard_token: Optional[str] = None

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

        config_file = os.environ.get("DUREX_CONFIG", DEFAULT_CONFIG_FILE)
        file_config = load_yaml_config(config_file)
        control_config = file_config.get("telegram_control", {}) if isinstance(file_config.get("telegram_control"), dict) else {}
        voice_config = control_config.get("voice", {}) if isinstance(control_config.get("voice"), dict) else {}

        if allowed_workdirs is None:
            raw = os.environ.get("DUREX_TELEGRAM_ALLOWED_WORKDIRS")
            if raw:
                allowed_workdirs = [path for path in raw.split(os.pathsep) if path]
            elif isinstance(control_config.get("allowed_workdirs"), list):
                allowed_workdirs = [str(path) for path in control_config["allowed_workdirs"] if path]
            else:
                allowed_workdirs = [os.getcwd()]

        config_workdir_choices = parse_workdir_choices(control_config.get("workdir_choices"))
        env_workdir_choices = parse_workdir_aliases(os.environ.get("DUREX_TELEGRAM_WORKDIR_CHOICES"))
        workdir_choices = merge_workdir_choices(config_workdir_choices, env_workdir_choices)
        if not workdir_choices:
            workdir_choices = {Path(path).name or path: path for path in allowed_workdirs}

        configured_voice_enabled = voice_config.get("enabled")
        voice_enabled_env = os.environ.get("DUREX_VOICE_ENABLED")
        if voice_enabled is not None:
            voice_is_enabled = voice_enabled
        elif voice_enabled_env is not None:
            voice_is_enabled = parse_bool_env(voice_enabled_env)
        elif configured_voice_enabled is not None:
            voice_is_enabled = bool(configured_voice_enabled)
        else:
            voice_is_enabled = False
        voice_provider = os.environ.get("DUREX_VOICE_PROVIDER", str(voice_config.get("provider", "faster_whisper")))
        voice_model = os.environ.get("DUREX_VOICE_MODEL", str(voice_config.get("model", "base")))
        voice_language_env = os.environ.get("DUREX_VOICE_LANGUAGE", str(voice_config.get("language", "auto"))).strip()
        voice_language = None if voice_language_env in {"", "auto"} else voice_language_env
        config_allowed_languages = tuple(str(item) for item in voice_config.get("allowed_languages", ("it", "en")))
        voice_allowed_languages = parse_csv_env(os.environ.get("DUREX_VOICE_ALLOWED_LANGUAGES"), config_allowed_languages)
        config_workdir_aliases = parse_workdir_choices(voice_config.get("workdir_aliases"))
        env_workdir_aliases = parse_workdir_aliases(os.environ.get("DUREX_VOICE_WORKDIR_ALIASES"))
        voice_workdir_aliases = merge_workdir_choices(workdir_choices, config_workdir_aliases, env_workdir_aliases)
        voice_aliases_file = os.environ.get("DUREX_VOICE_ALIASES_FILE", str(voice_config.get("aliases_file", DEFAULT_VOICE_ALIASES_FILE)))
        voice_debug_env = os.environ.get("DUREX_VOICE_DEBUG")
        voice_debug = (
            parse_bool_env(voice_debug_env)
            if voice_debug_env is not None
            else bool(voice_config.get("debug", False))
        )
        voice_max_file_bytes = parse_positive_int_setting(
            os.environ.get("DUREX_VOICE_MAX_FILE_BYTES", voice_config.get("max_file_bytes")),
            DEFAULT_TELEGRAM_FILE_MAX_BYTES,
            "DUREX_VOICE_MAX_FILE_BYTES",
        )
        voice_max_duration_seconds = parse_positive_int_setting(
            os.environ.get("DUREX_VOICE_MAX_DURATION_SECONDS", voice_config.get("max_duration_seconds")),
            DEFAULT_VOICE_MAX_DURATION_SECONDS,
            "DUREX_VOICE_MAX_DURATION_SECONDS",
        )
        voice_command_aliases = load_voice_command_aliases(voice_aliases_file)

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
                voice_command_aliases=voice_command_aliases,
                voice_aliases_file=voice_aliases_file,
                voice_debug=voice_debug,
                voice_max_file_bytes=voice_max_file_bytes,
                voice_max_duration_seconds=voice_max_duration_seconds,
                workdir_choices=workdir_choices,
            ),
            voice_transcriber=voice_transcriber,
        )

    def send(self, text: str, reply_markup: Optional[dict] = None) -> None:
        """
        Send one control message to Telegram.

        Args:
            text:
                Response text produced by command handling.
            reply_markup:
                Optional Telegram inline keyboard payload.

        Returns:
            None.
        """

        self.bridge.send_message(truncate_message(text), reply_markup=reply_markup)

    def build_voice_learn_keyboard(self, phrase: str) -> dict:
        """
        Build an inline keyboard for learning one transcript candidate.

        Args:
            phrase:
                Normalized transcript candidate.

        Returns:
            Telegram inline keyboard reply markup.
        """

        token = secrets.token_urlsafe(8)
        self.pending_voice_learns[token] = phrase
        return {
            "inline_keyboard": [
                [
                    {"text": "Learn Status", "callback_data": f"durexlearn:{token}:status"},
                    {"text": "Learn Tasks", "callback_data": f"durexlearn:{token}:tasks"},
                ],
                [
                    {"text": "Learn Tail", "callback_data": f"durexlearn:{token}:tail"},
                    {"text": "Learn Run", "callback_data": f"durexlearn:{token}:run"},
                ],
                [
                    {"text": "Learn Stop", "callback_data": f"durexlearn:{token}:stop"},
                ],
            ]
        }

    def prepare_tasks_view(self, limit: int = DEFAULT_TASK_LIMIT) -> str:
        """
        Prepare recent tasks with inline buttons.

        Args:
            limit:
                Maximum task count.

        Returns:
            Message text. The keyboard is stored for the next reply.
        """

        rows = recent_task_rows(limit)
        text = format_recent_tasks_view(rows)
        self.next_reply_markup = build_tasks_keyboard(rows)
        return text

    def start_add_wizard(self) -> str:
        """
        Start a guided add-task flow.

        Returns:
            Wizard prompt text.
        """

        token = secrets.token_urlsafe(8)
        self.add_wizards[token] = AddWizardState(token=token)
        return self.render_add_workdir_step(token)

    def render_add_workdir_step(self, token: str) -> str:
        """
        Render the workdir selection step.

        Args:
            token:
                Wizard token.

        Returns:
            Telegram text.
        """

        choices = list(self.config.workdir_choices.items())
        rows = [
            [{"text": label, "callback_data": f"durexadd:{token}:workdir:{index}"}]
            for index, (label, _path) in enumerate(choices[:20])
        ]
        rows.append([{"text": "Cancel", "callback_data": f"durexadd:{token}:cancel"}])
        self.next_reply_markup = {"inline_keyboard": rows}
        return "New task: choose workdir."

    def render_add_priority_step(self, token: str) -> str:
        """
        Render priority preset and stepper controls.

        Args:
            token:
                Wizard token.

        Returns:
            Telegram text.
        """

        state = self.add_wizards[token]
        self.next_reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "1 urgent", "callback_data": f"durexadd:{token}:preset:1"},
                    {"text": "10 high", "callback_data": f"durexadd:{token}:preset:10"},
                ],
                [
                    {"text": "100 normal", "callback_data": f"durexadd:{token}:preset:100"},
                    {"text": "999 low", "callback_data": f"durexadd:{token}:preset:999"},
                ],
                [
                    {"text": "-10", "callback_data": f"durexadd:{token}:dec:10"},
                    {"text": "-5", "callback_data": f"durexadd:{token}:dec:5"},
                    {"text": "-1", "callback_data": f"durexadd:{token}:dec:1"},
                    {"text": "+1", "callback_data": f"durexadd:{token}:inc:1"},
                    {"text": "+5", "callback_data": f"durexadd:{token}:inc:5"},
                    {"text": "+10", "callback_data": f"durexadd:{token}:inc:10"},
                ],
                [
                    {"text": "Next: Prompt", "callback_data": f"durexadd:{token}:prompt"},
                    {"text": "Cancel", "callback_data": f"durexadd:{token}:cancel"},
                ],
            ]
        }
        return f"New task: choose priority.\nCurrent priority: {state.priority}"

    def render_add_confirm_step(self, token: str) -> str:
        """
        Render final add-task confirmation.

        Args:
            token:
                Wizard token.

        Returns:
            Telegram text.
        """

        state = self.add_wizards[token]
        title = default_title(state.prompt or "")
        self.next_reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Create Task", "callback_data": f"durexadd:{token}:create"},
                    {"text": "Cancel", "callback_data": f"durexadd:{token}:cancel"},
                ]
            ]
        }
        return (
            "Create task?\n"
            f"Title: {title}\n"
            f"Workdir: {state.workdir}\n"
            f"Priority: {state.priority}\n"
            f"Prompt: {state.prompt}"
        )

    def prepare_config_view(self) -> str:
        """
        Prepare runtime config controls.

        Returns:
            Config view text.
        """

        self.next_reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": f"Voice debug: {'ON' if self.config.voice_debug else 'OFF'}",
                        "callback_data": "durexconfig:voice_debug",
                    }
                ]
            ]
        }
        return "Durex config"

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
            return self.prepare_tasks_view(limit=command.limit or DEFAULT_TASK_LIMIT)
        if command.action == "tail":
            return tail_task_output(task_id=command.task_id)
        if command.action == "run":
            return self.start_worker()
        if command.action == "stop":
            return self.stop_worker_after_current()
        if command.action == "add_wizard":
            return self.start_add_wizard()
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
            return self.prepare_tasks_view(limit=limit)

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

        if command in {"/add-wizard", "/new-task"}:
            return self.start_add_wizard()

        if command == "/config":
            return self.prepare_config_view()

        if command == "/learn":
            action, phrase = parse_learn_command(stripped)
            save_voice_command_alias(self.config.voice_aliases_file, action, phrase)
            self.config.voice_command_aliases[phrase] = action
            return f"Learned voice alias: '{phrase}' -> {action}"

        if command == "/run" and len(parts) == 1:
            return self.start_worker()

        if command in {"/stop", "/stop-worker"} and len(parts) == 1:
            return self.stop_worker_after_current()

        return "Unknown command. Send /help."

    def handle_learn_callback(self, callback: dict) -> Optional[str]:
        """
        Handle one inline Learn callback.

        Args:
            callback:
                Telegram callback_query payload.

        Returns:
            Response text when handled, otherwise None.
        """

        message = callback.get("message", {})
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        try:
            chat_id = int(chat.get("id", 0))
        except (TypeError, ValueError):
            return None
        if chat_id != self.bridge.config.allowed_chat_id:
            return None

        data = str(callback.get("data", ""))
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "durexlearn":
            return None

        token = parts[1]
        action = normalize_learn_action(parts[2])
        phrase = self.pending_voice_learns.pop(token, None)
        if not phrase:
            raise TelegramControlError("Learn candidate expired. Send the voice command again.")

        save_voice_command_alias(self.config.voice_aliases_file, action, phrase)
        self.config.voice_command_aliases[phrase] = action

        callback_id = callback.get("id")
        if callback_id and hasattr(self.bridge, "answer_callback_query"):
            self.bridge.answer_callback_query(str(callback_id), text="Learned")
        return f"Learned voice alias: '{phrase}' -> {action}"

    def handle_interactive_callback(self, callback: dict) -> Optional[str]:
        """
        Handle task/control inline callbacks.

        Args:
            callback:
                Telegram callback_query payload.

        Returns:
            Response text when handled, otherwise None.
        """

        message = callback.get("message", {})
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        try:
            chat_id = int(chat.get("id", 0))
        except (TypeError, ValueError):
            return None
        if chat_id != self.bridge.config.allowed_chat_id:
            return None

        data = str(callback.get("data", ""))
        callback_id = callback.get("id")

        if data == "durextasks:refresh":
            if callback_id and hasattr(self.bridge, "answer_callback_query"):
                self.bridge.answer_callback_query(str(callback_id), text="Refreshed")
            return self.prepare_tasks_view()

        if data == "durexcontrol:run":
            response = self.start_worker()
            if callback_id and hasattr(self.bridge, "answer_callback_query"):
                self.bridge.answer_callback_query(str(callback_id), text=response)
            return response

        if data == "durexcontrol:stop":
            response = self.stop_worker_after_current()
            if callback_id and hasattr(self.bridge, "answer_callback_query"):
                self.bridge.answer_callback_query(str(callback_id), text="Stop requested")
            return response

        if data == "durexconfig:voice_debug":
            object.__setattr__(self.config, "voice_debug", not self.config.voice_debug)
            if callback_id and hasattr(self.bridge, "answer_callback_query"):
                self.bridge.answer_callback_query(str(callback_id), text="Toggled")
            return self.prepare_config_view()

        parts = data.split(":")
        if len(parts) >= 3 and parts[0] == "durexadd":
            token = parts[1]
            action = parts[2]
            state = self.add_wizards.get(token)
            if state is None:
                raise TelegramControlError("Add wizard expired. Start again with /add-wizard.")
            if callback_id and hasattr(self.bridge, "answer_callback_query"):
                self.bridge.answer_callback_query(str(callback_id), text="OK")

            if action == "cancel":
                self.add_wizards.pop(token, None)
                if self.active_add_wizard_token == token:
                    self.active_add_wizard_token = None
                return "Add task cancelled."

            if action == "workdir" and len(parts) == 4:
                choices = list(self.config.workdir_choices.items())
                index = int(parts[3])
                if index < 0 or index >= len(choices):
                    raise TelegramControlError("Invalid workdir choice.")
                _label, path = choices[index]
                state.workdir = path
                state.phase = "priority"
                return self.render_add_priority_step(token)

            if action == "preset" and len(parts) == 4:
                state.priority = int(parts[3])
                return self.render_add_priority_step(token)

            if action in {"inc", "dec"} and len(parts) == 4:
                amount = int(parts[3])
                if action == "inc":
                    state.priority += amount
                else:
                    state.priority = max(1, state.priority - amount)
                return self.render_add_priority_step(token)

            if action == "prompt":
                if not state.workdir:
                    raise TelegramControlError("Choose a workdir first.")
                state.phase = "prompt"
                self.active_add_wizard_token = token
                return "Send the task prompt as a text message or voice message."

            if action == "create":
                if not state.workdir or not state.prompt:
                    raise TelegramControlError("Wizard is missing workdir or prompt.")
                response = self.add_task_from_values(
                    title=default_title(state.prompt),
                    prompt=state.prompt,
                    workdir=state.workdir,
                    priority=state.priority,
                )
                self.add_wizards.pop(token, None)
                if self.active_add_wizard_token == token:
                    self.active_add_wizard_token = None
                return response

        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "durextask":
            task_id = int(parts[1])
            action = parts[2]
            if action == "details":
                text, keyboard = task_detail(task_id)
                if callback_id and hasattr(self.bridge, "answer_callback_query"):
                    self.bridge.answer_callback_query(str(callback_id), text=f"Task #{task_id}")
                self.next_reply_markup = keyboard
                return text
            if action == "tail":
                text = tail_task_output(task_id=task_id)
                if callback_id and hasattr(self.bridge, "answer_callback_query"):
                    self.bridge.answer_callback_query(str(callback_id), text="Output")
                return text

        return None

    def download_voice_message(self, voice: dict) -> str:
        """
        Download a Telegram voice attachment to a temporary file.

        Args:
            voice:
                Telegram voice attachment object.

        Returns:
            Local audio path.
        """

        duration = self.voice_metadata_int(voice.get("duration"), "duration")
        if duration is not None and duration > self.config.voice_max_duration_seconds:
            raise TelegramControlError(
                f"Voice message exceeds the configured {self.config.voice_max_duration_seconds}-second limit."
            )
        declared_size = self.voice_metadata_int(voice.get("file_size"), "file_size")
        if declared_size is not None and declared_size > self.config.voice_max_file_bytes:
            raise TelegramControlError(
                f"Voice message exceeds the configured {self.config.voice_max_file_bytes}-byte limit."
            )

        file_id = voice.get("file_id")
        if not file_id:
            raise TelegramControlError("Voice message has no file_id.")
        file_info = self.bridge.get_file(str(file_id))
        file_path = file_info.get("file_path")
        if not file_path:
            raise TelegramControlError("Telegram did not return a voice file path.")
        remote_size = self.voice_metadata_int(file_info.get("file_size"), "file_size")
        if remote_size is not None and remote_size > self.config.voice_max_file_bytes:
            raise TelegramControlError(
                f"Voice message exceeds the configured {self.config.voice_max_file_bytes}-byte limit."
            )

        suffix = Path(str(file_path)).suffix or ".ogg"
        with tempfile.NamedTemporaryFile(prefix="durex_voice_", suffix=suffix, delete=False) as temp_file:
            destination = temp_file.name

        try:
            return self.bridge.download_file(
                str(file_path),
                destination,
                max_bytes=self.config.voice_max_file_bytes,
            )
        except BaseException:
            Path(destination).unlink(missing_ok=True)
            raise

    @staticmethod
    def voice_metadata_int(value: object, name: str) -> Optional[int]:
        """Validate an optional non-negative Telegram voice metadata integer."""

        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise TelegramControlError(f"Telegram voice {name} is invalid.") from exc
        if parsed < 0:
            raise TelegramControlError(f"Telegram voice {name} is invalid.")
        return parsed

    def transcribe_voice_command(self, audio_path: str) -> tuple[str, VoiceCommand, Optional[str], list[str]]:
        """
        Transcribe audio and parse it into a voice command.

        In automatic mode Durex probes the configured supported languages
        explicitly instead of relying on Whisper language detection for short
        command phrases.

        Args:
            audio_path:
                Local voice attachment path.

        Returns:
            Tuple of transcript, parsed command, detected language, and
            transcription attempt details.
        """

        languages: list[Optional[str]]
        if self.config.voice_language is None:
            languages = list(self.config.voice_allowed_languages)
        else:
            languages = [self.config.voice_language]

        attempts: list[str] = []
        candidates: list[str] = []
        for language in languages:
            result = self.voice_transcriber.transcribe(audio_path, language=language)
            detected = result.language or language or "unknown"
            transcript = result.text.strip()
            if not transcript:
                attempts.append(f"{language or 'auto'}: empty transcript")
                continue
            candidates.append(transcript)
            try:
                command = parse_voice_command(
                    transcript,
                    workdir_aliases=self.config.voice_workdir_aliases,
                    command_aliases=self.config.voice_command_aliases,
                )
                attempts.append(f"{language or 'auto'}: {transcript} -> {command.action}")
                return transcript, command, detected, attempts
            except VoiceCommandError:
                attempts.append(f"{language or 'auto'}: {transcript} (detected {detected})")

        if self.config.voice_language is None:
            result = self.voice_transcriber.transcribe(audio_path, language=None)
            detected = result.language or "unknown"
            transcript = result.text.strip()
            if transcript:
                candidates.append(transcript)
                try:
                    command = parse_voice_command(
                        transcript,
                        workdir_aliases=self.config.voice_workdir_aliases,
                        command_aliases=self.config.voice_command_aliases,
                    )
                    attempts.append(f"auto: {transcript} -> {command.action}")
                    return transcript, command, detected, attempts
                except VoiceCommandError:
                    attempts.append(f"auto: {transcript} (detected {detected})")
            else:
                attempts.append("auto: empty transcript")

        if attempts:
            raise VoiceCommandNotRecognized(attempts=attempts, candidates=unique_normalized_phrases(candidates))
        raise TelegramControlError("Voice transcription returned empty text.")

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
        try:
            transcript, command, detected_language, attempts = self.transcribe_voice_command(audio_path)
        finally:
            Path(audio_path).unlink(missing_ok=True)

        if (
            self.config.voice_language is None
            and detected_language
            and detected_language not in self.config.voice_allowed_languages
        ):
            allowed = ", ".join(self.config.voice_allowed_languages)
            language_note = f"\nDetected language: {detected_language} outside configured allow list ({allowed})."
        else:
            language_note = ""

        response = self.handle_voice_command(command)
        debug_note = ""
        if self.config.voice_debug:
            debug_note = "\nVoice attempts:\n" + "\n".join(attempts)
        return f"Voice transcript: {transcript}{language_note}{debug_note}\n\n{response}"

    def transcribe_prompt_voice(self, voice: dict) -> str:
        """
        Transcribe a voice message as free-form task prompt text.

        Args:
            voice:
                Telegram voice attachment object.

        Returns:
            Prompt transcript.
        """

        if not self.config.voice_enabled:
            raise TelegramControlError("Voice commands are disabled. Set DUREX_VOICE_ENABLED=1.")
        if self.voice_transcriber is None:
            raise TelegramControlError("Voice transcription is not configured.")
        audio_path = self.download_voice_message(voice)
        language = self.config.voice_language or (self.config.voice_allowed_languages[0] if self.config.voice_allowed_languages else None)
        try:
            result = self.voice_transcriber.transcribe(audio_path, language=language)
        finally:
            Path(audio_path).unlink(missing_ok=True)
        transcript = result.text.strip()
        if not transcript:
            raise TelegramControlError("Voice transcription returned empty text.")
        return transcript

    def capture_add_prompt(self, text: Optional[str], voice: Optional[dict]) -> Optional[str]:
        """
        Capture the next message as the prompt for an active add wizard.

        Args:
            text:
                Message text, if present.
            voice:
                Voice attachment, if present.

        Returns:
            Confirmation text when a prompt was captured, otherwise None.
        """

        token = self.active_add_wizard_token
        if not token:
            return None
        state = self.add_wizards.get(token)
        if state is None or state.phase != "prompt":
            self.active_add_wizard_token = None
            return None

        if text and text.startswith("/"):
            return None

        prompt = text.strip() if text else self.transcribe_prompt_voice(voice or {})
        if not prompt:
            raise TelegramControlError("Prompt cannot be empty.")
        state.prompt = prompt
        state.phase = "confirm"
        self.active_add_wizard_token = None
        return self.render_add_confirm_step(token)

    def handle_update(self, update: dict) -> Optional[str]:
        """
        Process one Telegram update and return the response text, if any.

        Args:
            update:
                Raw Telegram update dictionary.

        Returns:
            Response text for handled authorized messages, otherwise None.
        """

        callback = update.get("callback_query")
        if callback:
            try:
                response = self.handle_learn_callback(callback)
                if response is None:
                    response = self.handle_interactive_callback(callback)
            except (TelegramControlError, ValueError, sqlite3.Error) as exc:
                response = f"Command rejected: {exc}"
            if response:
                reply_markup = self.next_reply_markup
                self.next_reply_markup = None
                self.send(response, reply_markup=reply_markup)
            return response

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
            captured = self.capture_add_prompt(text, voice)
            if captured is not None:
                response = captured
            elif text:
                response = self.handle_text(text)
            else:
                response = self.handle_voice(voice)
        except VoiceCommandNotRecognized as exc:
            response = f"Command rejected: {exc}"
            candidates = exc.candidates
            reply_markup = self.build_voice_learn_keyboard(candidates[0]) if candidates else None
            if candidates:
                response += f"\n\nLearn candidate: {candidates[0]}"
            self.send(response, reply_markup=reply_markup)
            return response
        except (
            TelegramBridgeError,
            TelegramControlError,
            VoiceCommandError,
            VoiceTranscriptionError,
            ValueError,
            sqlite3.Error,
        ) as exc:
            response = f"Command rejected: {exc}"

        reply_markup = self.next_reply_markup
        self.next_reply_markup = None
        self.send(response, reply_markup=reply_markup)
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
                    allowed_updates=["message", "callback_query"],
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
/learn <status|tasks|tail|run|stop> <spoken phrase> - save a voice alias
/add-wizard - add a task with buttons
/config - show runtime toggles

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
