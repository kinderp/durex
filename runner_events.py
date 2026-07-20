#!/usr/bin/env python3
"""Ordered event emission helpers shared by Durex runner adapters."""

from __future__ import annotations

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
from task_services import TaskApplicationService, TaskClaim


class TerminalDisplayNormalizer:
    """Strip terminal controls incrementally across output chunk boundaries."""

    def __init__(self) -> None:
        self._state = "text"
        self._last_was_carriage_return = False

    def feed(self, text: str) -> str:
        visible: list[str] = []
        for char in text:
            if self._state == "text":
                if char == "\n" and self._last_was_carriage_return:
                    self._last_was_carriage_return = False
                    continue
                self._last_was_carriage_return = False
                if char == "\x1b":
                    self._state = "escape"
                elif char == "\r":
                    visible.append("\n")
                    self._last_was_carriage_return = True
                elif char in {"\n", "\t"} or (ord(char) >= 32 and char != "\x7f"):
                    visible.append(char)
                continue

            if self._state == "escape":
                if char == "[":
                    self._state = "csi"
                elif char == "]":
                    self._state = "osc"
                else:
                    self._state = "text"
                continue

            if self._state == "csi":
                if "@" <= char <= "~":
                    self._state = "text"
                continue

            if self._state == "osc":
                if char == "\x07":
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "osc_escape"
                continue

            if self._state == "osc_escape":
                self._state = "text" if char == "\\" else "osc"

        return redact_for_display("".join(visible)) or ""


def normalize_live_output(text: str) -> str:
    """Normalize terminal controls and redact obvious secrets for display."""

    return TerminalDisplayNormalizer().feed(text)


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
        observer: Optional[RunnerEventSink] = None,
        claim: Optional[TaskClaim] = None,
    ) -> None:
        self.tasks = tasks
        self.task_id = task_id
        self.run_id = run_id
        self.attempt = attempt
        self.last_sequence = 0
        self.started = False
        self.finished = False
        self.observer = observer
        self.claim = claim
        self._normalizer = TerminalDisplayNormalizer()

    def __call__(self, event: RunnerEvent) -> None:
        if event.task_id != self.task_id or event.run_id != self.run_id:
            raise ValueError("Runner event does not match the persistent sink identity")
        if event.sequence <= self.last_sequence:
            raise ValueError("Runner event sequence is not monotonic")

        if isinstance(event, RunnerLifecycleEvent):
            if event.state == RunnerLifecycle.STARTED:
                self.tasks.start_task_run(
                    self.task_id,
                    self.run_id,
                    self.attempt,
                    claim=self.claim,
                )
                self.started = True
                self.last_sequence = event.sequence
                self._observe(event)
                return
            self.tasks.finish_task_run(
                self.task_id,
                self.run_id,
                event.sequence,
                event.state.value,
                event.returncode,
                claim=self.claim,
            )
            self.finished = True
            self.last_sequence = event.sequence
            self._observe(event)
            return

        if isinstance(event, RunnerOutputEvent):
            text = self._normalizer.feed(event.text)
            if text:
                self.tasks.append_live_output(
                    self.task_id,
                    self.run_id,
                    event.sequence,
                    text,
                    claim=self.claim,
                )
            self.last_sequence = event.sequence
            self._observe(event)
            return

        self.last_sequence = event.sequence
        self._observe(event)

    def _observe(self, event: RunnerEvent) -> None:
        if self.observer is not None:
            self.observer(event)

    def fail_open_run(self, returncode: Optional[int] = None) -> bool:
        """Conservatively finalize a started run after runner-side failure."""

        if not self.started or self.finished:
            return False
        changed = self.tasks.finish_task_run(
            self.task_id,
            self.run_id,
            self.last_sequence + 1,
            RunnerLifecycle.FAILED.value,
            returncode,
            claim=self.claim,
        )
        self.finished = True
        self.last_sequence += 1
        return changed
