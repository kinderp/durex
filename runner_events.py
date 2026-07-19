#!/usr/bin/env python3
"""Ordered event emission helpers shared by Durex runner adapters."""

from __future__ import annotations

from typing import Optional
import uuid

from runtime_contracts import (
    RunnerEvent,
    RunnerEventSink,
    RunnerInteractionEvent,
    RunnerInteractionState,
    RunnerLifecycle,
    RunnerLifecycleEvent,
    RunnerOutputEvent,
)


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
