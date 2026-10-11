"""One FFmpeg command for an edited export: cuts → video processing → subtitles → encode.

**Cuts** use ``trim``/``atrim`` for every kept range and the ``concat`` filter to join them.
This decodes and re-encodes, which is required for frame-accurate cuts (copying streams can
only cut at keyframes). Cut points are snapped to the frame grid first, so each kept video
part and its audio have the same length and audio/video stay in sync across many cuts; the
``concat`` filter additionally pads audio with silence if a part is a fraction of a frame
shorter. Timestamps inside the graph start at the beginning of the file (FFmpeg's default
input offset), the same time base the preview player and the audio analysis use.

The **video stage** (resize layout or moving reframe crop) runs on the joined timeline, and
**subtitles** are rendered last, on the final canvas, from captions already remapped to the
edited timeline. Long graphs are written to a script file (``-filter_complex_script``) to stay
below the Windows command-line length limit.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

from ai_video_studio.core.ffmpeg_commands import EncoderSettings, check_filter_filename
from ai_video_studio.render.output import OutputSettings

# video_stage(input_label, output_label) -> filter graph text (may contain several chains)
VideoStage = Callable[[str, str], str]

GRAPH_FILE = "graph.txt"
INLINE_GRAPH_LIMIT = 6000  # characters; longer graphs go to GRAPH_FILE
MIN_RANGE = 0.04           # seconds; shorter kept parts are dropped


def snap_ranges(ranges: list[tuple[float, float]], fps: float | None, duration: float | None = None
                ) -> list[tuple[float, float]]:
    """Sort, clamp to the clip, snap to the frame grid and merge touching ranges."""
    grid = fps if fps and fps > 0 and not math.isnan(fps) else None
    end_limit = duration if duration and duration > 0 else None
    snapped = []
    for start, end in sorted((float(a), float(b)) for a, b in ranges):
        start, end = max(0.0, start), end if end_limit is None else min(end, end_limit)
        if grid:
            start = math.floor(start * grid + 0.5) / grid  # nearest frame start (halves round up)
            end = math.floor(end * grid + 0.5) / grid
            if end_limit is not None:
                end = min(end, end_limit)
        if end - start < MIN_RANGE:
            continue
        if snapped and start <= snapped[-1][1] + 1e-6:
            snapped[-1] = (snapped[-1][0], max(snapped[-1][1], end))
        else:
            snapped.append((round(start, 6), round(end, 6)))
    return snapped


def kept_duration(ranges: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in ranges)


def is_full_clip(ranges: list[tuple[float, float]] | None, duration: float | None) -> bool:
    """True when the ranges keep the whole clip (no real cut)."""
    if not ranges:
        return True
    if not duration:
        return False
    return len(ranges) == 1 and ranges[0][0] <= 0.001 and ranges[0][1] >= duration - 0.001


def audio_plan(settings: OutputSettings, has_audio: bool, source_codec: str | None, cutting: bool) -> tuple[str, list[str]]:
    """``(mode, notes)``; mode is none | copy | aac | filtered | all_tracks."""
    if not has_audio or settings.audio == "none":
        return "none", ([] if has_audio else ["The source has no audio track."])
    if cutting:
        notes = ["Audio is re-encoded to AAC because the video is cut."]
        if settings.all_audio_tracks:
            notes.append("Only the first audio track is kept when cutting.")
        return "filtered", notes
    if settings.all_audio_tracks:
        return "all_tracks", ["All audio tracks are kept and converted to AAC."]
    if settings.audio == "auto" and source_codec == "aac":
        return "copy", []
    return "aac", []


def build_filter_graph(
    *,
    keep_ranges: list[tuple[float, float]] | None,
    has_audio: bool,
    video_stage: VideoStage | None = None,
    subtitle_file: str | None = None,
    target_fps: float | None = None,
) -> tuple[str, str, str | None]:
    """Return ``(graph, video_label, audio_label)``; ``audio_label`` is None when audio is not filtered."""
    chains: list[str] = []
    video, audio = "0:v:0", None
    if keep_ranges:
        n = len(keep_ranges)
        chains.append(f"[0:v:0]split={n}" + "".join(f"[vs{i}]" for i in range(n)) if n > 1 else "[0:v:0]null[vs0]")
        if has_audio:
            chains.append(f"[0:a:0]asplit={n}" + "".join(f"[as{i}]" for i in range(n)) if n > 1
                          else "[0:a:0]anull[as0]")
        joined = ""
        for i, (start, end) in enumerate(keep_ranges):
            chains.append(f"[vs{i}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]")
            joined += f"[v{i}]"
            if has_audio:
                chains.append(f"[as{i}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]")
                joined += f"[a{i}]"
        outputs = "[vcut][acut]" if has_audio else "[vcut]"
        chains.append(f"{joined}concat=n={n}:v=1:a={1 if has_audio else 0}{outputs}")
        video, audio = "vcut", ("acut" if has_audio else None)
    if video_stage is not None:
        chains.append(video_stage(video, "vstage"))
        video = "vstage"
    tail = []
    if target_fps:
        tail.append(f"fps={target_fps:g}")
    if subtitle_file:
        check_filter_filename(subtitle_file)
        tail.append(f"subtitles={subtitle_file}")
    tail.append("setsar=1")
    chains.append(f"[{video}]" + ",".join(tail) + "[vout]")
    return ";".join(chains), "vout", audio


def render_args(
    ffmpeg: Path | str,
    source: str,
    graph: str,
    audio_label: str | None,
    audio_mode: str,
    encoder: EncoderSettings,
    output: str,
    workdir: Path | None = None,
) -> list[str]:
    """The FFmpeg command. A long ``graph`` is written to ``workdir/graph.txt``."""
    args = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-i", str(source)]
    if len(graph) > INLINE_GRAPH_LIMIT and workdir is not None:
        (Path(workdir) / GRAPH_FILE).write_text(graph, encoding="utf-8")
        args += ["-filter_complex_script", GRAPH_FILE]
    else:
        args += ["-filter_complex", graph]
    args += ["-map", "[vout]"]
    if audio_mode == "filtered":
        args += ["-map", f"[{audio_label}]"]
    elif audio_mode in ("copy", "aac"):
        args += ["-map", "0:a:0?"]
    elif audio_mode == "all_tracks":
        args += ["-map", "0:a?"]
    args += encoder.video_args()
    if audio_mode == "copy":
        args += ["-c:a", "copy"]
    elif audio_mode != "none":
        args += ["-c:a", "aac", "-b:a", encoder.audio_bitrate]
    args += ["-map_metadata", "0", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
    return args
