#!/usr/bin/env python3
"""Durable single-worker supervision independent of CLI and Telegram."""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import os
import threading
import time
from typing import Optional
import uuid

from process_control import RunCancellation
from runtime_contracts import (
    RunnerEvent,
    RunnerInteractionEvent,
    RunnerInteractionState,
    RunnerOutputEvent,
    WorkerSnapshot,
)
from task_services import TaskApplicationService, TaskClaim


DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_HEARTBEAT_SECONDS = 10.0

TaskExecutor = Callable[[TaskClaim, RunCancellation, Callable[[RunnerEvent], None]], None]


class DurableWorkerSupervisor:
    """Own one worker loop, its fenced claim, heartbeat, and cancellation."""

    def __init__(
        self,
        tasks: TaskApplicationService,
        execute: TaskExecutor,
        *,
        notify: Callable[[str], None] = lambda _message: None,
        worker_id: Optional[str] = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than lease_seconds")
        self.tasks = tasks
        self.execute = execute
        self.notify = notify
        self.worker_id = worker_id or f"local-{os.getpid()}-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._now = now
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_after_current = False
        self._current_claim: Optional[TaskClaim] = None
        self._cancellation: Optional[RunCancellation] = None
        self._pending_interaction = False
        self._last_output_at: Optional[str] = None
        self._last_error: Optional[str] = None

    def start(
        self,
        *,
        stop_when_empty: bool = True,
        check_interval: float = 60.0,
    ) -> bool:
        """Start one background worker loop unless it is already active."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._last_error = None
            self._stop_after_current = False
            thread = threading.Thread(
                target=self.run,
                kwargs={
                    "stop_when_empty": stop_when_empty,
                    "check_interval": check_interval,
                },
                daemon=True,
                name=f"durex-worker-{self.worker_id}",
            )
            self._thread = thread
            thread.start()
            return True

    def run(self, *, stop_when_empty: bool, check_interval: float) -> None:
        """Run claims synchronously until stopped or the queue is empty."""

        if check_interval < 0:
            raise ValueError("check_interval must not be negative")
        with self._lock:
            if self._running:
                raise RuntimeError("Worker supervisor is already running")
            self._running = True

        try:
            self.tasks.initialize()
            recovered = self.tasks.recover_stale_task_claims()
            if recovered:
                joined = ", ".join(f"#{task_id}" for task_id in recovered)
                self.notify(f"Recovered expired worker claims as failed: {joined}")

            while True:
                if self._consume_stop_after_current():
                    self.notify("Worker stopped before starting another task.")
                    return

                claim = self.tasks.claim_next_task(
                    worker_id=self.worker_id,
                    lease_id=uuid.uuid4().hex,
                    run_id=uuid.uuid4().hex,
                    lease_expires_at=self._lease_expiry(),
                )
                if claim is None:
                    if stop_when_empty:
                        self.notify("No executable tasks found. Worker is idle.")
                        return
                    self.notify(f"No task ready. Checking again in {check_interval:g} seconds.")
                    time.sleep(check_interval)
                    continue

                self._execute_claim(claim)
        except Exception as exc:
            self._set_last_error(str(exc))
            self.notify(f"Worker error: {exc}")
        finally:
            with self._lock:
                self._running = False

    def request_stop_after_current(self) -> None:
        """Stop before another claim is acquired."""

        with self._lock:
            self._stop_after_current = True

    def request_stop_current(self, reason: str) -> bool:
        """Persist and signal cancellation only for the current fenced claim."""

        with self._lock:
            claim = self._current_claim
            cancellation = self._cancellation
        if claim is None or cancellation is None:
            return False
        if not self.tasks.request_task_cancellation(claim, reason):
            return False
        cancellation.request(reason)
        return True

    def snapshot(self) -> WorkerSnapshot:
        """Return a consistent transport-neutral view of this worker."""

        with self._lock:
            claim = self._current_claim
            return WorkerSnapshot(
                running=self._running
                or (self._thread is not None and self._thread.is_alive()),
                current_task_id=claim.task.id if claim is not None else None,
                current_run_id=claim.run_id if claim is not None else None,
                worker_id=self.worker_id,
                started_at=claim.task.started_at if claim is not None else None,
                last_output_at=self._last_output_at,
                pending_interaction=self._pending_interaction,
                stop_after_current=self._stop_after_current,
                last_error=self._last_error,
            )

    def observe_event(self, event: RunnerEvent) -> None:
        """Track output and interaction state for the currently fenced run."""

        with self._lock:
            claim = self._current_claim
            if claim is None or event.run_id != claim.run_id:
                return
            if isinstance(event, RunnerOutputEvent):
                self._last_output_at = self._iso_now()
            elif isinstance(event, RunnerInteractionEvent):
                self._pending_interaction = (
                    event.state == RunnerInteractionState.REQUESTED
                )

    def _execute_claim(self, claim: TaskClaim) -> None:
        cancellation = RunCancellation()
        heartbeat_stop = threading.Event()
        with self._lock:
            self._current_claim = claim
            self._cancellation = cancellation
            self._pending_interaction = False
            self._last_output_at = None
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(claim, cancellation, heartbeat_stop),
            daemon=True,
            name=f"durex-heartbeat-{claim.run_id}",
        )
        heartbeat.start()
        self.notify(f"Starting task #{claim.task.id}: {claim.task.title}")

        try:
            self.execute(claim, cancellation, self.observe_event)
        except Exception as exc:
            self._set_last_error(str(exc))
            self.tasks.finish_task_claim(
                claim,
                "FAILED",
                "Worker execution raised an exception.",
                last_error=str(exc),
            )
            raise
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=self.heartbeat_seconds + 1.0)
            self._finalize_abandoned_claim(claim, cancellation)
            with self._lock:
                self._current_claim = None
                self._cancellation = None
                self._pending_interaction = False

    def _heartbeat_loop(
        self,
        claim: TaskClaim,
        cancellation: RunCancellation,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                renewed = self.tasks.heartbeat_task_claim(
                    claim,
                    self._lease_expiry(),
                )
            except Exception as exc:
                reason = f"Worker lease heartbeat failed: {exc}"
            else:
                if renewed:
                    continue
                reason = "Worker lease ownership was lost."
            self._set_last_error(reason)
            cancellation.request(reason)
            return

    def _finalize_abandoned_claim(
        self,
        claim: TaskClaim,
        cancellation: RunCancellation,
    ) -> None:
        task = self.tasks.task_detail(claim.task.id)
        if (
            task is None
            or task.status != "RUNNING"
            or task.lease_id != claim.lease_id
            or task.lease_epoch != claim.lease_epoch
        ):
            return
        if cancellation.requested:
            reason = cancellation.reason or "Task cancellation was requested."
            self.tasks.finish_task_claim(
                claim,
                "CANCELLED",
                reason,
                last_error=reason,
            )
            return
        reason = "Task executor returned without finalizing its claim."
        self._set_last_error(reason)
        self.tasks.finish_task_claim(
            claim,
            "FAILED",
            reason,
            last_error=reason,
        )

    def _consume_stop_after_current(self) -> bool:
        with self._lock:
            if not self._stop_after_current:
                return False
            self._stop_after_current = False
            return True

    def _lease_expiry(self) -> str:
        return (self._now() + dt.timedelta(seconds=self.lease_seconds)).isoformat()

    def _iso_now(self) -> str:
        return self._now().isoformat()

    def _set_last_error(self, error: str) -> None:
        with self._lock:
            self._last_error = error
