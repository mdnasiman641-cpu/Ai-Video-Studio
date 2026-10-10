import threading
import time

import pytest

from ai_video_studio.core.errors import InvalidStateTransition, MediaError
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import ALLOWED_TRANSITIONS, JobState, can_transition
from ai_video_studio.jobs.store import INTERRUPTED_MESSAGE, JobStore


def test_transition_table():
    assert can_transition(JobState.QUEUED, JobState.RUNNING)
    assert can_transition(JobState.FAILED, JobState.QUEUED)
    assert not can_transition(JobState.COMPLETED, JobState.QUEUED)
    assert not can_transition(JobState.QUEUED, JobState.COMPLETED)
    assert ALLOWED_TRANSITIONS[JobState.COMPLETED] == frozenset()


def test_store_rejects_invalid_transition(db):
    store = JobStore(db)
    job = store.create("x", "X")
    with pytest.raises(InvalidStateTransition):
        store.transition(job.id, JobState.COMPLETED)
    job = store.transition(job.id, JobState.RUNNING)
    assert job.attempts == 1 and job.started_at
    job = store.transition(job.id, JobState.COMPLETED, result={"ok": True})
    assert job.result == {"ok": True} and job.progress == 1.0


def test_recover_interrupted(db):
    store = JobStore(db)
    running = store.transition(store.create("x", "A").id, JobState.RUNNING)
    queued = store.create("x", "B")
    recovered = store.recover_interrupted()
    assert [j.id for j in recovered] == [running.id]
    assert store.get(running.id).state is JobState.FAILED
    assert store.get(running.id).error == INTERRUPTED_MESSAGE
    assert store.get(queued.id).state is JobState.QUEUED


def _manager(db, max_concurrent=2):
    manager = JobManager(JobStore(db), max_concurrent)
    return manager


def test_job_runs_and_completes(db):
    manager = _manager(db)
    events = []
    manager.add_listener(lambda j: events.append(j.state))
    manager.register("echo", lambda job, ctx: {"value": job.payload["v"]})
    job = manager.submit("echo", "Echo", {"v": 5})
    assert manager.wait_idle(5)
    job = manager.store.get(job.id)
    assert job.state is JobState.COMPLETED and job.result == {"value": 5}
    assert events[0] is JobState.QUEUED and events[-1] is JobState.COMPLETED
    manager.shutdown()


def test_failure_and_retry(db):
    manager = _manager(db)
    attempts = {"n": 0}

    def flaky(job, ctx):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise MediaError("bad file")
        return {}

    manager.register("flaky", flaky)
    job = manager.submit("flaky", "Flaky")
    manager.wait_idle(5)
    failed = manager.store.get(job.id)
    assert failed.state is JobState.FAILED and failed.error == "bad file"
    manager.retry(job.id)
    manager.wait_idle(5)
    done = manager.store.get(job.id)
    assert done.state is JobState.COMPLETED and done.attempts == 2 and done.error is None
    with pytest.raises(InvalidStateTransition):
        manager.retry(job.id)
    manager.shutdown()


def test_unexpected_exception_is_contained(db):
    manager = _manager(db)
    manager.register("boom", lambda job, ctx: 1 / 0)
    manager.register("ok", lambda job, ctx: {})
    bad = manager.submit("boom", "Boom")
    good = manager.submit("ok", "Ok")
    manager.wait_idle(5)
    assert manager.store.get(bad.id).state is JobState.FAILED
    assert "Unexpected error" in manager.store.get(bad.id).error
    assert manager.store.get(good.id).state is JobState.COMPLETED
    manager.shutdown()


def test_cancel_running_job(db):
    manager = _manager(db)
    started = threading.Event()

    def slow(job, ctx):
        started.set()
        while True:
            ctx.report_progress(0.5)
            time.sleep(0.01)

    manager.register("slow", slow)
    job = manager.submit("slow", "Slow")
    assert started.wait(5)
    manager.cancel(job.id)
    manager.wait_idle(5)
    assert manager.store.get(job.id).state is JobState.CANCELLED
    manager.shutdown()


def test_cancel_queued_job_and_concurrency_limit(db):
    manager = _manager(db, max_concurrent=1)
    release = threading.Event()
    running = []

    def block(job, ctx):
        running.append(job.id)
        release.wait(5)
        return {}

    manager.register("block", block)
    first = manager.submit("block", "1")
    second = manager.submit("block", "2")
    third = manager.submit("block", "3")
    time.sleep(0.2)
    assert manager.running_count() == 1
    assert manager.store.get(second.id).state is JobState.QUEUED
    manager.cancel(third.id)
    assert manager.store.get(third.id).state is JobState.CANCELLED
    release.set()
    manager.wait_idle(5)
    assert running == [first.id, second.id]
    with pytest.raises(InvalidStateTransition):
        manager.cancel(first.id)
    manager.shutdown()


def test_pause_and_resume(db):
    manager = _manager(db)
    manager.register("ok", lambda job, ctx: {})
    manager.pause()
    job = manager.submit("ok", "Ok")
    time.sleep(0.1)
    assert manager.store.get(job.id).state is JobState.QUEUED
    manager.resume()
    manager.wait_idle(5)
    assert manager.store.get(job.id).state is JobState.COMPLETED
    manager.shutdown()


def test_unknown_kind_from_previous_session_fails(db):
    store = JobStore(db)
    orphan = store.create("removed_feature", "Old")
    manager = JobManager(store, 1)
    manager.dispatch()
    assert store.get(orphan.id).state is JobState.FAILED


def test_queued_jobs_survive_restart(db):
    manager = _manager(db)
    manager.register("ok", lambda job, ctx: {})
    manager.pause()
    job = manager.submit("ok", "Ok")
    manager.shutdown()
    restarted = JobManager(JobStore(db), 2)
    restarted.register("ok", lambda job, ctx: {})
    restarted.recover()
    restarted.dispatch()
    restarted.wait_idle(5)
    assert restarted.store.get(job.id).state is JobState.COMPLETED
    restarted.shutdown()


def test_counts_and_newest_first(db):
    store = JobStore(db)
    a = store.create("x", "A")
    b = store.create("x", "B")
    store.transition(a.id, JobState.CANCELLED)
    assert [j.id for j in store.list(newest_first=True)] == [b.id, a.id]
    assert [j.id for j in store.list(limit=1)] == [a.id]
    counts = store.counts()
    assert counts[JobState.QUEUED] == 1 and counts[JobState.CANCELLED] == 1 and counts[JobState.RUNNING] == 0
