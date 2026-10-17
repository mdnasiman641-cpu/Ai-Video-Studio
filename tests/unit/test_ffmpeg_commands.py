import pytest

from ai_video_studio.core import ffmpeg_commands as fc


def test_audio_extract_args():
    args = fc.audio_extract_args("ffmpeg", "C:\\in put.mp4", "out.wav")
    assert args[args.index("-i") + 1] == "C:\\in put.mp4"
    assert args[args.index("-ar") + 1] == "16000" and args[args.index("-ac") + 1] == "1"
    assert "pcm_s16le" in args and args[-1] == "out.wav"
    assert "pipe:1" in args


def test_burn_in_args_libx264_copy_aac():
    enc = fc.EncoderSettings("libx264", 21)
    args = fc.burn_in_args("ffmpeg", "src.mp4", "captions.ass", "out.mp4", enc, "aac", True)
    assert args[args.index("-vf") + 1] == "ass=captions.ass:shaping=complex"
    assert args[args.index("-crf") + 1] == "21"
    assert args[args.index("-c:a") + 1] == "copy"
    assert "0:a:0?" in args and args[-1] == "out.mp4"


def test_burn_in_args_nvenc_reencode_audio_no_audio():
    enc = fc.EncoderSettings("h264_nvenc", 23)
    args = fc.burn_in_args("ffmpeg", "src.mov", "captions.ass", "out.mp4", enc, "pcm_s16le", True)
    assert args[args.index("-c:v") + 1] == "h264_nvenc" and args[args.index("-cq") + 1] == "23"
    assert args[args.index("-c:a") + 1] == "aac"
    silent = fc.burn_in_args("ffmpeg", "src.mp4", "captions.ass", "out.mp4", enc, None, False)
    assert "-c:a" not in silent and "0:a:0?" not in silent


@pytest.mark.parametrize("name", ["C:\\x\\captions.ass", "a:b.ass", "x'y.ass", "dir/c.ass"])
def test_subtitle_name_must_be_plain(name):
    with pytest.raises(ValueError):
        fc.burn_in_args("ffmpeg", "s", name, "o", fc.EncoderSettings(), None, False)
    with pytest.raises(ValueError):
        fc.frame_preview_args("ffmpeg", "s", name, 1, "o.png")


def test_unknown_encoder():
    with pytest.raises(ValueError):
        fc.EncoderSettings("magic").video_args()


def test_frame_preview_seeks_after_input():
    args = fc.frame_preview_args("ffmpeg", "s.mp4", "captions.ass", 2.5, "o.png")
    assert args.index("-ss") > args.index("-i") and args[args.index("-ss") + 1] == "2.500"


def test_progress_parser():
    p = fc.ProgressParser(10.0)
    assert p.feed("frame=10") is None
    assert p.feed("out_time_us=5000000") == 0.5
    assert p.feed("out_time_ms=N/A") is None
    assert p.feed("out_time_ms=2000000") == 0.5  # never goes backwards
    assert p.feed("progress=end") == 1.0 and p.finished
    assert fc.ProgressParser(None).feed("out_time_us=1000000") is None


def test_choose_encoder(monkeypatch):
    monkeypatch.setattr(fc, "nvenc_usable", lambda ffmpeg: False)
    assert fc.choose_video_encoder("libx264", "ffmpeg")[0] == "libx264"
    codec, reason = fc.choose_video_encoder("h264_nvenc", "ffmpeg")
    assert codec == "libx264" and "not usable" in reason
    monkeypatch.setattr(fc, "nvenc_usable", lambda ffmpeg: True)
    assert fc.choose_video_encoder("auto", "ffmpeg")[0] == "h264_nvenc"
    assert fc.choose_video_encoder("libx264", "ffmpeg")[0] == "libx264"


def test_available_encoders_parsing(monkeypatch):
    out = (" V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"
           " V....D libx264              libx264 H.264\n ------\n A....D aac   AAC\n")
    monkeypatch.setattr(fc, "run_process", lambda *a, **k: type("R", (), {"stdout": out, "returncode": 0})())
    assert {"h264_nvenc", "libx264", "aac"} <= fc.available_encoders("ffmpeg")
