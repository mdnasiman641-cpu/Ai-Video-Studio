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
