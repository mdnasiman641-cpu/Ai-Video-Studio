"""Colour grading and video filters for an export (no Qt).

:class:`ColorSettings` holds a colour grade (a built-in look or the user's own values) and an
optional video filter with an intensity. Both are expressed in the same set of adjustments
(:data:`ADJUSTMENTS`): the grade's values plus the filter's values scaled by its intensity are
added together and turned into **one** chain of FFmpeg filters, so the same adjustment is never
applied twice and a filter at 0 % adds nothing.

How the adjustments are rendered (FFmpeg filters whose behaviour has been stable for years):

* exposure, contrast, saturation, gamma, black & white amount → ``eq``
* temperature, tint and teal & orange split toning → per-channel ``curves`` (warm = more red and
  less blue in the mid tones; split toning moves the shadows towards teal and the highlights
  towards orange, leaving the mid tones, where skin usually is, least changed)
* highlights, shadows and fade (lifted blacks) → the ``curves`` master curve
* sepia → ``colorchannelmixer`` (the matrix is blended with the identity by its amount)
* soft glow → a blurred copy (``gblur``) screened over the picture (``blend``)
* vignette → ``vignette``; sharpening → ``unsharp`` (luma only)

The chain runs after the framing stage (resize or reframe) and before speed and captions, so
captions are never graded. The output size and aspect ratio are not changed.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from ai_video_studio.core.errors import StudioError

# name -> (label, minimum, maximum); every value is 0 = unchanged
ADJUSTMENTS: dict[str, tuple[str, float, float]] = {
    "exposure": ("Exposure / Brightness", -100, 100),
    "contrast": ("Contrast", -100, 100),
    "saturation": ("Saturation", -100, 100),
    "temperature": ("Temperature", -100, 100),
    "tint": ("Tint", -100, 100),
    "highlights": ("Highlights", -100, 100),
    "shadows": ("Shadows", -100, 100),
    "gamma": ("Gamma (mid tones)", -100, 100),
    "fade": ("Fade", 0, 100),
    "vignette": ("Vignette", 0, 100),
    "sharpen": ("Sharpening", 0, 100),
    "split_tone": ("Teal & Orange split", 0, 100),
    "mono": ("Black & White", 0, 100),
    "sepia": ("Sepia", 0, 100),
    "glow": ("Soft glow", 0, 100),
}
# shown as manual sliders; mono/sepia/glow are only used by looks and filters
MANUAL = ("exposure", "contrast", "saturation", "temperature", "tint", "highlights", "shadows", "gamma", "fade",
          "vignette", "sharpen", "split_tone")


def _a(**values: float) -> dict[str, float]:
    return {k: float(v) for k, v in values.items()}


# Colour grading looks: label and adjustment values (the sliders start from these)
GRADE_PRESETS: dict[str, tuple[str, dict[str, float]]] = {
    "original": ("Original", {}),
    "natural": ("Natural", _a(contrast=4, saturation=4)),
    "cinematic": ("Cinematic", _a(contrast=18, saturation=-6, highlights=-20, shadows=-6, split_tone=15,
                                  vignette=15)),
    "teal_orange": ("Teal & Orange", _a(contrast=10, saturation=6, highlights=-8, split_tone=60)),
    "warm_film": ("Warm Film", _a(temperature=35, contrast=-10, fade=15, saturation=-5, highlights=-5)),
    "cool_cinematic": ("Cool Cinematic", _a(temperature=-30, contrast=12, saturation=-10, highlights=-10)),
    "vintage_film": ("Vintage Film", _a(contrast=-15, fade=35, saturation=-25, temperature=15, vignette=20)),
    "moody_dark": ("Moody Dark", _a(exposure=-12, contrast=15, shadows=-25, highlights=-15, saturation=-20,
                                    vignette=30)),
    "bright_clean": ("Bright & Clean", _a(exposure=10, gamma=15, contrast=5, saturation=8, shadows=10)),
    "black_white": ("Black & White", _a(mono=100, contrast=20)),
    "custom": ("Custom", {}),
}

# Video filters: label and adjustment values at 100 % intensity
FILTERS: dict[str, tuple[str, dict[str, float]]] = {
    "none": ("Original / None", {}),
    "cinematic": ("Cinematic", _a(contrast=15, saturation=-10, split_tone=25, vignette=15)),
    "warm": ("Warm", _a(temperature=40, saturation=5)),
    "cool": ("Cool", _a(temperature=-40)),
    "vintage": ("Vintage", _a(fade=30, sepia=30, contrast=-10, saturation=-20, vignette=20)),
    "retro": ("Retro", _a(saturation=25, contrast=15, fade=20, temperature=20, tint=10)),
    "black_white": ("Black & White", _a(mono=100)),
    "sepia": ("Sepia", _a(sepia=100)),
    "fade": ("Fade", _a(fade=45, contrast=-15, saturation=-15)),
    "high_contrast": ("High Contrast", _a(contrast=45, shadows=-15, highlights=10)),
    "soft_contrast": ("Soft Contrast", _a(contrast=-30, shadows=15, highlights=-15)),
    "vibrant": ("Vibrant", _a(saturation=45, contrast=5)),
    "muted": ("Muted Colors", _a(saturation=-45, contrast=-5)),
    "dreamy": ("Dreamy Soft", _a(glow=70, exposure=5, contrast=-10, fade=10)),
    "sharp_clear": ("Sharp & Clear", _a(sharpen=70, contrast=10, saturation=5)),
}

SEPIA = (0.393, 0.769, 0.189, 0.349, 0.686, 0.168, 0.272, 0.534, 0.131)
IDENTITY = (1, 0, 0, 0, 1, 0, 0, 0, 1)


class ColorError(StudioError):
    pass


def _clamp(name: str, value: float) -> float:
    _label, low, high = ADJUSTMENTS[name]
    return max(low, min(high, value))


@dataclass
class ColorSettings:
    grading: bool = False                 # the Color Grading switch
    preset: str = "original"              # the look the values came from ("custom" after manual changes)
    values: dict[str, float] = field(default_factory=dict)  # the grade's adjustments (missing = 0)
    filter: str = "none"                  # the Video Filters choice
    intensity: int = 100                  # percent
    filtering: bool = False               # the Video Filters switch
    name: str = ""                        # the user's preset the values came from (shown to the user)

    # -- validation ---------------------------------------------------------------
    def validate(self) -> None:
        errors = []
        if self.preset not in GRADE_PRESETS:
            errors.append(f"Unknown colour grade '{self.preset}'")
        if self.filter not in FILTERS:
            errors.append(f"Unknown video filter '{self.filter}'")
        if len(self.name) > 60:
            errors.append("The colour preset name is too long")
        if not isinstance(self.intensity, int) or not 0 <= self.intensity <= 100:
            errors.append("Filter intensity must be between 0% and 100%")
        for name, value in self.values.items():
            if name not in ADJUSTMENTS:
                errors.append(f"Unknown colour adjustment '{name}'")
                continue
            label, low, high = ADJUSTMENTS[name]
            if not isinstance(value, (int, float)) or math.isnan(value) or not low <= value <= high:
                errors.append(f"{label} must be between {low:g} and {high:g}")
        if errors:
            raise ColorError("; ".join(errors))

    # -- what is applied ------------------------------------------------------------
    @property
    def grade_active(self) -> bool:
        return self.grading and any(abs(v) > 1e-9 for v in self.values.values())

    @property
    def filter_active(self) -> bool:
        return self.filtering and self.filter != "none" and self.intensity > 0

    @property
    def active(self) -> bool:
        return self.grade_active or self.filter_active

    def combined(self) -> dict[str, float]:
        """The grade plus the filter at its intensity, each adjustment within its range."""
        total = {name: 0.0 for name in ADJUSTMENTS}
        if self.grading:
            for name, value in self.values.items():
                total[name] += float(value)
        if self.filter_active:
            for name, value in FILTERS[self.filter][1].items():
                total[name] += value * self.intensity / 100
        return {name: _clamp(name, value) for name, value in total.items()}

    def load_preset(self, preset: str) -> None:
        """Set the grade's values to a built-in look."""
        if preset not in GRADE_PRESETS:
            raise ColorError(f"Unknown colour grade '{preset}'")
        self.preset = preset
        if preset != "custom":
            self.values = dict(GRADE_PRESETS[preset][1])

    def summary(self) -> list[str]:
        parts = []
        if self.grade_active:
            parts.append(f"colour grade {self.name or GRADE_PRESETS[self.preset][0]}")
        if self.filter_active:
            parts.append(f"filter {FILTERS[self.filter][0]} {self.intensity}%")
        return parts

    # -- FFmpeg ---------------------------------------------------------------------
    def chain(self, height: int = 1080) -> list[str]:
        """The filters, in order; ``[]`` when nothing is applied. ``height`` sizes the glow blur."""
        self.validate()
        if not self.active:
            return []
        return color_filters(self.combined(), height)

    def graph(self, height: int = 1080):
        """A video stage ``(in_label, out_label) -> graph text`` or ``None`` (see ``render.pipeline``)."""
        filters = self.chain(height)
        if not filters:
            return None

        def stage(src: str, dst: str) -> str:
            return f"[{src}]" + ",".join(filters) + f"[{dst}]"

        return stage

    def required_filters(self) -> set[str]:
        names = set()
        for f in self.chain():
            for part in f.replace(";", ",").split(","):
                part = part.split("]")[-1]
                if part:
                    names.add(part.split("=", 1)[0])
        return names

    # -- serialisation --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"grading": self.grading, "preset": self.preset, "values": dict(self.values), "filter": self.filter,
                "intensity": self.intensity, "filtering": self.filtering, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> "ColorSettings":
        """Read stored settings; damaged data gives "no colour changes", or raises with ``strict``."""
        try:
            d = data or {}
            values = d.get("values") or {}
            if not isinstance(values, dict):
                raise ColorError("Colour adjustments must be a mapping")
            settings = cls(grading=bool(d.get("grading", False)), preset=str(d.get("preset", "original")),
                           values={str(k): float(v) for k, v in values.items()}, filter=str(d.get("filter", "none")),
                           intensity=int(d.get("intensity", 100)), filtering=bool(d.get("filtering", False)),
                           name=str(d.get("name", "")))
            settings.validate()
            return settings
        except (ColorError, TypeError, ValueError) as exc:
            if strict:
                raise ColorError(f"Invalid colour settings: {exc}") from None
            return cls()

    def copy(self) -> "ColorSettings":
        return ColorSettings.from_dict(self.to_dict())


def _points(offsets: dict[float, float]) -> str:
    xs = (0.0, 0.25, 0.5, 0.75, 1.0)
    ys, last = [], -1.0
    for x in xs:
        y = max(0.0, min(1.0, x + offsets.get(x, 0.0)))
        y = max(y, last + 0.001) if x > 0 else y  # keep the curve rising
        ys.append(min(1.0, y))
        last = ys[-1]
    return " ".join(f"{x:g}/{y:.4f}" for x, y in zip(xs, ys))


def color_filters(v: dict[str, float], height: int = 1080) -> list[str]:
    """FFmpeg filters for combined adjustment values (see :data:`ADJUSTMENTS`)."""
    n = {k: v.get(k, 0.0) / 100 for k in ADJUSTMENTS}  # -1..1 or 0..1
    out: list[str] = []
    if n["glow"] > 0:
        sigma = max(1.0, 12 * n["glow"] * height / 1080)
        out.append(f"split[cg0][cg1];[cg1]gblur=sigma={sigma:.2f}[cg2];"
                   f"[cg0][cg2]blend=all_mode=screen:all_opacity={0.55 * n['glow']:.3f}")
    t, tint, split = n["temperature"], n["tint"], n["split_tone"]
    channels = {
        "r": {0.25: 0.04 * t - 0.06 * split, 0.5: 0.06 * t, 0.75: 0.04 * t + 0.05 * split},
        "g": {0.25: -0.035 * tint + 0.02 * split, 0.5: -0.05 * tint, 0.75: -0.035 * tint},
        "b": {0.25: -0.04 * t + 0.06 * split, 0.5: -0.06 * t, 0.75: -0.04 * t - 0.06 * split},
    }
    master = {0.0: 0.12 * n["fade"], 0.25: 0.08 * n["shadows"], 0.75: 0.08 * n["highlights"], 1.0: -0.03 * n["fade"]}
    curves = [f"{c}='{_points(o)}'" for c, o in channels.items() if any(abs(x) > 1e-6 for x in o.values())]
    if any(abs(x) > 1e-6 for x in master.values()):
        curves.append(f"m='{_points(master)}'")
    if curves:
        out.append("curves=" + ":".join(curves))
    if n["sepia"] > 0:
        k = n["sepia"]
        m = [i + (s - i) * k for i, s in zip(IDENTITY, SEPIA)]
        names = ("rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb")
        out.append("colorchannelmixer=" + ":".join(f"{name}={value:.4f}" for name, value in zip(names, m)))
    eq = {}
    if n["exposure"]:
        eq["brightness"] = 0.3 * n["exposure"]
    if n["contrast"]:
        eq["contrast"] = 1 + 0.5 * n["contrast"]
    saturation = max(0.0, 1 + n["saturation"]) * (1 - n["mono"])
    if abs(saturation - 1) > 1e-6:
        eq["saturation"] = saturation
    if n["gamma"]:
        eq["gamma"] = 1 + 0.5 * n["gamma"]
    if eq:
        out.append("eq=" + ":".join(f"{k}={x:.4f}" for k, x in eq.items()))
    if n["vignette"] > 0:
        out.append(f"vignette=angle={0.75 * n['vignette']:.4f}")
    if n["sharpen"] > 0:
        out.append(f"unsharp=lx=5:ly=5:la={1.2 * n['sharpen']:.3f}:cx=3:cy=3:ca=0")
    return out


class ColorPresetStore:
    """The user's own colour looks, kept in the settings store (``color_presets``). Built-in looks
    live in code, so deleting every custom preset never removes them."""

    KEY = "color_presets"

    def __init__(self, settings):
        self._settings = settings

    def _all(self) -> dict[str, dict]:
        try:
            data = json.loads(self._settings.get(self.KEY) or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def names(self) -> list[str]:
        return sorted(self._all(), key=str.casefold)

    def get(self, name: str) -> dict[str, float]:
        data = self._all()
        if name not in data:
            raise ColorError(f"The colour preset '{name}' does not exist.")
        return dict(data[name])

    def save(self, name: str, values: dict[str, float]) -> str:
        name = name.strip()
        if not name:
            raise ColorError("A preset name is required.")
        if len(name) > 60:
            raise ColorError("The preset name is too long (at most 60 characters).")
        if name.casefold() in {label.casefold() for label, _v in GRADE_PRESETS.values()}:
            raise ColorError(f"'{name}' is a built-in look; choose another name.")
        ColorSettings(grading=True, preset="custom", values=dict(values)).validate()  # only valid values are kept
        data = self._all()
        data[name] = {k: float(v) for k, v in values.items() if abs(float(v)) > 1e-9}
        self._settings.set(self.KEY, json.dumps(data))
        return name

    def delete(self, name: str) -> None:
        data = self._all()
        if data.pop(name, None) is not None:
            self._settings.set(self.KEY, json.dumps(data))


def grade_choices() -> list[tuple[str, str]]:
    return [(key, label) for key, (label, _v) in GRADE_PRESETS.items()]


def filter_choices() -> list[tuple[str, str]]:
    return [(key, label) for key, (label, _v) in FILTERS.items()]
