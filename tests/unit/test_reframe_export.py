import json

import pytest

from ai_video_studio.core.errors import ProjectError
from ai_video_studio.core.ffmpeg_commands import EncoderSettings
from ai_video_studio.core.media import MediaItem
from ai_video_studio.reframe.export import (
    command_fps,
    reframe_args,
    reframe_filter,
    reframe_frame_args,
    sendcmd_script,
)
from ai_video_studio.reframe.framing import CropPath
from ai_video_studio.reframe.model import ReframeError, ReframeSettings, ReframeState, TrackingAnalysis
from ai_video_studio.storage.project import Project
from tests.reframe_helpers import face_at, make_analysis, make_track

W, H = 1920, 1080


def moving_path():
    return CropPath(W, H, 608, 1080, [0.0, 1.0, 2.0], [0.0, 100.0, 100.0], [0.0, 0.0, 0.0])


def parse(script):
    rows = []
    for line in script.strip().splitlines():
        t, rest = line.split(" ", 1)
        parts = rest.rstrip(";").split(", ")
        assert parts[0].startswith("crop x ") and parts[1].startswith("crop y ")
        rows.append((float(t), int(parts[0][7:]), int(parts[1][7:])))
    return rows


def test_sendcmd_script_has_one_command_per_changed_frame():
    rows = parse(sendcmd_script(moving_path(), 25, 2.0))
    assert rows[0] == (0.0, 0, 0)
    # 25 fps: x changes every frame from 0 s to 1 s (4 px per frame), then stays at 100.
    assert [x for _t, x, _y in rows] == [4 * k for k in range(26)]
    times = [t for t, _x, _y in rows]
    assert times == sorted(times)
    assert times[1] == pytest.approx(0.02)  # half a frame early, so it applies to that frame


def test_sendcmd_script_for_static_crop_is_a_single_command():
    path = CropPath(W, H, 608, 1080, [0.0, 5.0], [656.0, 656.0], [0.0, 0.0])
    assert parse(sendcmd_script(path, 30, 5.0)) == [(0.0, 656, 0)]


def test_command_rate_limits():
    assert command_fps(None) == 30 and command_fps(0) == 30 and command_fps(float("nan")) == 30
    assert command_fps(12) == 24 and command_fps(59.94) == 59.94 and command_fps(1000) == 120


def test_reframe_filter_chain():
    path = moving_path()
    assert reframe_filter(path, 1080, 1920) == (
        "sendcmd=f=reframe.cmd,crop=w=608:h=1080:x=0:y=0,scale=1080:1920:flags=lanczos,setsar=1")
    with_subs = reframe_filter(path, 1080, 1920, subtitle_file="captions.ass")
    assert with_subs.endswith(",setsar=1,subtitles=captions.ass")  # captions rendered on the output frame
    static = reframe_filter(path, 720, 1280, command_file=None)
    assert static.startswith("crop=w=608:h=1080:x=0:y=0,scale=720:1280")
    with pytest.raises(ValueError):
        reframe_filter(path, 1080, 1920, subtitle_file="C:\\temp\\captions.ass")
    with pytest.raises(ValueError):
        reframe_filter(path, 1080, 1920, command_file="../x.cmd")


def test_reframe_args_keep_audio_and_use_selected_encoder():
    vf = reframe_filter(moving_path(), 1080, 1920)
    cpu = reframe_args("ffmpeg", "in.mp4", vf, "out.mp4", EncoderSettings("libx264", 20), "aac", True)
    assert cpu[cpu.index("-vf") + 1] == vf
    assert ["-map", "0:a:0?"] == cpu[cpu.index("-map", cpu.index("-map") + 1):][:2]
    assert "-c:a" in cpu and cpu[cpu.index("-c:a") + 1] == "copy"
    assert "libx264" in cpu and "-r" not in cpu  # frame timing of the source is kept
    gpu = reframe_args("ffmpeg", "in.mp4", vf, "out.mp4", EncoderSettings("h264_nvenc", 20), "opus", True)
    assert "h264_nvenc" in gpu and gpu[gpu.index("-c:a") + 1] == "aac"
    silent = reframe_args("ffmpeg", "in.mp4", vf, "out.mp4", EncoderSettings(), None, False)
    assert "-c:a" not in silent and "0:a:0?" not in silent


def test_frame_preview_args_use_the_crop_at_that_time():
    args = reframe_frame_args("ffmpeg", "in.mp4", 0.5, moving_path(), 1080, 1920, "captions.ass", "f.png")
    vf = args[args.index("-vf") + 1]
    assert vf == "crop=w=608:h=1080:x=50:y=0,scale=1080:1920:flags=lanczos,setsar=1,subtitles=captions.ass"
    assert args[args.index("-ss") + 1] == "0.500" and args.index("-ss") > args.index("-i")


# -- settings and persistence ---------------------------------------------------------------------
def test_settings_validation():
    ReframeSettings().validate()
    for bad in (dict(mode="zoom"), dict(preset="21:9"), dict(zoom=0.5), dict(caption_safe="left"),
                dict(output_width=1080, output_height=0), dict(output_width=10, output_height=10),
                dict(dead_zone=0.9), dict(switches=[[-1, 2]]), dict(analysis_fps=0)):
        with pytest.raises(ReframeError):
            ReframeSettings(**bad).validate()


def test_settings_schedule_and_unknown_keys():
    s = ReframeSettings.from_dict({"target": 2, "switches": [[8, None], [3.5, 1]], "future_option": True})
    assert s.subject_schedule() == [(0.0, 2), (3.5, 1), (8.0, None)]


def test_analysis_round_trip_and_damage():
    analysis = make_analysis([make_track(1, "face", face_at(500, 400), 3), make_track(2, "person", face_at(900, 500, 300), 3)],
                             duration=3)
    analysis.warnings = ["note"]
    analysis.source_size, analysis.source_mtime = 1234, 99.5
    copy = TrackingAnalysis.from_dict(json.loads(json.dumps(analysis.to_dict())))
    assert [t.kind for t in copy.tracks] == ["face", "person"]
    assert len(copy.tracks[0].samples) == len(analysis.tracks[0].samples)
    assert copy.tracks[0].samples[3].cx == pytest.approx(analysis.tracks[0].samples[3].cx, abs=1e-4)
    assert copy.warnings == ["note"] and copy.source_size == 1234
    with pytest.raises(ReframeError, match="damaged"):
        TrackingAnalysis.from_dict({"width": 10})


def test_project_saves_and_reopens_reframe_data(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    project = Project(name="P")
    item = project.add_media(str(video))
    analysis = make_analysis([make_track(1, "face", face_at(500, 400), 2)], duration=2)
    item.reframe = ReframeState(ReframeSettings(preset="1:1", target=1, switches=[[1.0, None]], zoom=1.3), analysis)
    path = project.save(tmp_path / "p.avsproj")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 3 and data["media"][0]["reframe"]["settings"]["preset"] == "1:1"
    loaded = Project.load(path).media[0]
    assert loaded.reframe.settings.target == 1 and loaded.reframe.settings.zoom == 1.3
    assert loaded.reframe.analysis.tracks[0].id == 1


def test_version_2_projects_still_open(tmp_path):
    old = {"format": "ai-video-studio-project", "version": 2, "id": "abc", "name": "Old",
           "media": [{"id": "m1", "path": str(tmp_path / "a.mp4"), "captions": None}]}
    path = tmp_path / "old.avsproj"
    path.write_text(json.dumps(old), encoding="utf-8")
    project = Project.load(path)
    assert project.media[0].reframe is None


def test_damaged_reframe_data_is_reported(tmp_path):
    data = MediaItem(path=str(tmp_path / "a.mp4")).to_dict()
    data["reframe"] = {"settings": {"preset": "nope"}}
    project = {"format": "ai-video-studio-project", "version": 3, "id": "abc", "name": "X", "media": [data]}
    path = tmp_path / "bad.avsproj"
    path.write_text(json.dumps(project), encoding="utf-8")
    with pytest.raises((ReframeError, ProjectError)):
        Project.load(path)
