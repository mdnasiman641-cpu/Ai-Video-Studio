"""Background job execution.

The manager is independent of Qt: handlers run on worker threads and listeners
are called from those threads. The UI layer (``ui/job_bridge.py``) forwards
listener calls to the GUI thread through queued Qt signals.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Protocol

from ai_video_studio.core.errors import InvalidStateTransition, JobCancelled, StudioError
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.jobs.store import JobStore

log = logging.getLogger(__name__)

MAX_WORKER_THREADS = 8


class JobContext:
    def __init__(self, manager: "JobManager", job: Job):
        self._manager = manager
        self.job = job
        self.cancel_event = threading.Event()

    def report_progress(self, fraction: float) -> None:
        self.check_cancelled()
        self._manager._progress(self.job.id, fraction)

    def set_stage(self, stage: str) -> None:
        """Tell the UI what the job is doing now (e.g. "analyzing" before "rendering")."""
        self.check_cancelled()
        self._manager._stage(self.job.id, stage)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled("Cancelled by user")

    def set_detail(self, **info) -> None:
        """Live information for the UI while the job runs (e.g. ``encoder``, ``speed``, ``step``).

        Kept in memory only: it describes the running job and is dropped when it finishes."""
        self._manager._detail(self.job.id, info)


class JobHandler(Protocol):
    def __call__(self, job: Job, ctx: JobContext) -> dict | None: ...


class JobManager:
    def __init__(self, store: JobStore, max_concurrent: int = 2, max_heavy: int = 1):
        self.store = store
        self._handlers: dict[str, JobHandler] = {}
        self._heavy_kinds: set[str] = set()
        self._max_heavy = max(1, int(max_heavy))
        self._listeners: list[Callable[[Job], None]] = []
        self._running: dict[str, JobContext] = {}
        self._details: dict[str, dict] = {}  # live details of running jobs (JobContext.set_detail)
        self._lock = threading.RLock()
        self._max_concurrent = max(1, int(max_concurrent))
        self._paused = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS, thread_name_prefix="avs-job")
        self._idle = threading.Condition(self._lock)

    # -- configuration -------------------------------------------------------
    def register(self, kind: str, handler: JobHandler, heavy: bool = False) -> None:
        """``heavy`` jobs (video renders) are limited separately by ``max_heavy``."""
        self._handlers[kind] = handler
        if heavy:
            self._heavy_kinds.add(kind)
        else:
            self._heavy_kinds.discard(kind)

    def add_listener(self, listener: Callable[[Job], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Job], None]) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def set_max_concurrent(self, value: int) -> None:
        with self._lock:
            self._max_concurrent = min(max(1, int(value)), MAX_WORKER_THREADS)
        self.dispatch()

    @property
    def max_heavy(self) -> int:
        return self._max_heavy

    def set_max_heavy(self, value: int) -> None:
        with self._lock:
            self._max_heavy = min(max(1, int(value)), MAX_WORKER_THREADS)
        self.dispatch()

    def is_heavy(self, kind: str) -> bool:
        return kind in self._heavy_kinds

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop starting new jobs. Running jobs finish normally."""
        with self._lock:
            self._paused = True
        log.info("Job queue paused")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        log.info("Job queue resumed")
        self.dispatch()

    # -- public operations ---------------------------------------------------
    def recover(self) -> list[Job]:
        jobs = self.store.recover_interrupted()
        for job in jobs:
            log.warning("Recovered interrupted job", extra={"job_id": job.id, "kind": job.kind})
            self._notify(job)
        return jobs

    def submit(self, kind: str, title: str, payload: dict | None = None, project_id: str | None = None) -> Job:
        if kind not in self._handlers:
            raise StudioError(f"No handler registered for job type '{kind}'")
        job = self.store.create(kind, title, payload, project_id)
        log.info("Job queued", extra={"job_id": job.id, "kind": kind})
        self._notify(job)
        self.dispatch()
        return job

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            ctx = self._running.get(job_id)
            if ctx is not None:
                ctx.cancel_event.set()  # the worker moves it to CANCELLED when the handler stops
                return self.store.get(job_id)
            job = self.store.get(job_id)
            if job.state not in (JobState.QUEUED, JobState.HELD):
                raise InvalidStateTransition(f"A {job.state.value} job cannot be cancelled")
            job = self.store.transition(job_id, JobState.CANCELLED, error="Cancelled by user")
        self._notify(job)
        return job

    def hold(self, job_id: str) -> Job:
        """Keep a queued job from starting (pause it individually). Running jobs are not affected."""
        with self._lock:
            job = self.store.transition(job_id, JobState.HELD)
        self._notify(job)
        return job

    def release(self, job_id: str) -> Job:
        with self._lock:
            job = self.store.transition(job_id, JobState.QUEUED)
        self._notify(job)
        self.dispatch()
        return job

    def retry(self, job_id: str) -> Job:
        job = self.store.transition(job_id, JobState.QUEUED)
        log.info("Job re-queued", extra={"job_id": job_id})
        self._notify(job)
        self.dispatch()
        return job

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def dispatch(self) -> None:
        with self._lock:
            if self._paused or self._closed:
                return
            free = self._max_concurrent - len(self._running)
            if free <= 0:
                return
            heavy_free = self._max_heavy - sum(1 for c in self._running.values() if c.job.kind in self._heavy_kinds)
            started = 0
            # Look further than the free slots: a waiting heavy job must not block light ones behind it.
            for job in self.store.next_queued(free + 50):
                if started >= free:
                    break
                if job.kind in self._heavy_kinds:
                    if heavy_free <= 0:
                        continue
                    heavy_free -= 1
                started += 1
                handler = self._handlers.get(job.kind)
                job = self.store.transition(job.id, JobState.RUNNING)
                if handler is None:
                    job = self.store.transition(job.id, JobState.FAILED, error=f"Unknown job type '{job.kind}'")
                    self._notify(job)
                    continue
                ctx = JobContext(self, job)
                self._running[job.id] = ctx
                self._notify(job)
                self._executor.submit(self._run, handler, ctx)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until nothing is running or queued (used by tests and shutdown)."""
        with self._idle:
            return self._idle.wait_for(
                lambda: not self._running and (self._paused or not self.store.next_queued(1)),
                timeout=timeout,
            )

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Cancel running jobs and stop the worker pool. Queued jobs stay queued for next start.

        Returns False if some job did not stop within ``timeout`` (e.g. a model download);
        such jobs stay RUNNING in the database and are reported as interrupted on next start.
        """
        with self._lock:
            self._closed = True
            for ctx in self._running.values():
                ctx.cancel_event.set()
        with self._idle:
            stopped = self._idle.wait_for(lambda: not self._running, timeout=timeout)
        self._executor.shutdown(wait=False, cancel_futures=True)
        return stopped

    # -- internals -----------------------------------------------------------
    def _run(self, handler: JobHandler, ctx: JobContext) -> None:
        job = ctx.job
        try:
            ctx.check_cancelled()
            result = handler(job, ctx)
            ctx.check_cancelled()
            final = self.store.transition(job.id, JobState.COMPLETED, result=result or {})
            log.info("Job completed", extra={"job_id": job.id, "kind": job.kind})
        except JobCancelled:
            final = self.store.transition(job.id, JobState.CANCELLED, error="Cancelled by user")
            log.info("Job cancelled", extra={"job_id": job.id})
        except StudioError as exc:
            final = self.store.transition(job.id, JobState.FAILED, error=str(exc))
            log.warning("Job failed", extra={"job_id": job.id, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a crashing handler must not kill the queue
            final = self.store.transition(job.id, JobState.FAILED, error=f"Unexpected error: {exc}")
            log.exception("Job crashed", extra={"job_id": job.id})
        with self._lock:
            self._running.pop(job.id, None)
            self._details.pop(job.id, None)
        self._notify(final)
        self.dispatch()
        with self._idle:
            self._idle.notify_all()

    def detail(self, job_id: str) -> dict:
        """The running job's live details (see ``JobContext.set_detail``)."""
        with self._lock:
            return dict(self._details.get(job_id, {}))

    def _detail(self, job_id: str, info: dict) -> None:
        with self._lock:
            self._details.setdefault(job_id, {}).update(info)
        self._notify(self.store.get(job_id))

    def _stage(self, job_id: str, stage: str) -> None:
        self.store.set_stage(job_id, stage)
        self._notify(self.store.get(job_id))

    def _progress(self, job_id: str, fraction: float) -> None:
        self.store.set_progress(job_id, fraction)
        self._notify(self.store.get(job_id))

    def _notify(self, job: Job) -> None:
        for listener in list(self._listeners):
            try:
                listener(job)
            except Exception:  # noqa: BLE001
                log.exception("Job listener failed")
