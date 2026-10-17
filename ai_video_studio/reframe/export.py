"""FFmpeg filter graph for a crop that moves over time.

FFmpeg's ``crop`` filter fixes its size when the graph starts but accepts new ``x``/``y``
values through commands. A ``sendcmd`` script generated from the crop path sends the
position for every output frame (only when it changes by at least one pixel), so the
exported crop follows the tracking result frame by frame. Crop, scale and subtitles run
in one filter graph, so timestamps, frame rate and audio sync are untouched.
"""

from __future__ import annotations

import math
from pathlib import Path

from ai_video_studio.core.ffmpeg_commands import EncoderSettings, check_filter_filename, encode_args, subtitle_filter
from ai_video_studio.reframe.framing import CropPath

CROP_COMMAND_FILE = "reframe.cmd"
MIN_COMMAND_FPS = 24.0
MAX_COMMAND_FPS = 120.0


def command_fps(source_fps: float | None) -> float:
    """Rate at which crop positions are sent: the source frame rate, within sensible limits.

    For variable-frame-rate sources a rate of at least 24/s still updates the crop for
    nearly every frame; positions between keyframes are interpolated anyway.
    """
    if not source_fps or source_fps <= 0 or math.isnan(source_fps):
        return 30.0
    return min(MAX_COMMAND_FPS, max(MIN_COMMAND_FPS, source_fps))


def sendcmd_script(path: CropPath, fps: float, duration: float) -> str:
    """``sendcmd`` script with one ``crop x, y`` command per frame where the position changes.

    Each command is timed half a frame early so it applies to the frame it was computed
    for, regardless of small timestamp rounding.
    """
    fps = command_fps(fps)
    step = 1.0 / fps
    count = int(math.ceil(max(0.0, duration) * fps)) + 1
    lines = []
    last = None
    for k in range(count):
        t = k * step
        x, y = path.at(t)
        if (x, y) != last:
            lines.append(f"{max(0.0, t - step / 2):.4f} crop x {x}, crop y {y};")
            last = (x, y)
    return "\n".join(lines) + "\n"


def reframe_filter(path: CropPath, out_w: int, out_h: int, subtitle_file: str | None = None,
                   command_file: str | None = CROP_COMMAND_FILE) -> str:
    """Filter chain: [sendcmd →] crop → scale to the output size → [burn in subtitles].

    ``command_file`` is ``None`` for a crop that does not move. Subtitles are rendered
    after scaling, on the final output frame, so they match the preview and are sharp.
    """
    x0, y0 = path.at(0.0)
    parts = []
    if command_file is not None:
        check_filter_filename(command_file)
        parts.append(f"sendcmd=f={command_file}")
    parts.append(f"crop=w={path.crop_w}:h={path.crop_h}:x={x0}:y={y0}")
    parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
    parts.append("setsar=1")
    if subtitle_file:
        check_filter_filename(subtitle_file)
        parts.append(subtitle_filter(subtitle_file))
    return ",".join(parts)


def reframe_args(ffmpeg: Path | str, source: str, video_filter: str, output: str, encoder: EncoderSettings,
                 source_audio_codec: str | None, has_audio: bool) -> list[str]:
    return encode_args(ffmpeg, source, video_filter, output, encoder, source_audio_codec, has_audio)


def reframe_frame_args(ffmpeg: Path | str, source: str, at_seconds: float, path: CropPath, out_w: int, out_h: int,
                       subtitle_file: str | None, output_png: str) -> list[str]:
    """Render the single output frame at ``at_seconds`` exactly as the export will (crop, scale, subtitles)."""
    x, y = path.at(at_seconds)
    vf = [f"crop=w={path.crop_w}:h={path.crop_h}:x={x}:y={y}", f"scale={out_w}:{out_h}:flags=lanczos", "setsar=1"]
    if subtitle_file:
        check_filter_filename(subtitle_file)
        vf.append(subtitle_filter(subtitle_file))
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-ss", f"{max(0.0, at_seconds):.3f}",
        "-vf", ",".join(vf),
        "-frames:v", "1", str(output_png),
    ]
