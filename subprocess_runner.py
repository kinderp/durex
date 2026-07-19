#!/usr/bin/env python3
"""Incremental subprocess runner with ordered runtime event emission."""

from __future__ import annotations

import codecs
import os
import selectors
import subprocess
from typing import Optional, Sequence

from process_control import RunCancellation, terminate_process_group
from runner_events import RunnerEventEmitter
from runtime_contracts import RunnerEventSink, RunnerLifecycle, RunnerResult


def _stop_process(process: subprocess.Popen, timeout_seconds: float = 5.0) -> None:
    """Stop a subprocess after an event consumer aborts the run."""

    terminate_process_group(process, timeout_seconds=timeout_seconds)


def run_subprocess_command(
    cmd: Sequence[str],
    *,
    task_id: int,
    cwd: Optional[str] = None,
    event_sink: Optional[RunnerEventSink] = None,
    run_id: Optional[str] = None,
    cancellation: Optional[RunCancellation] = None,
) -> RunnerResult:
    """Run a command while streaming stdout and stderr as ordered events."""

    process = subprocess.Popen(
        list(cmd),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        _stop_process(process)
        raise RuntimeError("Subprocess output pipes were not created")

    emitter = RunnerEventEmitter(task_id=task_id, sink=event_sink, run_id=run_id)
    streams = {
        process.stdout: {
            "decoder": codecs.getincrementaldecoder("utf-8")(errors="replace"),
            "parts": [],
        },
        process.stderr: {
            "decoder": codecs.getincrementaldecoder("utf-8")(errors="replace"),
            "parts": [],
        },
    }
    selector = selectors.DefaultSelector()
    cancelled = False

    def cancel_process() -> None:
        nonlocal cancelled
        if process.poll() is None:
            cancelled = True
            terminate_process_group(process)

    if cancellation is not None:
        cancellation.bind_terminator(cancel_process)

    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        emitter.lifecycle(RunnerLifecycle.STARTED)

        while selector.get_map():
            for key, _mask in selector.select():
                stream = key.fileobj
                data = os.read(stream.fileno(), 4096)
                state = streams[stream]
                decoder = state["decoder"]
                parts = state["parts"]
                if data:
                    chunk = decoder.decode(data)
                    if chunk:
                        parts.append(chunk)
                        emitter.output(chunk)
                    continue

                tail = decoder.decode(b"", final=True)
                if tail:
                    parts.append(tail)
                    emitter.output(tail)
                selector.unregister(stream)

        returncode = int(process.wait())
        lifecycle = (
            RunnerLifecycle.CANCELLED
            if cancelled
            else RunnerLifecycle.COMPLETED if returncode == 0 else RunnerLifecycle.FAILED
        )
        emitter.lifecycle(
            lifecycle,
            returncode=returncode,
            detail=cancellation.reason if cancelled and cancellation is not None else None,
        )
        stdout = "".join(streams[process.stdout]["parts"])
        stderr = "".join(streams[process.stderr]["parts"])
        return RunnerResult(
            returncode=returncode,
            output=stdout + "\n" + stderr,
            lifecycle=lifecycle,
        )
    except BaseException:
        _stop_process(process)
        raise
    finally:
        if cancellation is not None:
            cancellation.unbind_terminator(cancel_process)
        selector.close()
        process.stdout.close()
        process.stderr.close()
