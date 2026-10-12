"""Bulk Processing core: names, conflicts, presets, batches, payloads and job scheduling."""

import sqlite3
import threading
from pathlib import Path

import pytest

from ai_video_studio.captions.model import CaptionSegment
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.store import INTERRUPTED_MESSAGE, JobStore
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.batch import (
    BUILTIN_RENDER_PRESETS,
    Batch,
    BatchError,
    BatchItem,
    BatchStore,
    RenderPreset,
    RenderPresetStore,
    build_payload,
    item_progress,
    output_name,
)
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.naming import (
    NamingError,
    numbered,
    path_key,
    plan_outputs,
    render_name,
    sanitize_filename,
    validate_template,
)
from ai_video_studio.storage.database import MIGRATIONS, Database

META = VideoMetadata(duration=10.0, width=1920, height=1080, fps=25.0, video_codec="h264", has_audio=True,
                     audio_codec="aac")


# -- names ----------------------------------------------------------------------------

def test_templates_and_windows_safe_names():
    assert render_name("{original_name}_{resolution}", {"original_name": "clip", "resolution": "1080x1920"}) == \
        "clip_1080x1920"
    assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_filename("name. . ") == "name"
    assert sanitize_filename("CON") == "_CON" and sanitize_filename("lpt1.final") == "_lpt1.final"
    assert sanitize_filename("tab\there\x01") == "tab_here_"
    assert sanitize_filename("") == "output" and len(sanitize_filename("x" * 400)) == 150
    assert render_name("{original_name}:{preset}", {"original_name": "a", "preset": "b/c"}) == "a_b_c"
    for bad in ("", "fixed name", "{original_name}_{nope}", "{original_name"):
        with pytest.raises(NamingError):
            validate_template(bad)


def test_conflicts_never_touch_sources_or_busy_files(tmp_path):
    existing = tmp_path / "a_vertical.mp4"
    existing.write_bytes(b"x")
    source = tmp_path / "b.mp4"
    planned = [("1", existing), ("2", tmp_path / "dup.mp4"), ("3", tmp_path / "dup.mp4"), ("4", source),
               ("5", tmp_path / "busy.mp4")]
    busy = {path_key(tmp_path / "busy.mp4")}
    rename = plan_outputs(planned, "rename", {path_key(source)}, busy)
    assert [d.path.name for d in rename] == ["a_vertical (2).mp4", "dup.mp4", "dup (2).mp4", "b (2).mp4", "busy (2).mp4"]
    assert [d.action for d in rename] == ["rename", "write", "rename", "rename", "rename"]
    skip = plan_outputs(planned, "skip", {path_key(source)}, busy)
    assert skip[0].path is None and skip[0].action == "skip" and "already exists" in skip[0].reason
    replace = plan_outputs(planned, "replace", {path_key(source)}, busy)
    assert replace[0].path == existing and replace[0].action == "replace"
    assert replace[3].path.name == "b (2).mp4"  # the source is never replaced, even with "replace"
    assert numbered(Path("x/y.mp4"), 3).name == "y (3).mp4"
    with pytest.raises(NamingError):
        plan_outputs(planned, "overwrite-all", set(), set())


# -- presets ------------------------------------------------------------------------------

def test_builtin_presets_are_valid_and_protected(db):
    store = RenderPresetStore(db)
    for name, preset in BUILTIN_RENDER_PRESETS.items():
        preset.validate()
        assert store.get(name).to_dict() == preset.to_dict()
    assert {"Vertical Reels 1080x1920", "YouTube 1920x1080", "Square 1080x1080",
            "Vertical Reels with captions"} <= set(BUILTIN_RENDER_PRESETS)
    assert BUILTIN_RENDER_PRESETS["Vertical Reels with captions"].resize.burn_captions
    for action in (lambda: store.delete("YouTube 1920x1080"), lambda: store.rename("YouTube 1920x1080", "x"),
                   lambda: store.save("YouTube 1920x1080", RenderPreset())):
        with pytest.raises(BatchError):
            action()


def test_user_presets_create_edit_rename_delete_and_persist(tmp_path):
    db = Database(tmp_path / "p.db")
    store = RenderPresetStore(db)
    preset = RenderPreset(resize=ResizeSettings(preset="720x1280", mode="fill"))
    store.save("Mine", preset)
    preset.output.codec = "h265"
    store.save("Mine", preset)  # edit
    assert store.get("Mine").output.codec == "h265"
    store.save("Other", RenderPreset())
    with pytest.raises(BatchError):
        store.rename("Mine", "Other")
    store.rename("Mine", "Renamed")
    db.close()
    again = RenderPresetStore(Database(tmp_path / "p.db"))
    names = [n for n, builtin in again.names() if not builtin]
    assert names == ["Other", "Renamed"] and again.get("Renamed").resize.preset == "720x1280"
    again.delete("Other")
    assert not again.exists("Other")


def test_preset_validation():
    with pytest.raises(BatchError):
        RenderPreset(operation="reframe", tracking_mode="manual").validate()
    with pytest.raises(BatchError):
        RenderPreset(operation="reframe", resize=ResizeSettings(preset="original")).validate()
    with pytest.raises(BatchError):
        RenderPreset(resize=ResizeSettings(mode="crop")).validate()
    with pytest.raises(NamingError):
        RenderPreset(filename_template="same").validate()
    assert RenderPreset.from_dict(RenderPreset().to_dict()).to_dict() == RenderPreset().to_dict()


# -- batches ----------------------------------------------------------------------------

def test_batch_add_remove_overrides_and_store(db, tmp_path):
    batch = Batch()
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    added, dups = batch.add([str(a), str(b), str(a)])
    assert len(added) == 2 and dups == [str(a)]
    assert batch.add([str(tmp_path / "." / "a.mp4")])[1]  # same file through another spelling
    item = batch.items[0]
    custom = RenderPreset(resize=ResizeSettings(preset="1080x1080"))
    item.override = custom
    assert batch.effective(item) is custom and batch.effective(batch.items[1]) is batch.shared
    store = BatchStore(db)
    store.save(batch)
    again = store.latest()
    assert [i.path for i in again.items] == [str(a.resolve()), str(b.resolve())]
    assert again.items[0].override.resize.preset == "1080x1080" and again.items[1].override is None
    assert batch.remove({item.id}) == 1 and len(batch.items) == 1


def test_payload_for_resize_and_reframe():
    item = BatchItem("/videos/clip.mp4", metadata=META)
    preset = RenderPreset(resize=ResizeSettings(preset="1080x1920", mode="fit", burn_captions=True))
    assert output_name(preset, item, 3, "Reels") == "clip_1080x1920.mp4"
    payload, notes = build_payload(item, preset, Path("/out/clip.mp4"), overwrite=False, batch_id="b",
                                   captions=[CaptionSegment(0, 1, "Hi")], caption_style=None,
                                   tracking_defaults=ReframeSettings(), models_dir="/m")
    assert payload["stage"]["type"] == "layout" and payload["stage"]["layout"]["out_h"] == 1920
    captions = payload["captions"]  # timed inside the job, after the cuts are known
    assert captions["height"] == 1920 and captions["segments"][0]["text"] == "Hi"
    assert payload["batch_item"] == item.id and not notes and payload["effects"] is None
    _payload, notes = build_payload(item, preset, Path("/out/clip.mp4"), overwrite=False, batch_id="b",
                                    captions=None, caption_style=None, tracking_defaults=ReframeSettings(),
                                    models_dir="/m")
    assert "No captions found" in notes[0]
    reframe = RenderPreset(operation="reframe", tracking_mode="active_speaker")
    payload, _ = build_payload(item, reframe, Path("/out/r.mp4"), overwrite=True, batch_id="b", captions=None,
                               caption_style=None, tracking_defaults=ReframeSettings(), models_dir="/m")
    auto = payload["reframe_auto"]
    assert "stage" not in payload and auto["settings"]["tracking_mode"] == "active_speaker"
    assert auto["settings"]["preset"] == "9:16" and "speech" in auto["analysis"]["features"]
    silent = BatchItem("/videos/silent.mp4", metadata=VideoMetadata(10, 1920, 1080, 25, "h264", has_audio=False))
    with pytest.raises(BatchError):
        build_payload(silent, reframe, Path("/out/s.mp4"), overwrite=False, batch_id="b", captions=None,
                      caption_style=None, tracking_defaults=ReframeSettings(), models_dir="/m")


def test_batch_progress():
    assert item_progress([]) == 0.0
    assert item_progress([("completed", 1), ("rendering", 0.5), ("waiting", 0), ("failed", 0)]) == 0.625


# -- scheduling --------------------------------------------------------------------------

class Gate:
    def __init__(self):
        self.release = threading.Event()
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()

    def handler(self, job, ctx):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        ctx.set_stage("rendering")
        self.release.wait(10)
        with self.lock:
            self.running -= 1
        return {}


def test_heavy_jobs_run_one_at_a_time_without_blocking_light_jobs(db):
    gate = Gate()
    manager = JobManager(JobStore(db), max_concurrent=3, max_heavy=1)
    manager.register("render", gate.handler, heavy=True)
    light_done = threading.Event()
    manager.register("probe", lambda job, ctx: light_done.set() or {})
    renders = [manager.submit("render", f"r{i}") for i in range(3)]
    probe = manager.submit("probe", "p")
    assert light_done.wait(5)  # the light job is not stuck behind the waiting renders
    running = [j for j in renders if manager.store.get(j.id).state is JobState.RUNNING]
    assert len(running) == 1 and manager.store.get(running[0].id).stage == "rendering"
    gate.release.set()
    assert manager.wait_idle(10)
    assert gate.peak == 1 and manager.store.get(probe.id).state is JobState.COMPLETED
    manager.shutdown()


def test_hold_and_release(db):
    manager = JobManager(JobStore(db), 1)
    manager.register("x", lambda job, ctx: {})
    manager.pause()
    job = manager.submit("x", "x")
    assert manager.hold(job.id).state is JobState.HELD
    manager.resume()
    assert manager.wait_idle(5) and manager.store.get(job.id).state is JobState.HELD  # not started while held
    assert manager.store.counts()[JobState.HELD] == 1
    manager.release(job.id)
    assert manager.wait_idle(5) and manager.store.get(job.id).state is JobState.COMPLETED
    manager.pause()
    held = manager.hold(manager.submit("x", "y").id)
    assert manager.cancel(held.id).state is JobState.CANCELLED  # a held job can be cancelled
    manager.shutdown()


def test_interrupted_render_is_failed_and_retryable(db):
    store = JobStore(db)
    job = store.create("render_video", "r", {"output": "o.mp4"})
    store.transition(job.id, JobState.RUNNING)
    store.set_stage(job.id, "rendering")
    [recovered] = store.recover_interrupted()
    assert recovered.state is JobState.FAILED and recovered.error == INTERRUPTED_MESSAGE
    retried = store.transition(job.id, JobState.QUEUED)
    assert retried.stage is None and retried.error is None


def test_migration_from_version_2_keeps_jobs(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0] + MIGRATIONS[1] + "PRAGMA user_version = 2;")
    conn.execute("INSERT INTO jobs(id, kind, title, state, created_at) VALUES('j', 'probe_media', 't', 'completed', 'x')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version == len(MIGRATIONS) >= 3
    assert JobStore(db).get("j").stage is None
    db.close()
