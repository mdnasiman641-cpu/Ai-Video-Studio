"""Locating FFmpeg/FFprobe and reading video metadata."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

from ai_video_studio.core.errors import DependencyMissingError, MediaError
from ai_video_studio.core.paths import app_root, exe_dir
from ai_video_studio.core.process import run_process

log = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 60


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def find_tool(name: str, configured_dir: str | None = None) -> Path | None:
    """Find ``ffmpeg`` or ``ffprobe``.

    Search order: the directory configured in Settings, a ``bin/`` or ``ffmpeg/bin/``
    folder shipped next to the application (the packaged build puts FFmpeg in
    ``<install folder>\\bin``), then ``PATH``.
    """
    candidates: list[Path] = []
    if configured_dir:
        candidates.append(Path(configured_dir) / _exe(name))
    for root in dict.fromkeys((exe_dir(), app_root())):
        candidates += [root / "bin" / _exe(name), root / "ffmpeg" / "bin" / _exe(name)]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def require_tool(name: str, configured_dir: str | None = None) -> Path:
    path = find_tool(name, configured_dir)
    if path is None:
        raise DependencyMissingError(
            f"{name} was not found. The installer and portable package include FFmpeg in the 'bin' "
            f"folder next to AI-Video-Studio.exe; if it was removed, install FFmpeg (for example "
            f"'winget install Gyan.FFmpeg') or choose the folder containing {name}.exe in Settings → FFmpeg."
        )
    return path


def tool_version(path: Path) -> str | None:
    try:
        result = run_process([str(path), "-version"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout.splitlines()[0].strip()


@dataclass
class VideoMetadata:
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    pixel_format: str | None = None
    rotation: int = 0
    bit_rate: int | None = None
    format_name: str | None = None
    file_size: int | None = None
    has_audio: bool = False
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def display_width(self) -> int:
        return self.height if self.rotation % 180 else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.rotation % 180 else self.height

    @property
    def resolution_text(self) -> str:
        return f"{self.display_width}x{self.display_height}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VideoMetadata":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _parse_rate(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rotation(stream: dict) -> int:
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        rot = _to_int(tags["rotate"]) or 0
    else:
        rot = 0
        for side in stream.get("side_data_list") or []:
            if "rotation" in side:
                rot = _to_int(side["rotation"]) or 0
                break
    return rot % 360


def parse_probe_output(data: dict) -> VideoMetadata:
    """Convert ``ffprobe -show_format -show_streams`` JSON into :class:`VideoMetadata`."""
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise MediaError("The file does not contain a video stream.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width = _to_int(video.get("width")) or 0
    height = _to_int(video.get("height")) or 0
    if width <= 0 or height <= 0:
        raise MediaError("The video stream has no valid frame size.")

    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    duration = _to_float(fmt.get("duration")) or _to_float(video.get("duration")) or 0.0

    return VideoMetadata(
        duration=round(duration, 3),
        width=width,
        height=height,
        fps=round(fps, 3),
        video_codec=video.get("codec_name") or "unknown",
        pixel_format=video.get("pix_fmt"),
        rotation=_rotation(video),
        bit_rate=_to_int(fmt.get("bit_rate")),
        format_name=fmt.get("format_name"),
        file_size=_to_int(fmt.get("size")),
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=_to_int(audio.get("sample_rate")) if audio else None,
        channels=_to_int(audio.get("channels")) if audio else None,
    )


def probe_video(
    path: str | os.PathLike,
    ffprobe: Path,
    cancel_event: threading.Event | None = None,
) -> VideoMetadata:
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"File not found: {path}")
    args = [
        str(ffprobe),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = run_process(args, timeout=PROBE_TIMEOUT_SECONDS, cancel_event=cancel_event)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"FFprobe timed out reading {path.name}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise MediaError(f"FFprobe could not read {path.name}: {detail}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"FFprobe returned invalid output for {path.name}") from exc
    meta = parse_probe_output(data)
    log.info("Probed video", extra={"path": str(path), "resolution": meta.resolution_text,
                                    "duration": meta.duration, "codec": meta.video_codec})
    return meta
