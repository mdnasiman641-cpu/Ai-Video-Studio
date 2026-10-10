"""GPU / NVENC detection against the real tools on this machine. Results are printed to the log."""

import pytest

from ai_video_studio.captions.transcriber import cuda_device_count, device_attempts
from ai_video_studio.core.ffmpeg_commands import available_encoders, choose_video_encoder, nvenc_usable
from ai_video_studio.core.ffmpeg_tools import find_tool
from tests.conftest import requires_ffmpeg


def test_cuda_detection_is_consistent():
    gpus = cuda_device_count()
    print(f"\n[hardware] CUDA devices visible to CTranslate2: {gpus}")
    assert isinstance(gpus, int) and gpus >= 0
    expected_first = "cuda" if gpus else "cpu"
    assert device_attempts("auto", gpus)[0][0] == expected_first
    assert device_attempts("auto", gpus)[-1] == ("cpu", "int8")  # CPU fallback is always last


@requires_ffmpeg
def test_encoder_choice_matches_real_nvenc_check():
    ffmpeg = find_tool("ffmpeg")
    listed = "h264_nvenc" in available_encoders(ffmpeg)
    usable = nvenc_usable(ffmpeg)
    codec, reason = choose_video_encoder("auto", ffmpeg)
    print(f"\n[hardware] h264_nvenc listed={listed} usable={usable} auto-choice={codec} ({reason})")
    assert "libx264" in available_encoders(ffmpeg), "FFmpeg build lacks libx264 (CPU fallback)"
    assert codec == ("h264_nvenc" if usable else "libx264")
    if not listed:
        assert not usable


@requires_ffmpeg
def test_ffmpeg_has_libass_for_burn_in():
    from ai_video_studio.core.process import run_process

    out = run_process([str(find_tool("ffmpeg")), "-hide_banner", "-filters"], timeout=30).stdout
    if " subtitles " not in out:
        pytest.fail("This FFmpeg build has no 'subtitles' filter (libass); caption burn-in cannot work")
