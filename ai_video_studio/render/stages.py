"""Video stages for the render pipeline, rebuilt from a job payload."""

from __future__ import annotations

from pathlib import Path

from ai_video_studio.render.pipeline import VideoStage


def stage_from_payload(stage: dict | None, workdir: Path, keep_ranges: list[tuple[float, float]] | None
                       ) -> VideoStage | None:
    """``stage`` is None (no resizing) or a dict with a ``type``; see the stage builders below."""
    if not stage:
        return None
    kind = stage.get("type")
    if kind == "scale":  # fast low-resolution preview
        height = int(stage["height"])
        return lambda src, dst: f"[{src}]scale=-2:{height}[{dst}]"
    raise ValueError(f"Unknown video stage {kind!r}")
