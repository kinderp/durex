#!/usr/bin/env python3
"""Owner-scoped cancellation and process-group termination primitives."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Callable, Optional


class RunCancellation:
    """Thread-safe cancellation request bound to at most one active process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._requested = False
        self._reason: Optional[str] = None
        self._terminator: Optional[Callable[[], None]] = None

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def request(self, reason: str) -> bool:
        """Record the first request and invoke the bound terminator once."""

        if not reason.strip():
            raise ValueError("Cancellation reason must not be empty")
        with self._lock:
            if self._requested:
                return False
            self._requested = True
            self._reason = reason
            terminator = self._terminator
            self._event.set()
        if terminator is not None:
            terminator()
        return True

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait until cancellation is requested."""

        return self._event.wait(timeout)

    def bind_terminator(self, terminator: Callable[[], None]) -> None:
        """Bind the active runner process and honor an earlier request."""

        with self._lock:
            if self._terminator is not None:
                raise RuntimeError("Cancellation already owns an active process")
            self._terminator = terminator
            requested = self._requested
        if requested:
            terminator()

    def unbind_terminator(self, terminator: Callable[[], None]) -> None:
        """Release the process binding without clearing cancellation history."""

        with self._lock:
            if self._terminator is terminator:
                self._terminator = None


def terminate_process_group(
    process: subprocess.Popen,
    timeout_seconds: float = 5.0,
) -> int:
    """Terminate only the session led by ``process``, then force it if needed."""

    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must not be negative")
    if process.poll() is not None:
        return int(process.returncode or 0)

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return int(process.wait())

    try:
        return int(process.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return int(process.wait())
