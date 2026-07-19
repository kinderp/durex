#!/usr/bin/env python3
"""Transport-neutral runtime contracts for incremental bridge refactoring."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Union

from task_services import TaskRecord


class RunnerLifecycle(str, Enum):
    """Lifecycle states emitted by a task runner."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunnerLifecycleEvent:
    """A transport-neutral runner lifecycle event."""

    task_id: int
    state: RunnerLifecycle
    returncode: Optional[int] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class RunnerOutputEvent:
    """One ordered output chunk emitted by a runner."""

    task_id: int
    sequence: int
    text: str


@dataclass(frozen=True)
class RunnerInteractionEvent:
    """One interaction request without transport-specific payloads."""

    task_id: int
    interaction_id: str
    kind: str
    prompt: str
    command: Optional[str] = None


RunnerEvent = Union[RunnerLifecycleEvent, RunnerOutputEvent, RunnerInteractionEvent]
RunnerEventSink = Callable[[RunnerEvent], None]


@dataclass(frozen=True)
class RunnerResult:
    """Normalized final result returned by a task runner implementation."""

    returncode: int
    output: str


class TaskRunner(Protocol):
    """Execution backend contract independent of Telegram and SQLite."""

    def run(self, task: TaskRecord, emit: RunnerEventSink) -> RunnerResult:
        """Run one task and emit ordered runtime events."""


@dataclass(frozen=True)
class WorkerSnapshot:
    """Observable state exposed by a worker supervisor."""

    running: bool
    current_task_id: Optional[int] = None
    last_error: Optional[str] = None


class WorkerSupervisor(Protocol):
    """Lifecycle boundary used by command adapters."""

    def start(self) -> bool:
        """Start work and return whether a new worker was started."""

    def request_stop(self) -> None:
        """Request a cooperative stop."""

    def snapshot(self) -> WorkerSnapshot:
        """Return the current observable worker state."""


class TelegramTransportConfig(Protocol):
    """Authorization settings exposed by a Telegram transport."""

    allowed_chat_id: int


class TelegramTransport(Protocol):
    """Telegram operations required by the control adapter."""

    config: TelegramTransportConfig

    def poll_updates(
        self,
        timeout: int = 20,
        allowed_updates: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Fetch Telegram updates."""

    def send_message(self, text: str, reply_markup: Optional[dict] = None) -> int:
        """Send one message and return its Telegram identifier."""

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> None:
        """Acknowledge one callback query."""

    def get_file(self, file_id: str) -> dict[str, Any]:
        """Return Telegram metadata for one file."""

    def download_file(
        self,
        file_path: str,
        destination: str | Path,
        max_bytes: Optional[int] = None,
    ) -> str:
        """Download a Telegram file to a bounded local destination."""
