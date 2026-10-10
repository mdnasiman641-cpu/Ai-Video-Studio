"""Subprocess helpers that never open console windows on Windows and support cancellation."""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass

from ai_video_studio.core.errors import JobCancelled

CREATE_NO_WINDOW = 0x08000000


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def run_process(
    args: list[str],
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
) -> ProcessResult:
    """Run a command and capture text output.

    If ``cancel_event`` is set while the process runs, the process is killed and
    :class:`JobCancelled` is raised. ``subprocess.TimeoutExpired`` propagates on timeout.
    """
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        **_popen_kwargs(),
    )
    waited = 0.0
    poll = 0.1
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=poll)
            return ProcessResult(proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            waited += poll
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.communicate()
                raise JobCancelled("Cancelled by user")
            if timeout is not None and waited >= timeout:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(args, timeout)


def run_streaming(
    args: list[str],
    on_stdout_line,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
    stderr_tail_lines: int = 40,
) -> ProcessResult:
    """Run a command, calling ``on_stdout_line(line)`` for each stdout line on the caller's thread.

    Used for ``ffmpeg -progress pipe:1``. stderr is drained on a helper thread (so the
    child never blocks on a full pipe) and the last lines are returned for error messages.
    If ``cancel_event`` is set the process is killed and :class:`JobCancelled` is raised.
    Exceptions raised by ``on_stdout_line`` kill the process and propagate.
    """
    import collections
    import queue

    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        **_popen_kwargs(),
    )
    lines: queue.Queue = queue.Queue()
    tail: collections.deque[str] = collections.deque(maxlen=stderr_tail_lines)

    def read_stdout():
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    def read_stderr():
        for line in proc.stderr:
            tail.append(line.rstrip("\n"))

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for t in readers:
        t.start()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Cancelled by user")
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            on_stdout_line(line)
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        for t in readers:
            t.join(timeout=2)
    return ProcessResult(proc.returncode, "", "\n".join(tail))
