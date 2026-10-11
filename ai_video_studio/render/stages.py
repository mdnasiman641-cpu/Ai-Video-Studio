"""Video stages for the render pipeline, rebuilt from a job payload.

``{"type": "layout", "layout": {...}}``: Resize & Crop (static geometry, see ``layout``).
``{"type": "reframe", ...crop path...}``: the moving Auto Reframe crop, then scaling to the
output size. With cuts, the crop keyframes are on the source timeline while the stage runs
after the cuts are joined, so each output time is mapped back to its source time.
``{"type": "scale", "height": h}``: small fast previews.
"""

from __future__ import annotations

import math
from pathlib import Path

from ai_video_studio.reframe.export import CROP_COMMAND_FILE, command_fps
from ai_video_studio.reframe.framing import CropPath
from ai_video_studio.render.layout import Layout, layout_filter
from ai_video_studio.render.pipeline import VideoStage


def source_time_mapper(keep_ranges: list[tuple[float, float]] | None):
    """Function from edited-timeline time to source time."""
    if not keep_ranges:
        return lambda t: t
    starts, total = [], 0.0
    for a, b in keep_ranges:
        starts.append(total)
        total += b - a

    def to_source(t: float) -> float:
        for (a, b), offset in zip(keep_ranges, starts):
            if t < offset + (b - a):
                return a + max(0.0, t - offset)
        return keep_ranges[-1][1]

    return to_source


def mapped_sendcmd_script(path: CropPath, fps: float | None, duration: float, to_source) -> str:
    """Like ``reframe.export.sendcmd_script``, on the edited timeline."""
    rate = command_fps(fps)
    step = 1.0 / rate
    lines, last = [], None
    for k in range(int(math.ceil(max(0.0, duration) * rate)) + 1):
        t = k * step
        x, y = path.at(to_source(t))
        if (x, y) != last:
            lines.append(f"{max(0.0, t - step / 2):.4f} crop x {x}, crop y {y};")
            last = (x, y)
    return "\n".join(lines) + "\n"


def stage_from_payload(stage: dict | None, workdir: Path, keep_ranges: list[tuple[float, float]] | None
                       ) -> VideoStage | None:
    if not stage:
        return None
    kind = stage.get("type")
    if kind == "scale":
        height = int(stage["height"])
        return lambda src, dst: f"[{src}]scale=-2:{height}[{dst}]"
    if kind == "layout":
        layout = Layout.from_dict(stage["layout"])
        return lambda src, dst: layout_filter(layout, src, dst)
    if kind == "reframe":
        path = CropPath.from_keyframes(int(stage["src_w"]), int(stage["src_h"]), int(stage["crop_w"]),
                                       int(stage["crop_h"]), stage["keyframes"])
        out_w, out_h = int(stage["out_w"]), int(stage["out_h"])
        to_source = source_time_mapper(keep_ranges)
        duration = sum(b - a for a, b in keep_ranges) if keep_ranges else float(stage.get("duration") or 0)
        x0, y0 = path.at(to_source(0.0))
        moving = not path.is_static
        if moving:
            script = mapped_sendcmd_script(path, stage.get("fps"), duration, to_source)
            (Path(workdir) / CROP_COMMAND_FILE).write_text(script, encoding="utf-8")
        prefix = f"sendcmd=f={CROP_COMMAND_FILE}," if moving else ""
        return lambda src, dst: (f"[{src}]{prefix}crop=w={path.crop_w}:h={path.crop_h}:x={x0}:y={y0},"
                                 f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[{dst}]")
    raise ValueError(f"Unknown video stage {kind!r}")
