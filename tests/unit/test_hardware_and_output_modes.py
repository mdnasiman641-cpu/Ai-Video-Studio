"""Hardware detection, export modes, encoder choice with fallback, output sizes, the analysis
cache and the export check (no GUI, FFmpeg replaced by fakes where noted)."""

import os
import time

import pytest

from ai_video_studio.core import ffmpeg_commands as fc
from ai_video_studio.core import hardware
from ai_video_studio.core.errors import MediaError
from ai_video_studio.jobs.render_jobs import verify_output
from ai_video_studio.reframe.framing import compute_crop_path
from ai_video_studio.reframe.model import ReframeSettings
from ai_video_studio.render.batch import BatchError, RenderPreset, batch_analysis_features
from ai_video_studio.render.layout import (
    ResizeSettings,
    aspect_and_resolution,
    compute_layout,
    layout_filter,
    settings_for_size,
    size_for,
)
from ai_video_studio.render.output import MODES, OutputSettings, OutputSettingsError
from ai_video_studio.storage import analysis_cache


# -- export modes ------------------------------------------------------------------------

def test_modes_have_genuinely_different_encoder_settings():
    seen = {}
    for mode in ("draft", "fast", "standard", "high", "maximum"):
        s = OutputSettings(mode=mode)
        args = {codec: fc.EncoderSettings(codec=codec, quality=s.crf_value, speed=s.speed).video_args()
                for codec in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf", "libx265")}
        seen[mode] = args
        assert s.audio_kbps in (96, 128, 160, 192, 256)
    x264 = {m: a["libx264"][a["libx264"].index("-preset") + 1] for m, a in seen.items()}
    assert x264 == {"draft": "ultrafast", "fast": "veryfast", "standard": "medium", "high": "slow",
                    "maximum": "slower"}
    nvenc = {m: a["h264_nvenc"][a["h264_nvenc"].index("-preset") + 1] for m, a in seen.items()}
    assert nvenc == {"draft": "p1", "fast": "p3", "standard": "p5", "high": "p6", "maximum": "p7"}
    assert "-multipass" in seen["maximum"]["h264_nvenc"] and "-multipass" not in seen["standard"]["h264_nvenc"]
    crf = [OutputSettings(mode=m).crf_value for m in ("draft", "fast", "standard", "high", "maximum")]
    assert crf == sorted(crf, reverse=True) and len(set(crf)) == 5  # each mode a different quality
    assert seen["standard"]["h264_qsv"][-2:] == ["-pix_fmt", "nv12"] and "-global_quality" in seen["high"]["h264_qsv"]
    assert seen["high"]["h264_amf"][seen["high"]["h264_amf"].index("-quality") + 1] == "quality"
    assert seen["standard"]["libx265"][seen["standard"]["libx265"].index("-crf") + 1] == "25"  # H.265 offset


def test_custom_mode_and_older_settings_keep_their_values():
    custom = OutputSettings(mode="custom", quality="custom", crf=30, cpu_preset="veryslow", audio_bitrate=320)
    assert custom.crf_value == 30 and custom.audio_kbps == 320
    args = fc.EncoderSettings(codec="libx264", quality=30, speed="custom", cpu_preset="veryslow").video_args()
    assert args[args.index("-preset") + 1] == "veryslow"
    old = OutputSettings.from_dict({"codec": "h264", "encoder": "gpu", "quality": "small"})  # saved before modes
    assert old.mode == "custom" and old.crf_value == 26 and old.encoder == "nvenc"
    assert OutputSettings.from_dict({"encoder": "libx264"}).encoder == "cpu"
    for bad in ({"mode": "turbo"}, {"container": "avi"}, {"cpu_preset": "warp"}, {"hw_decode": "always"},
                {"encoder": "gpu"}):
        with pytest.raises(OutputSettingsError):
            OutputSettings(**bad).validate()
    assert OutputSettings(container="mkv").suffix == ".mkv" and set(MODES) >= {"draft", "maximum", "custom"}


# -- encoder choice ------------------------------------------------------------------------

def fake_encoders(monkeypatch, usable: set[str], listed: set[str] | None = None):
    monkeypatch.setattr(fc, "nvenc_usable", lambda ffmpeg, encoder="h264_nvenc": encoder in usable)
    monkeypatch.setattr(fc, "available_encoders", lambda ffmpeg: listed or {"libx264", "libx265"} | usable)
    fc._encoder_errors["ffmpeg|h264_nvenc"] = "No NVENC capable devices found"


def test_auto_prefers_nvenc_then_quick_sync_then_amf_then_cpu(monkeypatch):
    fake_encoders(monkeypatch, {"h264_nvenc", "h264_qsv", "h264_amf"})
    assert fc.choose_video_encoder("auto", "ffmpeg")[0] == "h264_nvenc"
    fake_encoders(monkeypatch, {"h264_qsv", "h264_amf"})
    assert fc.choose_video_encoder("auto", "ffmpeg")[0] == "h264_qsv"
    fake_encoders(monkeypatch, {"h264_amf"})
    assert fc.choose_video_encoder("auto", "ffmpeg") == ("h264_amf", "AMD AMF")
    assert fc.choose_video_encoder("amf", "ffmpeg")[0] == "h264_amf"
    fake_encoders(monkeypatch, set())
    codec, reason = fc.choose_video_encoder("auto", "ffmpeg")
    assert codec == "libx264" and "No hardware encoder is usable" in reason


def test_chosen_gpu_encoder_falls_back_only_when_allowed(monkeypatch):
    fake_encoders(monkeypatch, set())
    codec, reason = fc.choose_video_encoder("nvenc", "ffmpeg", allow_fallback=True)
    assert codec == "libx264" and "No NVENC capable devices found" in reason and "using CPU" in reason
    with pytest.raises(fc.EncoderUnavailableError, match="not usable on this system"):
        fc.choose_video_encoder("nvenc", "ffmpeg", allow_fallback=False)
    assert fc.encoder_label("h264_nvenc") == "NVIDIA NVENC H.264" and fc.encoder_label("libx264") == "CPU (libx264)"


def test_progress_parser_reads_speed():
    p = fc.ProgressParser(10.0)
    assert p.feed("speed=1.75x") is None and p.speed == 1.75
    assert p.feed("speed=N/A") is None and p.speed == 1.75
    assert p.feed("fps=48.5") is None and p.fps == 48.5


# -- hardware --------------------------------------------------------------------------------

def test_detection_trusts_test_encodes_not_names(monkeypatch):
    monkeypatch.setattr(hardware, "cpu_info", lambda: ("AMD Ryzen 7 7700 8-Core Processor", 16, 8))
    monkeypatch.setattr(hardware, "memory_gb", lambda: 31.2)
    monkeypatch.setattr(hardware, "gpu_info", lambda: [hardware.GpuInfo("NVIDIA GeForce RTX 4070", "nvidia", 12282)])
    monkeypatch.setattr(hardware, "ffmpeg_version", lambda f: "7.1.1-essentials")
    monkeypatch.setattr(hardware, "hwaccels", lambda f: ["cuda", "d3d11va", "qsv"])
    fake_encoders(monkeypatch, {"h264_nvenc", "hevc_nvenc"}, {"libx264", "libx265", "h264_nvenc", "hevc_nvenc",
                                                                   "h264_qsv", "h264_amf"})
    info = hardware.detect("fake-ffmpeg-1", refresh=True)
    assert info.usable("h264_nvenc") and not info.usable("h264_qsv") and info.encoders["h264_qsv"].listed
    assert not info.encoders["hevc_amf"].listed
    rec = hardware.recommend(info)
    assert rec.encoder_label == "NVIDIA NVENC" and rec.max_renders == 2 and rec.transcription_threads == 8
    text = "\n".join(info.summary_lines())
    assert "RTX 4070 (11 GB)" in text and "h264_nvenc ✔ usable" in text and "h264_qsv listed, test failed" in text
    # an NVIDIA card whose encoder does not work in this FFmpeg: not offered
    fake_encoders(monkeypatch, set(), {"libx264", "h264_nvenc"})
    info = hardware.detect("fake-ffmpeg-2", refresh=True)
    assert not info.family_usable("nvenc") and hardware.recommend(info).max_renders == 1


def test_real_machine_detection_is_consistent():
    info = hardware.detect(None, refresh=True)  # without FFmpeg: operating system facts only
    assert info.logical_cores >= 1 and info.physical_cores >= 1 and info.encoders == {}


# -- output size ----------------------------------------------------------------------------

def test_aspect_and_resolution_sizes():
    assert size_for("9:16", 1080) == (1080, 1920) and size_for("4:5", 1080) == (1080, 1350)
    assert size_for("16:9", 2160) == (3840, 2160) and size_for("1:1", 720) == (720, 720)
    s = ResizeSettings()
    settings_for_size(s, "4:5", 2160)
    assert (s.preset, s.width, s.height) == ("custom", 2160, 2700) and aspect_and_resolution(s) == ("4:5", 2160)
    settings_for_size(s, "9:16", 1080)
    assert s.preset == "1080x1920" and aspect_and_resolution(s) == ("9:16", 1080)
    settings_for_size(s, "original", 720)
    assert s.output_size(1920, 1080) == (1280, 720) and s.output_size(1080, 1920) == (720, 1280)
    settings_for_size(s, "custom", 0, 1000, 500)
    assert s.output_size(1920, 1080) == (1000, 500) and aspect_and_resolution(s) == ("custom", 0)
    s.width = 1001
    with pytest.raises(Exception, match="even"):
        s.validate()


def test_blur_background_is_computed_at_a_quarter_size():
    layout = compute_layout(1920, 1080, ResizeSettings(preset="1080x1920", mode="blur", blur=20))
    graph = layout_filter(layout, "a", "b")
    assert "scale=270:480" in graph and "boxblur=luma_radius=5" in graph and "scale=1080:1920:flags=bilinear" in graph


def test_auto_reframe_custom_aspect_never_stretches():
    s = ReframeSettings(tracking_mode="center", preset="custom", output_width=1000, output_height=500)
    s.validate()
    path = compute_crop_path(None, s, 1920, 1080, 5.0)
    assert path.crop_w / path.crop_h == pytest.approx(2.0, abs=0.01)
    with pytest.raises(Exception, match="custom aspect"):
        ReframeSettings(preset="custom").validate()
    preset = RenderPreset(operation="reframe", resize=ResizeSettings(preset="custom", width=1000, height=500))
    preset.validate()
    with pytest.raises(BatchError):
        RenderPreset(operation="reframe", resize=ResizeSettings(preset="original")).validate()


def test_batches_skip_person_detection_for_face_only_modes():
    assert "persons" in ReframeSettings(tracking_mode="auto_face").analysis_features()
    assert "persons" not in batch_analysis_features(ReframeSettings(tracking_mode="auto_face"))
    assert "persons" not in batch_analysis_features(ReframeSettings(tracking_mode="active_speaker"))
    assert "persons" in batch_analysis_features(ReframeSettings(tracking_mode="auto_visible"))


# -- analysis cache ---------------------------------------------------------------------------

def test_analysis_cache_matches_file_and_settings(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"1234")
    analysis_cache.put("transcript", video, {"model": "small", "language": "bn"}, {"segments": [1]})
    assert analysis_cache.get("transcript", video, {"model": "small", "language": "bn"}) == {"segments": [1]}
    assert analysis_cache.get("transcript", video, {"model": "small", "language": "en"}) is None  # other settings
    assert analysis_cache.get("levels", video, {"model": "small", "language": "bn"}) is None      # other kind
    other = tmp_path / "b.mp4"
    other.write_bytes(b"1234")
    assert analysis_cache.get("transcript", other, {"model": "small", "language": "bn"}) is None  # other video
    time.sleep(0.01)
    video.write_bytes(b"12345")  # the file changed: never reuse
    os.utime(video, None)
    assert analysis_cache.get("transcript", video, {"model": "small", "language": "bn"}) is None
    analysis_cache.put("levels", video, {}, {"x": "y" * 1000})
    analysis_cache.prune(limit=10)
    assert analysis_cache.get("levels", video, {}) is None and analysis_cache.clear() >= 0


# -- the export check -----------------------------------------------------------------------------

def test_verify_output_rejects_empty_and_mismatching_files(tmp_path, monkeypatch):
    from ai_video_studio.core.ffmpeg_tools import VideoMetadata
    from ai_video_studio.jobs import render_jobs

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaError, match="no file"):
        verify_output(empty, None, 1.0)
    meta = VideoMetadata(duration=10.0, width=1080, height=1920, fps=30.0, video_codec="h264", has_audio=False)
    monkeypatch.setattr(render_jobs, "probe_video", lambda path, ffprobe: meta)
    for expect, audio, message in (({"width": 1920, "height": 1080}, False, "1080×1920 instead of 1920×1080"),
                                   ({"fps": 25}, False, "30 fps instead of 25"),
                                   (None, True, "audio track is missing")):
        out = tmp_path / "o.mp4"
        out.write_bytes(b"x")
        with pytest.raises(MediaError, match=message):
            verify_output(out, "ffprobe", 10.0, expect=expect, has_audio=audio)
        assert not out.exists()  # a failing file is removed
    out = tmp_path / "ok.mp4"
    out.write_bytes(b"x")
    verify_output(out, "ffprobe", 10.0, expect={"width": 1080, "height": 1920, "fps": 30}, has_audio=False)
    with pytest.raises(MediaError, match="9.9|instead of about"):
        out.write_bytes(b"x")
        verify_output(out, "ffprobe", 20.0)
