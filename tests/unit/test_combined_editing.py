"""Combined editing: speed and audio effects, the one-pass filter graph, caption timing and
per-caption styles, caption templates, and bulk payloads/queue behaviour for 10-15 videos."""

import json
import threading
from pathlib import Path

import pytest

from ai_video_studio.captions.ass import build_ass
from ai_video_studio.captions.model import CaptionSegment, CaptionTrack, Word, output_captions, retime
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import MediaItem
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.batch import (
    Batch,
    BatchItem,
    RenderPreset,
    RenderPresetStore,
    build_payload,
    frame_preview_payload,
    output_name,
)
from ai_video_studio.render.effects import EditEffects, EffectsError, atempo_chain
from ai_video_studio.render.layout import ResizeSettings
from ai_video_studio.render.naming import path_key, plan_outputs
from ai_video_studio.render.output import OutputSettings
from ai_video_studio.render.pipeline import audio_plan, build_filter_graph
from ai_video_studio.smartcut.model import SmartCutSettings
from ai_video_studio.storage.presets import PresetStore

META = VideoMetadata(duration=10.0, width=1920, height=1080, fps=25.0, video_codec="h264", has_audio=True,
                     audio_codec="aac")
SILENT = VideoMetadata(duration=10.0, width=1920, height=1080, fps=25.0, video_codec="h264", has_audio=False)


# -- speed and audio effects -----------------------------------------------------------------

@pytest.mark.parametrize("factor", [0.25, 0.5, 0.75, 1.25, 2.0, 3.0, 4.0])
def test_atempo_chain_stays_in_range_and_multiplies_to_the_factor(factor):
    values = [float(f.split("=")[1]) for f in atempo_chain(factor)]
    assert all(0.5 <= v <= 2.0 for v in values)
    product = 1.0
    for v in values:
        product *= v
    assert abs(product - factor) < 1e-6
    assert atempo_chain(1.0) == []


def test_invalid_effects_are_rejected():
    for bad in ({"video_speed": 0.1}, {"video_speed": 5}, {"audio_speed": 0}, {"volume": 500},
                {"noise_reduction": "max"}, {"fade_in": 11}, {"bass_db": 20}, {"voice": "robot"}):
        with pytest.raises(EffectsError):
            EditEffects(**bad).validate()
        with pytest.raises(EffectsError):
            EditEffects.from_dict(bad, strict=True)
        assert EditEffects.from_dict(bad) == EditEffects()  # stored data never breaks a project
    with pytest.raises(EffectsError):
        EditEffects.from_dict({"volume": "loud"}, strict=True)


def test_disabled_operations_add_no_filters():
    e = EditEffects(speed_enabled=False, video_speed=2.0, audio_enabled=False, volume=200, noise_reduction="strong")
    assert not e.active and e.video_filters() == [] and e.audio_filters(10) == []
    assert e.output_duration(10) == 10


def test_audio_filter_order_and_values():
    e = EditEffects(speed_enabled=True, video_speed=1.25, audio_enabled=True, volume=120, noise_reduction="medium",
                    fade_in=0.5, fade_out=1.0, normalize=True, bass_db=4, treble_db=-2, voice="radio")
    chain = e.audio_filters(8.0)
    names = [f.split("=")[0] for f in chain]
    assert names == ["aresample", "afftdn", "atempo", "highpass", "lowpass", "bass", "treble", "volume", "loudnorm",
                     "aresample", "afade", "afade"]
    assert "atempo=1.25" in chain and "volume=1.2" in chain and "afftdn=nr=12" in chain
    assert chain[-1] == "afade=t=out:st=7.000:d=1"  # fade-out ends with the output
    assert e.video_filters() == ["setpts=PTS/1.25"] and e.output_duration(10) == 8.0
    assert {"afftdn", "loudnorm", "atempo", "setpts"} <= e.required_filters()


def test_pitch_and_unlinked_audio():
    tape = EditEffects(speed_enabled=True, video_speed=1.5, preserve_pitch=False)
    assert tape.audio_filters(4) == ["aresample=48000", "asetrate=72000", "aresample=48000"]
    unlinked = EditEffects(speed_enabled=True, video_speed=2.0, link_audio=False, audio_speed=1.0)
    chain = unlinked.audio_filters(5.0)
    assert unlinked.unlinked and chain[-2:] == ["apad", "atrim=end=5.000000"]  # cut/padded to the video
    assert not any(f.startswith("atempo") for f in chain)
    linked = EditEffects(speed_enabled=True, video_speed=2.0, link_audio=True, audio_speed=0.5)
    assert linked.audio_factor == 2.0 and "apad" not in linked.audio_filters(5.0)
    deeper = EditEffects(audio_enabled=True, voice="deeper").audio_filters(5.0)
    assert deeper[1:4] == ["asetrate=40800", "aresample=48000", "atempo=1.17647"]


def test_effects_round_trip_on_a_media_item():
    e = EditEffects(speed_enabled=True, video_speed=0.5, audio_enabled=True, volume=80, fade_out=2.0)
    item = MediaItem(path="/v/a.mp4", effects=e)
    again = MediaItem.from_dict(json.loads(json.dumps(item.to_dict())))
    assert again.effects == e
    assert MediaItem.from_dict({"path": "/v/b.mp4"}).effects is None  # older projects


def test_graph_runs_every_operation_in_one_pass_in_order():
    e = EditEffects(speed_enabled=True, video_speed=2.0, audio_enabled=True, volume=150)
    graph, video, audio = build_filter_graph(
        keep_ranges=[(0.0, 2.0), (4.0, 6.0)], has_audio=True,
        video_stage=lambda src, dst: f"[{src}]scale=360:640[{dst}]", subtitle_file="subtitles.ass",
        video_filters=e.video_filters(), source_fps=25.0, audio_filters=e.audio_filters(2.0))
    order = [graph.index(s) for s in ("concat=n=2", "scale=360:640", "setpts=PTS/2", "fps=25", "subtitles=")]
    assert order == sorted(order)
    assert video == "vout" and audio == "aout"
    assert "[acut]aresample=48000,atempo=2,volume=1.5[aout]" in graph
    # without cuts the audio filters read the source audio; without effects nothing changes
    graph, _v, audio = build_filter_graph(keep_ranges=None, has_audio=True, audio_filters=["volume=0.5"])
    assert "[0:a:0]volume=0.5[aout]" in graph and audio == "aout"
    graph, _v, audio = build_filter_graph(keep_ranges=None, has_audio=True)
    assert audio is None and "setpts" not in graph and "fps=" not in graph


def test_audio_plan_with_effects_and_without_audio():
    settings = OutputSettings()
    assert audio_plan(settings, True, "aac", False)[0] == "copy"
    mode, notes = audio_plan(settings, True, "aac", False, effects=True)
    assert mode == "filtered" and "speed or audio effects" in notes[0]
    mode, notes = audio_plan(settings, False, None, False, effects=True)
    assert mode == "none" and any("not applied" in n for n in notes)


# -- captions ---------------------------------------------------------------------------------

def test_captions_follow_cuts_then_speed():
    segs = [CaptionSegment(0.5, 1.5, "one"), CaptionSegment(2.5, 3.5, "cut away"),
            CaptionSegment(4.5, 5.5, "three", words=[Word(4.5, 5.0, " thr"), Word(5.0, 5.5, "ee")])]
    out = output_captions(segs, [(0.0, 2.0), (4.0, 6.0)], 2.0)
    assert [(s.text, s.start, s.end) for s in out] == [("one", 0.25, 0.75), ("three", 1.25, 1.75)]
    assert [(w.start, w.end) for w in out[1].words] == [(1.25, 1.5), (1.5, 1.75)]
    assert [(s.start, s.end) for s in retime(segs, 0.5)][0] == (1.0, 3.0)  # slow motion: later and longer
    assert output_captions(segs, None, 1.0)[1].text == "cut away"


def test_caption_own_style_survives_edits_cuts_and_saving():
    style = BUILTIN_PRESETS["Bold Green"].to_dict()
    track = CaptionTrack([CaptionSegment(0, 2, "hello world", style=style), CaptionSegment(2, 3, "plain")])
    a, b = track.split(track.segments[0].id, text_index=5)
    assert a.style == style and b.style == style
    merged = track.merge([a.id, b.id])
    assert merged.style == style
    again = CaptionTrack.from_dict(json.loads(json.dumps(track.to_dict())))
    assert again.segments[0].style == style and again.segments[1].style is None
    assert output_captions(again.segments, [(0.0, 3.0)], 1.5)[0].style == style


def test_ass_uses_each_captions_own_style_and_fades():
    default = CaptionStyle()
    highlight = BUILTIN_PRESETS["Highlight Style"]
    social = BUILTIN_PRESETS["Social Video"]
    segs = [CaptionSegment(0, 1, "a"), CaptionSegment(1, 2, "b", style=highlight.to_dict()),
            CaptionSegment(2, 3, "c", style=social.to_dict()), CaptionSegment(3, 4, "d", style=highlight.to_dict())]
    ass = build_ass(segs, default, 1080, 1920)
    assert "Style: Default," in ass and "Style: S1," in ass and "Style: S1Box," in ass and "Style: S2," in ass
    assert ass.count("Style: S") == 3  # one style per distinct caption style (S1, S1Box, S2)
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert lines[0].endswith(",Default,,0,0,0,,a")
    assert ",S1Box,," in lines[1] and ",S1,,0,0,0,,b" in lines[2]
    assert lines[3].endswith(r"{\fad(150,150)}c")  # Social Video fades in and out
    # the video's default style keeps the old style names, so existing output is unchanged
    boxed = build_ass([CaptionSegment(0, 1, "x")], BUILTIN_PRESETS["Boxed"], 640, 360)
    assert "Style: Box," in boxed and ",Box,,0,0,0,,x" in boxed and r"\fad" not in boxed


def test_builtin_templates_are_valid_and_saved_presets_keep_their_values(db):
    for name in ("Classic White", "Bold Yellow", "Bold Green", "Red Outline", "Clean Minimal", "Highlight Style",
                 "Boxed", "Social Video", "Large Readable"):
        BUILTIN_PRESETS[name].validate()
    store = PresetStore(db)
    custom = CaptionStyle(font_size=90, text_color="#12AB34", background=True, fade_in_ms=200, max_lines=3)
    store.save("Mine", custom)
    assert store.get("Mine") == custom
    db.execute("UPDATE caption_presets SET style = ? WHERE name = ?", ("{not json", "Mine"))
    with pytest.raises(Exception, match="damaged"):
        store.get("Mine")
    # an old saved style (before fades existed) still loads; a damaged one falls back
    assert CaptionStyle.from_dict({"font_size": 50}).fade_in_ms == 0
    assert CaptionStyle.from_dict({"font_size": 5000}, fallback=custom) == custom


# -- bulk editing ---------------------------------------------------------------------------------

def shared_preset() -> RenderPreset:
    return RenderPreset(
        resize=ResizeSettings(preset="1080x1920", mode="blur", burn_captions=True),
        smart_cut=True, smart_cut_settings=SmartCutSettings(threshold_db=-40, min_silence=0.8),
        caption_style=BUILTIN_PRESETS["Bold Yellow"],
        effects=EditEffects(speed_enabled=True, video_speed=1.25, audio_enabled=True, volume=130),
        output=OutputSettings(codec="h265", quality="small"), filename_template="{original_name}_edited")


def test_shared_settings_reach_twelve_videos_and_overrides_are_kept(tmp_path):
    batch = Batch(shared=shared_preset())
    paths = []
    for i in range(12):
        p = tmp_path / f"clip {i:02d}.mp4"
        p.write_bytes(b"x")
        paths.append(str(p))
    added, _dupes = batch.add(paths)
    assert len(added) == 12
    for item in batch.items:
        item.metadata = META
    special = batch.items[3]
    special.override = batch.shared.copy()
    special.override.operation = "none"
    special.override.effects = EditEffects()
    special.override.filename_template = "{original_name}_special"
    batch = Batch.from_dict(json.loads(json.dumps(batch.to_dict())))  # survives saving
    assert batch.items[3].override.operation == "none" and batch.shared.smart_cut
    planned = [(i.id, tmp_path / "out" / output_name(batch.effective(i), i, n + 1, "Shared"))
               for n, i in enumerate(batch.items)]
    decisions = plan_outputs(planned, "rename", {path_key(i.path) for i in batch.items}, set())
    outputs = [d.path for d in decisions]
    assert len({path_key(o) for o in outputs}) == 12 and not {path_key(o) for o in outputs} & set(map(path_key, paths))
    captions = [CaptionSegment(0, 1, "Hi")]
    for item, decision in zip(batch.items, decisions):
        payload, _notes = build_payload(item, batch.effective(item), decision.path, overwrite=False, batch_id=batch.id,
                                        captions=captions, caption_style=CaptionStyle(),
                                        tracking_defaults=ReframeSettings(), models_dir="/m")
        if item is batch.items[3]:
            assert "stage" not in payload and payload["effects"] is None
            assert decision.path.name == "clip 03_special.mp4"
            continue
        assert payload["stage"]["layout"]["out_h"] == 1920
        assert payload["smartcut_auto"]["settings"]["threshold_db"] == -40
        assert payload["effects"]["video_speed"] == 1.25 and payload["effects"]["volume"] == 130
        assert payload["captions"]["style"]["text_color"] == BUILTIN_PRESETS["Bold Yellow"].text_color
        assert payload["output_settings"]["codec"] == "h265" and payload["batch_item"] == item.id


def test_bulk_payload_edge_cases():
    item = BatchItem("/v/silent.mp4", metadata=SILENT)
    payload, notes = build_payload(item, shared_preset(), Path("/o/s.mp4"), overwrite=False, batch_id="b",
                                   captions=None, caption_style=None, tracking_defaults=ReframeSettings(),
                                   models_dir="/m")
    assert "smartcut_auto" not in payload and any("Smart Cut skipped" in n for n in notes)
    assert any("Audio effects skipped" in n for n in notes) and any("No captions" in n for n in notes)
    reviewed = BatchItem("/v/a.mp4", metadata=META)
    payload, notes = build_payload(reviewed, shared_preset(), Path("/o/a.mp4"), overwrite=False, batch_id="b",
                                   captions=None, caption_style=None, tracking_defaults=ReframeSettings(),
                                   models_dir="/m", reviewed_cuts=[(0.0, 4.0), (6.0, 10.0)])
    assert payload["keep_ranges"] == [[0.0, 4.0], [6.0, 10.0]] and "smartcut_auto" not in payload
    off = RenderPreset()  # nothing switched on: no cuts, effects or captions
    payload, _ = build_payload(reviewed, off, Path("/o/b.mp4"), overwrite=False, batch_id="b",
                               captions=[CaptionSegment(0, 1, "x")], caption_style=None,
                               tracking_defaults=ReframeSettings(), models_dir="/m")
    assert payload["keep_ranges"] is None and payload["effects"] is None and payload["captions"] is None
    frame, notes = frame_preview_payload(reviewed, shared_preset(), [CaptionSegment(4, 6, "mid")], None)
    assert frame["stage"]["type"] == "layout" and frame["at"] == 5.0 and "Style: Default" in frame["ass"]
    assert any("not visible in a still frame" in n for n in notes)


def test_presets_keep_every_parameter_and_old_presets_load(db):
    store = RenderPresetStore(db)
    store.save("All on", shared_preset())
    loaded = store.get("All on")
    assert loaded.to_dict() == shared_preset().to_dict()
    old = RenderPreset.from_dict({"operation": "resize", "resize": {"preset": "1080x1080", "mode": "fit"}})
    assert not old.smart_cut and old.caption_style is None and not old.effects.active
    db.execute("UPDATE render_presets SET data = ? WHERE name = ?", ("[]", "All on"))
    with pytest.raises(Exception, match="damaged"):
        store.get("All on")
    bad = shared_preset()
    bad.effects.video_speed = 9
    with pytest.raises(EffectsError):
        bad.validate()


def test_one_failure_does_not_stop_the_queue_and_retry_repeats_only_failures(db):
    calls: dict[str, int] = {}
    lock = threading.Lock()
    fail_once = {"v05"}

    def handler(job, ctx):
        ctx.check_cancelled()
        name = job.payload["name"]
        with lock:
            calls[name] = calls.get(name, 0) + 1
        if name in fail_once:
            raise RuntimeError("FFmpeg failed")
        return {"output": name}

    manager = JobManager(JobStore(db), max_concurrent=2, max_heavy=1)
    manager.register("render_video", handler, heavy=True)
    manager.pause()
    jobs = [manager.submit("render_video", f"v{i:02d}", {"name": f"v{i:02d}"}) for i in range(15)]
    cancelled = manager.cancel(jobs[9].id)  # a pending job is cancelled before it starts
    assert cancelled.state is JobState.CANCELLED
    manager.resume()
    assert manager.wait_idle(20)
    states = {j.payload["name"]: manager.store.get(j.id).state for j in jobs}
    assert states["v05"] is JobState.FAILED and states["v09"] is JobState.CANCELLED
    assert sum(s is JobState.COMPLETED for s in states.values()) == 13
    assert "v09" not in calls and "FFmpeg failed" in manager.store.get(jobs[5].id).error
    fail_once.clear()
    manager.retry(jobs[5].id)
    assert manager.wait_idle(20)
    assert manager.store.get(jobs[5].id).state is JobState.COMPLETED
    assert calls["v05"] == 2 and all(n == 1 for k, n in calls.items() if k != "v05")  # successes not redone
    manager.shutdown()


def test_edit_page_settings_convert_to_bulk_settings():
    from ai_video_studio.render.batch import BatchError, preset_from_edit

    common = {"smart_cut_settings": SmartCutSettings(threshold_db=-42), "caption_style": BUILTIN_PRESETS["Bold Green"],
              "effects": EditEffects(speed_enabled=True, video_speed=1.5), "tracking_mode": "auto_face",
              "output": OutputSettings()}
    resized = preset_from_edit(ResizeSettings(preset="1080x1080", mode="fill"), use_cuts=True, burn_captions=True,
                               **common)
    assert resized.operation == "resize" and resized.resize.preset == "1080x1080" and resized.resize.burn_captions
    assert resized.smart_cut and resized.smart_cut_settings.threshold_db == -42
    assert resized.caption_style == BUILTIN_PRESETS["Bold Green"] and resized.effects.video_factor == 1.5
    reframed = preset_from_edit(ResizeSettings(preset="1080x1920", mode="reframe"), use_cuts=False,
                                burn_captions=False, **common)
    assert reframed.operation == "reframe" and reframed.caption_style is None and not reframed.smart_cut
    off = preset_from_edit(ResizeSettings(enabled=False), use_cuts=False, burn_captions=False, **common)
    assert off.operation == "none"
    assert preset_from_edit(None, use_cuts=False, burn_captions=False, **common).operation == "none"
    with pytest.raises(BatchError, match="Batch Resize"):  # a manual crop region is chosen per video
        preset_from_edit(ResizeSettings(mode="crop"), use_cuts=False, burn_captions=False, **common)
    assert ResizeSettings.from_dict({"preset": "1080x1080"}).enabled  # older saved settings


def test_automatic_captions_payload_never_uses_other_captions():
    from ai_video_studio.render.batch import BatchError

    preset = RenderPreset(resize=ResizeSettings(preset="1080x1920", mode="blur", burn_captions=True),
                          auto_captions=True, caption_style=BUILTIN_PRESETS["Social Video"])
    assert RenderPreset.from_dict(preset.to_dict()).auto_captions
    assert not RenderPreset.from_dict({"operation": "resize"}).auto_captions  # older presets
    item = BatchItem("/v/a.mp4", metadata=META)
    whisper = {"model": "base", "language": "auto", "device": "auto", "model_path": None}
    payload, notes = build_payload(item, preset, Path("/o/a.mp4"), overwrite=False, batch_id="b",
                                   captions=[CaptionSegment(0, 1, "someone else's")], caption_style=None,
                                   tracking_defaults=ReframeSettings(), models_dir="/m", transcription=whisper)
    assert payload["captions_auto"] == whisper and payload["captions"]["segments"] == []
    assert payload["captions"]["style"] == BUILTIN_PRESETS["Social Video"].to_dict() and not notes
    with pytest.raises(BatchError):
        build_payload(item, preset, Path("/o/a.mp4"), overwrite=False, batch_id="b", captions=None,
                      caption_style=None, tracking_defaults=ReframeSettings(), models_dir="/m")
    preset.resize.burn_captions = False  # automatic captions only matter with burn-in
    payload, _ = build_payload(item, preset, Path("/o/a.mp4"), overwrite=False, batch_id="b", captions=None,
                               caption_style=None, tracking_defaults=ReframeSettings(), models_dir="/m",
                               transcription=whisper)
    assert payload["captions_auto"] is None and payload["captions"] is None
