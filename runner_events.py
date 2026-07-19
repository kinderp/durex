#!/usr/bin/env python3
"""Ordered event emission helpers shared by Durex runner adapters."""

from __future__ import annotations

import re
from typing import Optional
import uuid

from approval_detector import redact_for_display
from runtime_contracts import (
    RunnerEvent,
    RunnerEventSink,
    RunnerInteractionEvent,
    RunnerInteractionState,
    RunnerLifecycle,
    RunnerLifecycleEvent,
    RunnerOutputEvent,
)
from task_services import TaskApplicationService


ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_SINGLE_RE = re.compile(r"\x1b[@-_]")
DISPLAY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_live_output(text: str) -> str:
    """Normalize terminal controls and redact obvious secrets for display."""

    normalized = ANSI_OSC_RE.sub("", text)
    normalized = ANSI_CSI_RE.sub("", normalized)
    normalized = ANSI_SINGLE_RE.sub("", normalized)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = DISPLAY_CONTROL_RE.sub("", normalized)
    return redact_for_display(normalized) or ""


class RunnerEventEmitter:
    """Emit one monotonic event stream for a single task execution."""

    def __init__(
        self,
        task_id: int,
        sink: Optional[RunnerEventSink] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id or uuid.uuid4().hex
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        self.sink = sink
        self._sequence = 0

    def _emit(self, event: RunnerEvent) -> RunnerEvent:
        if self.sink is not None:
            self.sink(event)
        return event

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def lifecycle(
        self,
        state: RunnerLifecycle,
        returncode: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> RunnerLifecycleEvent:
        """Emit one lifecycle transition."""

        return self._emit(
            RunnerLifecycleEvent(
                task_id=self.task_id,
                state=state,
                returncode=returncode,
                detail=detail,
                run_id=self.run_id,
                sequence=self._next_sequence(),
            )
        )

    def output(self, text: str) -> Optional[RunnerOutputEvent]:
        """Emit one non-empty decoded output chunk."""

        if not text:
            return None
        return self._emit(
            RunnerOutputEvent(
                task_id=self.task_id,
                sequence=self._next_sequence(),
                text=text,
                run_id=self.run_id,
            )
        )

    def interaction(
        self,
        *,
        interaction_id: str,
        state: RunnerInteractionState,
        kind: str,
        prompt: str,
        command: Optional[str] = None,
        decision: Optional[str] = None,
        source: Optional[str] = None,
    ) -> RunnerInteractionEvent:
        """Emit an interaction request or its matching resolution."""

        return self._emit(
            RunnerInteractionEvent(
                task_id=self.task_id,
                interaction_id=interaction_id,
                kind=kind,
                prompt=prompt,
                command=command,
                run_id=self.run_id,
                sequence=self._next_sequence(),
                state=state,
                decision=decision,
                source=source,
            )
        )


class PersistentRunnerEventSink:
    """Project runner lifecycle and normalized output into SQLite services."""

    def __init__(
        self,
        tasks: TaskApplicationService,
        task_id: int,
        run_id: str,
        attempt: int,
    ) -> None:
        self.tasks = tasks
        self.task_id = task_id
        self.run_id = run_id
        self.attempt = attempt

    def __call__(self, event: RunnerEvent) -> None:
        if event.task_id != self.task_id or event.run_id != self.run_id:
            raise ValueError("Runner event does not match the persistent sink identity")

        if isinstance(event, RunnerLifecycleEvent):
            if event.state == RunnerLifecycle.STARTED:
                self.tasks.start_task_run(self.task_id, self.run_id, self.attempt)
                return
            self.tasks.finish_task_run(
                self.task_id,
                self.run_id,
                event.sequence,
                event.state.value,
                event.returncode,
            )
            return

        if isinstance(event, RunnerOutputEvent):
            text = normalize_live_output(event.text)
            if text:
                self.tasks.append_live_output(
                    self.task_id,
                    self.run_id,
                    event.sequence,
                    text,
                )
