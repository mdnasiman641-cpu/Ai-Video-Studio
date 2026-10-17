"""Render pipeline: cut snapping, filter graphs, audio handling, encoder settings."""

import pytest

from ai_video_studio.core import ffmpeg_commands as fc
from ai_video_studio.core.ffmpeg_commands import EncoderSettings
from ai_video_studio.render.output import OutputSettings, OutputSettingsError
from ai_video_studio.render.pipeline import (
    GRAPH_FILE,
    audio_plan,
    build_filter_graph,
    is_full_clip,
    kept_duration,
    render_args,
    snap_ranges,
)


def test_snap_ranges_to_frames_and_merge():
    ranges = snap_ranges([(2.01, 3.0), (0.0, 1.013), (1.02, 1.5), (5.0, 9.0)], fps=25, duration=6.0)
    # 1.013 -> 1.0 and 1.02 -> 1.04 would leave a 1-frame gap; 2.01 -> 2.0; the last range is clamped.
    assert ranges == [(0.0, 1.0), (1.04, 1.52), (2.0, 3.0), (5.0, 6.0)]
    assert all(round(a * 25, 6).is_integer() and round(b * 25, 6).is_integer() for a, b in ranges)
    assert snap_ranges([(0.0, 1.0), (1.0, 2.0)], 30) == [(0.0, 2.0)]
    assert snap_ranges([(0.5, 0.51)], 25) == []          # shorter than a frame: dropped
    assert snap_ranges([(0.1234, 0.9)], None) == [(0.1234, 0.9)]  # unknown frame rate: unchanged
    assert kept_duration([(0, 1), (2, 2.5)]) == 1.5


def test_full_clip_detection():
    assert is_full_clip(None, 10) and is_full_clip([(0.0, 10.0)], 10)
    assert not is_full_clip([(0.0, 5.0)], 10)


def test_graph_with_cuts_uses_trim_atrim_and_concat_in_order():
    graph, video, audio = build_filter_graph(keep_ranges=[(0.0, 1.0), (2.0, 3.5)], has_audio=True,
                                             subtitle_file="captions.ass", target_fps=30)
    assert "[0:v:0]split=2[vs0][vs1]" in graph and "[0:a:0]asplit=2[as0][as1]" in graph
    assert "[vs1]trim=start=2.000000:end=3.500000,setpts=PTS-STARTPTS[v1]" in graph
    assert "[as1]atrim=start=2.000000:end=3.500000,asetpts=PTS-STARTPTS[a1]" in graph
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcut][acut]" in graph
    assert graph.endswith("[vcut]fps=30,ass=captions.ass:shaping=complex,setsar=1[vout]")
    assert (video, audio) == ("vout", "acut")


def test_graph_without_audio_or_cuts():
    graph, _v, audio = build_filter_graph(keep_ranges=[(1.0, 2.0)], has_audio=False)
    assert "atrim" not in graph and "concat=n=1:v=1:a=0[vcut]" in graph and audio is None
    plain, _v, audio = build_filter_graph(keep_ranges=None, has_audio=True,
                                          video_stage=lambda s, d: f"[{s}]scale=640:360[{d}]")
    assert plain == "[0:v:0]scale=640:360[vstage];[vstage]setsar=1[vout]" and audio is None


def test_subtitle_file_must_be_a_plain_name():
    with pytest.raises(ValueError):
        build_filter_graph(keep_ranges=None, has_audio=False, subtitle_file="C:\\x\\c.ass")


def test_audio_plan():
    s = OutputSettings()
    assert audio_plan(s, True, "aac", False)[0] == "copy"
    assert audio_plan(s, True, "pcm_s16le", False)[0] == "aac"
    mode, notes = audio_plan(s, True, "aac", True)
    assert mode == "filtered" and "re-encoded" in notes[0]
    assert audio_plan(OutputSettings(audio="none"), True, "aac", True)[0] == "none"
    assert audio_plan(s, False, None, True)[0] == "none"
    assert audio_plan(OutputSettings(audio="aac"), True, "aac", False)[0] == "aac"
    assert audio_plan(OutputSettings(all_audio_tracks=True), True, "aac", False)[0] == "all_tracks"
    mode, notes = audio_plan(OutputSettings(all_audio_tracks=True), True, "aac", True)
    assert mode == "filtered" and any("first audio track" in n for n in notes)


def test_render_args_maps_and_codecs(tmp_path):
    graph, _v, audio = build_filter_graph(keep_ranges=[(0.0, 1.0), (2.0, 3.0)], has_audio=True)
    args = render_args("ffmpeg", "in.mp4", graph, audio, "filtered", EncoderSettings("libx265", 21), "o.mp4",
                       tmp_path)
    assert args[args.index("-filter_complex") + 1] == graph
    assert args.count("-map") == 2 and "[acut]" in args and "[vout]" in args
    assert args[args.index("-c:v") + 1] == "libx265" and args[args.index("-crf") + 1] == "25"  # H.265 offset
    assert args[args.index("-c:a") + 1] == "aac" and "hvc1" in args
    copy = render_args("ffmpeg", "in.mp4", "[0:v:0]setsar=1[vout]", None, "copy", EncoderSettings(), "o.mp4")
    assert copy[copy.index("-c:a") + 1] == "copy" and "0:a:0?" in copy
    silent = render_args("ffmpeg", "in.mp4", "[0:v:0]setsar=1[vout]", None, "none", EncoderSettings(), "o.mp4")
    assert "-c:a" not in silent and silent.count("-map") == 1


def test_long_graphs_go_to_a_script_file(tmp_path):
    ranges = [(i * 2.0, i * 2.0 + 1.0) for i in range(300)]
    graph, _v, audio = build_filter_graph(keep_ranges=ranges, has_audio=True)
    args = render_args("ffmpeg", "in.mp4", graph, audio, "filtered", EncoderSettings(), "o.mp4", tmp_path)
    assert "-filter_complex" not in args and args[args.index("-filter_complex_script") + 1] == GRAPH_FILE
    assert (tmp_path / GRAPH_FILE).read_text() == graph
    assert sum(len(a) for a in args) < 2000  # far below the Windows command-line limit


def test_encoder_args_quality_and_bitrate():
    assert EncoderSettings("libx264", 21).video_args()[:6] == ["-c:v", "libx264", "-preset", "medium", "-crf", "21"]
    nv = EncoderSettings("hevc_nvenc", 21).video_args()
    assert nv[nv.index("-cq") + 1] == "25" and "hvc1" in nv
    rate = EncoderSettings("h264_nvenc", 21, bitrate_kbps=8000).video_args()
    assert rate[rate.index("-b:v") + 1] == "8000k" and "-cq" not in rate and rate[rate.index("-maxrate") + 1] == "12000k"


def test_output_settings_validation_and_notes():
    s = OutputSettings()
    s.validate()
    assert s.crf_value == 21 and s.target_bitrate is None and s.target_fps() is None
    assert OutputSettings(quality="custom", crf=30).crf_value == 30
    assert OutputSettings(rate_control="bitrate", bitrate_kbps=5000).target_bitrate == 5000
    for bad in (dict(codec="vp9"), dict(fps="23"), dict(audio_bitrate=100), dict(bitrate_kbps=10), dict(crf=60)):
        with pytest.raises(OutputSettingsError):
            OutputSettings(**bad).validate()
    assert "higher than the source" in OutputSettings(fps="60").notes(25.0)[0]
    assert OutputSettings(fps="30").notes(29.97) == []
    assert OutputSettings.from_dict({"codec": "h265", "bogus": 1}).codec == "h265"


def test_h265_encoder_choice(monkeypatch):
    monkeypatch.setattr(fc, "nvenc_usable", lambda ffmpeg, encoder="h264_nvenc": False)
    monkeypatch.setattr(fc, "available_encoders", lambda ffmpeg: {"libx264", "libx265"})
    assert fc.choose_video_encoder("auto", "ffmpeg", "h265")[0] == "libx265"
    assert "not usable" in fc.choose_video_encoder("gpu", "ffmpeg", "h265")[1]
    monkeypatch.setattr(fc, "available_encoders", lambda ffmpeg: {"libx264"})
    with pytest.raises(fc.EncoderUnavailableError):
        fc.choose_video_encoder("cpu", "ffmpeg", "h265")
    monkeypatch.setattr(fc, "nvenc_usable", lambda ffmpeg, encoder="h264_nvenc": encoder == "hevc_nvenc")
    assert fc.choose_video_encoder("cpu", "ffmpeg", "h265")[0] == "hevc_nvenc"  # no libx265: GPU instead, said so
    assert fc.choose_video_encoder("auto", "ffmpeg", "h265") == ("hevc_nvenc", "NVIDIA NVENC")
