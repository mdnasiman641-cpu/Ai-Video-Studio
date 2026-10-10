import pytest

from ai_video_studio.core.errors import MediaError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata, parse_probe_output
from ai_video_studio.core.media import format_duration, format_size

PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
         "avg_frame_rate": "30000/1001", "r_frame_rate": "30000/1001", "pix_fmt": "yuv420p"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
    ],
    "format": {"duration": "12.345678", "bit_rate": "8000000", "size": "12345678", "format_name": "mov,mp4"},
}


def test_parse_basic():
    meta = parse_probe_output(PROBE)
    assert (meta.width, meta.height) == (1920, 1080)
    assert meta.fps == pytest.approx(29.97, abs=0.01)
    assert meta.duration == pytest.approx(12.346)
    assert meta.video_codec == "h264"
    assert meta.has_audio and meta.audio_codec == "aac"
    assert meta.sample_rate == 48000 and meta.channels == 2
    assert meta.resolution_text == "1920x1080"


def test_rotation_swaps_display_size():
    data = {"streams": [dict(PROBE["streams"][0], side_data_list=[{"rotation": -90}])], "format": PROBE["format"]}
    meta = parse_probe_output(data)
    assert meta.rotation == 270
    assert meta.resolution_text == "1080x1920"
    data["streams"][0] = dict(PROBE["streams"][0], tags={"rotate": "90"})
    assert parse_probe_output(data).resolution_text == "1080x1920"


def test_no_video_stream():
    with pytest.raises(MediaError):
        parse_probe_output({"streams": [PROBE["streams"][1]], "format": {}})


def test_cover_art_is_not_video():
    art = {"codec_type": "video", "codec_name": "mjpeg", "width": 500, "height": 500,
           "disposition": {"attached_pic": 1}}
    with pytest.raises(MediaError):
        parse_probe_output({"streams": [art, PROBE["streams"][1]], "format": {}})


def test_invalid_frame_rate_and_size():
    stream = dict(PROBE["streams"][0], avg_frame_rate="0/0", r_frame_rate="25/1")
    assert parse_probe_output({"streams": [stream], "format": {}}).fps == 25
    with pytest.raises(MediaError):
        parse_probe_output({"streams": [dict(stream, width=0)], "format": {}})


def test_metadata_round_trip():
    meta = parse_probe_output(PROBE)
    assert VideoMetadata.from_dict(meta.to_dict() | {"unknown_future_field": 1}) == meta


def test_formatting():
    assert format_duration(3725.25) == "1:02:05.2"
    assert format_duration(65.5) == "01:05.5"
    assert format_duration(None) == "-"
    assert format_size(512) == "512 B"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"
