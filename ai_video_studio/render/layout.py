"""Resize & Crop geometry: one calculation for the preview and the FFmpeg export.

Every mode is expressed the same way: a rectangle of the **source** (``src_*``) is scaled into
a rectangle of the **output canvas** (``dst_*``); the rest of the canvas is a solid colour or
a blurred copy of the video. The preview draws exactly these rectangles and the export filter
is generated from the same :class:`Layout`, so they cannot disagree.

All rectangles use even integers: H.264/H.265 in 4:2:0 need even frame sizes, and even
offsets keep the colour planes aligned.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ai_video_studio.core.errors import StudioError

PRESETS: dict[str, tuple[str, tuple[int, int] | None]] = {
    "original": ("Original resolution", None),
    "1920x1080": ("1920×1080 — 16:9 landscape", (1920, 1080)),
    "1080x1920": ("1080×1920 — 9:16 vertical", (1080, 1920)),
    "1080x1080": ("1080×1080 — 1:1 square", (1080, 1080)),
    "1080x1350": ("1080×1350 — 4:5 portrait", (1080, 1350)),
    "720x1280": ("720×1280 — 9:16 vertical", (720, 1280)),
    "1280x720": ("1280×720 — 16:9 landscape", (1280, 720)),
    "custom": ("Custom size", None),
}
MODES = {
    "fit": "Fit — show the whole frame, pad the rest",
    "fill": "Fill — fill the canvas, crop the excess",
    "crop": "Crop — choose a region",
    "blur": "Blur background — whole frame over a blurred copy",
    "custom": "Custom position and size",
    "reframe": "Auto Reframe — follow faces (uses the Auto Reframe analysis)",
}
MIN_SIZE, MAX_W, MAX_H = 64, 7680, 4320
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
ASPECT_NAMES = {(16, 9): "16:9", (9, 16): "9:16", (1, 1): "1:1", (4, 5): "4:5", (5, 4): "5:4", (4, 3): "4:3",
                (3, 4): "3:4", (21, 9): "21:9", (2, 1): "2:1"}


class LayoutError(StudioError):
    pass


def even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def even_floor(value: float) -> int:
    return max(0, int(value) // 2 * 2)


def aspect_text(w: int, h: int) -> str:
    from math import gcd

    g = gcd(w, h) or 1
    a, b = w // g, h // g
    if (a, b) in ASPECT_NAMES:
        return ASPECT_NAMES[(a, b)]
    for (x, y), name in ASPECT_NAMES.items():
        if abs(w / h - x / y) < 0.01:
            return f"≈{name}"
    return f"{w / h:.3f}:1"


@dataclass
class ResizeSettings:
    preset: str = "1080x1920"
    width: int = 1080          # custom size
    height: int = 1920
    mode: str = "blur"
    pos_x: float = 0.5         # fill: which part stays visible (0 left … 1 right); custom: centre on the canvas
    pos_y: float = 0.5
    zoom: float = 1.0          # custom: size relative to Fit
    crop_x: float = 0.0        # crop: region of the source (fractions of width/height)
    crop_y: float = 0.0
    crop_w: float = 1.0
    crop_h: float = 1.0
    lock_aspect: bool = True   # crop: region keeps the output aspect ratio (no padding)
    background: str = "#000000"
    blur: int = 20
    burn_captions: bool = False
    use_cuts: bool = True      # apply the Smart Cut cuts, if any
    enabled: bool = True       # Edit page final export: resize/crop (or Auto Reframe) is switched on
    mode_before_reframe: str = ""  # the fixed mode to return to when Auto Reframe is switched off
    burn_set: bool = False     # burn-in was switched on/off by the user (otherwise: on when captions exist)

    def validate(self) -> None:
        errors = []
        if self.preset not in PRESETS:
            errors.append(f"Unknown size preset '{self.preset}'")
        if self.preset == "custom":
            for name, value, limit in (("Width", self.width, MAX_W), ("Height", self.height, MAX_H)):
                if not MIN_SIZE <= int(value) <= limit:
                    errors.append(f"{name} must be between {MIN_SIZE} and {limit} pixels")
                elif int(value) % 2:
                    errors.append(f"{name} must be an even number (required by H.264/H.265 in 4:2:0)")
        if self.mode not in MODES:
            errors.append(f"Unknown mode '{self.mode}'")
        if not (0 <= self.pos_x <= 1 and 0 <= self.pos_y <= 1):
            errors.append("Position must be between 0 and 100 %")
        if not 0.1 <= self.zoom <= 4:
            errors.append("Size must be between 10 % and 400 % of Fit")
        if not (0 <= self.crop_x < 1 and 0 <= self.crop_y < 1 and 0 < self.crop_w <= 1 and 0 < self.crop_h <= 1
                and self.crop_x + self.crop_w <= 1.0001 and self.crop_y + self.crop_h <= 1.0001):
            errors.append("The crop region must lie inside the video")
        if not HEX_COLOR.match(self.background or ""):
            errors.append("Background colour must look like #RRGGBB")
        if not 1 <= int(self.blur) <= 60:
            errors.append("Blur strength must be between 1 and 60")
        if errors:
            raise LayoutError("; ".join(errors))

    def output_size(self, src_w: int, src_h: int) -> tuple[int, int]:
        if self.preset == "custom":
            return int(self.width), int(self.height)
        size = PRESETS[self.preset][1]
        return size if size else (max(2, even_floor(src_w)), max(2, even_floor(src_h)))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ResizeSettings":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass(frozen=True)
class Layout:
    out_w: int
    out_h: int
    src_x: int
    src_y: int
    src_w: int
    src_h: int
    dst_x: int
    dst_y: int
    dst_w: int
    dst_h: int
    background: str     # none | color | blur
    color: str = "#000000"
    blur: int = 20

    @property
    def scale(self) -> float:
        return self.dst_w / self.src_w

    @property
    def upscaled(self) -> bool:
        return self.scale > 1.01

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Layout":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _fit(w: float, h: float, box_w: int, box_h: int) -> tuple[int, int]:
    k = min(box_w / w, box_h / h)
    return min(even(w * k), box_w), min(even(h * k), box_h)


def _centered(dw: int, dh: int, out_w: int, out_h: int) -> tuple[int, int]:
    return even_floor((out_w - dw) / 2), even_floor((out_h - dh) / 2)


def _clamp_rect(x: float, y: float, w: float, h: float, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    w, h = min(even(w), even_floor(src_w) or 2), min(even(h), even_floor(src_h) or 2)
    x = min(max(0, even_floor(x)), even_floor(src_w - w))
    y = min(max(0, even_floor(y)), even_floor(src_h - h))
    return x, y, w, h


def compute_layout(src_w: int, src_h: int, s: ResizeSettings) -> Layout:
    """The layout for a source of ``src_w``×``src_h`` display pixels (not used for mode "reframe")."""
    s.validate()
    if src_w < 2 or src_h < 2:
        raise LayoutError("The video has no valid frame size.")
    out_w, out_h = s.output_size(src_w, src_h)
    bg = {"blur": "blur"}.get(s.mode, "color")
    full = (0, 0, even_floor(src_w), even_floor(src_h))

    if s.mode in ("fit", "blur", "reframe"):
        sx, sy, sw, sh = full
        dw, dh = _fit(sw, sh, out_w, out_h)
        dx, dy = _centered(dw, dh, out_w, out_h)
    elif s.mode == "fill":
        k = max(out_w / src_w, out_h / src_h)
        sw, sh = out_w / k, out_h / k
        sx, sy, sw, sh = _clamp_rect((src_w - sw) * s.pos_x, (src_h - sh) * s.pos_y, sw, sh, src_w, src_h)
        dx, dy, dw, dh = 0, 0, out_w, out_h
    elif s.mode == "crop":
        x, y, w, h = s.crop_x * src_w, s.crop_y * src_h, s.crop_w * src_w, s.crop_h * src_h
        if s.lock_aspect:
            target = out_w / out_h
            cx, cy = x + w / 2, y + h / 2
            if w / h > target:
                w = h * target
            else:
                h = w / target
            x, y = cx - w / 2, cy - h / 2
        sx, sy, sw, sh = _clamp_rect(x, y, w, h, src_w, src_h)
        dw, dh = _fit(sw, sh, out_w, out_h)
        dx, dy = _centered(dw, dh, out_w, out_h)
    else:  # custom: size relative to Fit, centred at (pos_x, pos_y) of the canvas
        k = min(out_w / src_w, out_h / src_h) * s.zoom
        pw, ph = src_w * k, src_h * k
        px, py = s.pos_x * out_w - pw / 2, s.pos_y * out_h - ph / 2
        vx0, vy0 = max(0.0, px), max(0.0, py)
        vx1, vy1 = min(float(out_w), px + pw), min(float(out_h), py + ph)
        if vx1 - vx0 < 2 or vy1 - vy0 < 2:
            raise LayoutError("The video is positioned completely outside the output canvas.")
        sx, sy, sw, sh = _clamp_rect((vx0 - px) / k, (vy0 - py) / k, (vx1 - vx0) / k, (vy1 - vy0) / k, src_w, src_h)
        dx, dy = even_floor(vx0), even_floor(vy0)
        dw, dh = min(even(vx1 - vx0), out_w - dx), min(even(vy1 - vy0), out_h - dy)
    covers = dx == 0 and dy == 0 and dw == out_w and dh == out_h
    return Layout(out_w, out_h, sx, sy, sw, sh, dx, dy, dw, dh, "none" if covers else bg, s.background, int(s.blur))


def layout_notes(layout: Layout, src_w: int, src_h: int) -> list[str]:
    notes = []
    if layout.upscaled:
        notes.append(f"The video is enlarged {layout.scale:.2f}×; it will look softer than the source.")
    if layout.out_w > 4096 or layout.out_h > 4096:
        notes.append("Sizes above 4096 pixels are beyond NVENC H.264; the CPU encoder is used if NVENC fails.")
    return notes


def layout_filter(layout: Layout, src: str, dst: str) -> str:
    """Filter graph text from ``[src]`` to ``[dst]`` for this layout."""
    L = layout
    fg = (f"crop={L.src_w}:{L.src_h}:{L.src_x}:{L.src_y},"
          f"scale={L.dst_w}:{L.dst_h}:flags=lanczos,setsar=1")
    if L.background == "none":
        return f"[{src}]{fg}[{dst}]"
    if L.background == "blur":
        radius = max(1, min(int(L.blur), min(L.out_w, L.out_h) // 4 - 1))
        return (f"[{src}]split=2[lfg][lbg];"
                f"[lbg]scale={L.out_w}:{L.out_h}:force_original_aspect_ratio=increase:flags=bilinear,"
                f"crop={L.out_w}:{L.out_h},boxblur=luma_radius={radius}:luma_power=2,setsar=1[lbgb];"
                f"[lfg]{fg}[lfgs];[lbgb][lfgs]overlay=x={L.dst_x}:y={L.dst_y}[{dst}]")
    color = "0x" + L.color.lstrip("#")
    return f"[{src}]{fg},pad={L.out_w}:{L.out_h}:{L.dst_x}:{L.dst_y}:color={color}[{dst}]"


def frame_filter(layout: Layout) -> str:
    """The same processing as a ``-filter_complex`` for one input (single-frame previews)."""
    return layout_filter(layout, "0:v:0", "vout")
