"""Caption animations rendered with ASS override tags (no Qt).

A :class:`CaptionAnimation` has an entrance, an exit, an optional loop (emphasis) and an
optional word-level effect, with durations, a delay, an intensity and a loop period. It is
stored on a caption (``CaptionSegment.animation``) or used as the default for every caption
(Bulk Processing, "apply to all").

**How it is rendered.** Motion, scale, opacity, blur and rotation are described as keyframes
(:data:`ENTRANCES`, :data:`EXITS`, :data:`LOOPS`). Each caption becomes several ASS events in
a row (delay, entrance, middle with the loop, exit); every keyframe interval is its own event
with the start state as tags, one ``\\move`` and one ``\\t`` to the end state, so libass
(FFmpeg's ``subtitles`` filter) draws exactly these states in the export. The live preview
interpolates the same keyframes (:func:`state_at`). Text reveals and word effects set tags
per word or per grapheme cluster, never inside a cluster, so Bengali conjuncts (যুক্তাক্ষর),
vowel signs (কার) and ফলা stay shaped.

**Timing.** An animation never extends outside its caption: the entrance and exit are
shortened to at most 45 % of the caption each, the delay to half of it. Word effects follow
the caption's word timestamps (from Whisper) when they match the text; otherwise each word
gets a share of the time from the end of the delay to 85 % of the caption, proportional to
its length in grapheme clusters (the fallback).
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import asdict, dataclass, fields

from ai_video_studio.core.errors import StudioError

Keyframes = list[tuple[float, dict[str, float]]]
NEUTRAL = {"s": 100.0, "x": 0.0, "y": 0.0, "o": 1.0, "b": 0.0, "rx": 0.0, "rz": 0.0}

# s scale %, x/y offset as a fraction of the frame width/height, o opacity, b blur (1080p
# pixels), rx/rz rotation in degrees. Unlisted properties are neutral.
_IN: dict[str, Keyframes] = {
    "fade": [(0, {"o": 0}), (1, {})],
    "slide_left": [(0, {"x": -0.12, "o": 0}), (1, {})],
    "slide_right": [(0, {"x": 0.12, "o": 0}), (1, {})],
    "slide_up": [(0, {"y": 0.06, "o": 0}), (1, {})],
    "slide_down": [(0, {"y": -0.06, "o": 0}), (1, {})],
    "zoom": [(0, {"s": 30, "o": 0}), (1, {})],
    "pop": [(0, {"s": 0, "o": 0}), (0.6, {"s": 115}), (1, {})],
    "bounce": [(0, {"s": 0, "o": 0}), (0.45, {"s": 125}), (0.7, {"s": 90}), (0.85, {"s": 104}), (1, {})],
    "elastic": [(0, {"s": 0, "o": 0}), (0.35, {"s": 130}), (0.55, {"s": 82}), (0.72, {"s": 109}), (0.86, {"s": 96}),
                (1, {})],
    "blur": [(0, {"b": 20, "o": 0}), (1, {})],
    "flip": [(0, {"rx": 90, "o": 0}), (1, {})],
}


def _reverse(frames: Keyframes) -> Keyframes:
    return [(1 - t, props) for t, props in reversed(frames)]


ENTRANCES = {
    "none": "None", "fade": "Fade In", "slide_left": "Slide In Left", "slide_right": "Slide In Right",
    "slide_up": "Slide In Up", "slide_down": "Slide In Down", "zoom": "Zoom In", "pop": "Pop In",
    "bounce": "Bounce In", "typewriter": "Typewriter", "word_reveal": "Word Reveal",
    "char_reveal": "Character Reveal", "blur": "Blur In", "flip": "Flip In", "elastic": "Elastic In",
}
TEXT_ENTRANCES = ("typewriter", "word_reveal", "char_reveal")  # reveal the text instead of moving it
EXITS = {
    "none": "None", "fade": "Fade Out", "slide_left": "Slide Out Left", "slide_right": "Slide Out Right",
    "slide_up": "Slide Out Up", "slide_down": "Slide Out Down", "zoom": "Zoom Out", "pop": "Pop Out",
    "blur": "Blur Out", "flip": "Flip Out",
}
_OUT: dict[str, Keyframes] = {
    "fade": _reverse(_IN["fade"]),
    "slide_left": _reverse(_IN["slide_left"]),     # ends on the left
    "slide_right": _reverse(_IN["slide_right"]),
    "slide_up": _reverse(_IN["slide_down"]),       # ends above
    "slide_down": _reverse(_IN["slide_up"]),
    "zoom": _reverse(_IN["zoom"]),
    "pop": [(0, {}), (0.4, {"s": 115}), (1, {"s": 0, "o": 0})],
    "blur": _reverse(_IN["blur"]),
    "flip": _reverse(_IN["flip"]),
}
LOOPS = {
    "none": "None", "pulse": "Gentle Pulse", "bounce": "Bounce", "float": "Floating", "shake": "Shake",
    "glow": "Glow Pulse", "wobble": "Wobble", "scale_pulse": "Scale Pulse",
}
_SHAKE = [(0.0, 0.0), (0.006, 0.002), (-0.005, -0.002), (0.004, 0.003), (-0.006, 0.001), (0.005, -0.003),
          (-0.003, 0.002), (0.002, -0.001)]
# keyframes over one period, and the period relative to the chosen loop period
_LOOP: dict[str, tuple[Keyframes, float]] = {
    "pulse": ([(0, {}), (0.5, {"s": 104}), (1, {})], 1.0),
    "scale_pulse": ([(0, {}), (0.5, {"s": 112}), (1, {})], 1.0),
    "bounce": ([(0, {}), (0.25, {"y": -0.018}), (0.5, {"y": -0.025}), (0.75, {"y": -0.018}), (1, {})], 0.8),
    "float": ([(i / 8, {"y": -0.012 * math.sin(2 * math.pi * i / 8)}) for i in range(9)], 2.0),
    "shake": ([(i / 8, {"x": dx, "y": dy}) for i, (dx, dy) in enumerate(_SHAKE)] + [(1, {})], 0.4),
    "glow": ([(0, {}), (0.5, {"b": 6}), (1, {})], 1.2),
    "wobble": ([(0, {}), (0.25, {"rz": 3}), (0.5, {}), (0.75, {"rz": -3}), (1, {})], 1.0),
}
WORD_EFFECTS = {
    "none": "None", "highlight": "Highlight Current Word", "word_reveal": "Word-by-Word Reveal",
    "word_pop": "Word Pop", "karaoke": "Karaoke Highlight", "typewriter": "Typewriter Reveal",
}
SCALE_LOOPS = ("pulse", "scale_pulse")
MIN_PHASE_MS = 40
MAX_EVENTS_PER_CAPTION = 600


class AnimationError(StudioError):
    pass


@dataclass
class CaptionAnimation:
    entrance: str = "none"
    exit: str = "none"
    loop: str = "none"
    word: str = "none"           # word-level effect over the whole caption
    in_ms: int = 400             # entrance duration
    out_ms: int = 300            # exit duration
    delay_ms: int = 0            # the caption appears this long after its start time
    intensity: int = 100         # percent: distance, scale and rotation of the movement
    period_ms: int = 1000        # loop speed

    def validate(self) -> None:
        errors = []
        for name, value, options in (("entrance", self.entrance, ENTRANCES), ("exit", self.exit, EXITS),
                                     ("loop", self.loop, LOOPS), ("word effect", self.word, WORD_EFFECTS)):
            if value not in options:
                errors.append(f"Unknown {name} animation '{value}'")
        for label, value, low, high in (("Entrance duration", self.in_ms, 0, 3000),
                                        ("Exit duration", self.out_ms, 0, 3000), ("Delay", self.delay_ms, 0, 3000),
                                        ("Intensity", self.intensity, 0, 100), ("Loop period", self.period_ms, 200, 5000)):
            if not isinstance(value, int) or not low <= value <= high:
                errors.append(f"{label} must be between {low} and {high}")
        if self.word != "none" and self.entrance in TEXT_ENTRANCES:
            errors.append("Choose either a text reveal entrance or a word effect, not both")
        if self.word == "word_pop" and self.loop in SCALE_LOOPS:
            errors.append("Word Pop cannot be combined with a pulsing loop")
        if errors:
            raise AnimationError("; ".join(errors))

    @property
    def active(self) -> bool:
        return any(v != "none" for v in (self.entrance, self.exit, self.loop, self.word)) or self.delay_ms > 0

    def summary(self) -> str:
        parts = [f"{kind}: {labels[value]}" for kind, value, labels in (
            ("In", self.entrance, ENTRANCES), ("Out", self.exit, EXITS), ("Loop", self.loop, LOOPS),
            ("Words", self.word, WORD_EFFECTS)) if value != "none"]
        return ", ".join(parts) or "No animation"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> "CaptionAnimation":
        """Read stored settings; damaged data gives no animation, or raises with ``strict``."""
        values = {}
        for f in fields(cls):
            if data and f.name in data:
                default = getattr(cls(), f.name)
                try:
                    values[f.name] = type(default)(data[f.name])
                except (TypeError, ValueError):
                    if strict:
                        raise AnimationError(f"Invalid caption animation value for {f.name}") from None
        animation = cls(**values)
        try:
            animation.validate()
        except AnimationError:
            if strict:
                raise
            return cls()
        return animation


# -- grapheme clusters ----------------------------------------------------------------

ZWJ, ZWNJ = "‍", "‌"


def _is_virama(ch: str) -> bool:
    return unicodedata.combining(ch) == 9


def _extends(ch: str) -> bool:
    return (unicodedata.category(ch) in ("Mn", "Mc", "Me") or ch in (ZWJ, ZWNJ) or "︀" <= ch <= "️"
            or "\U0001f3fb" <= ch <= "\U0001f3ff")


def graphemes(text: str) -> list[str]:
    """User-perceived characters: a base with its combining marks, and conjuncts joined by a
    virama (হসন্ত) or ZWJ, so ক্ষ, ক্র, র্ক, ্য-ফলা and কার stay whole. Works on code points
    (Python strings), never on UTF-8 bytes or UTF-16 units."""
    clusters: list[str] = []
    for ch in text:
        if clusters and ch not in "\r\n" and (
                _extends(ch) or _is_virama(clusters[-1][-1]) and unicodedata.category(ch).startswith("L")
                or clusters[-1][-1] == ZWJ):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def cluster_len(text: str) -> int:
    return len(graphemes(text))


# -- keyframes ------------------------------------------------------------------------

def _scaled(props: dict[str, float], k: float) -> dict[str, float]:
    state = dict(NEUTRAL)
    for name, value in props.items():
        state[name] = value if name == "o" else NEUTRAL[name] + (value - NEUTRAL[name]) * k
    return state


def _frames(frames: Keyframes, k: float) -> list[tuple[float, dict[str, float]]]:
    return [(t, _scaled(props, k)) for t, props in frames]


def _interpolate(frames: list[tuple[float, dict[str, float]]], f: float) -> dict[str, float]:
    if f <= frames[0][0]:
        return dict(frames[0][1])
    for (t0, a), (t1, b) in zip(frames, frames[1:]):
        if f <= t1:
            r = 0.0 if t1 <= t0 else (f - t0) / (t1 - t0)
            return {name: a[name] + (b[name] - a[name]) * r for name in NEUTRAL}
    return dict(frames[-1][1])


@dataclass
class Phases:
    """Times in seconds on the output timeline."""
    show: float        # end of the delay
    body: float        # end of the entrance
    exit: float        # start of the exit
    end: float


def phases(anim: CaptionAnimation, start: float, end: float) -> Phases:
    duration = max(0.0, end - start)
    delay = min(anim.delay_ms / 1000, duration * 0.5)
    rest = duration - delay
    entrance = min(anim.in_ms / 1000, rest * 0.45) if anim.entrance != "none" else 0.0
    leave = min(anim.out_ms / 1000, rest * 0.45) if anim.exit != "none" else 0.0
    if entrance * 1000 < MIN_PHASE_MS:
        entrance = 0.0
    if leave * 1000 < MIN_PHASE_MS:
        leave = 0.0
    return Phases(start + delay, start + delay + entrance, end - leave, end)


def _loop(anim: CaptionAnimation) -> tuple[list[tuple[float, dict[str, float]]], float] | None:
    if anim.loop == "none":
        return None
    frames, factor = _LOOP[anim.loop]
    return _frames(frames, anim.intensity / 100), anim.period_ms / 1000 * factor


def state_at(anim: CaptionAnimation, start: float, end: float, t: float) -> dict[str, float] | None:
    """Scale, offset, opacity, blur and rotation at time ``t`` (``None`` = not shown yet).

    Used by the live preview; the export uses the same keyframes."""
    p = phases(anim, start, end)
    k = anim.intensity / 100
    if t < p.show or t >= end:
        return None
    if t < p.body and anim.entrance in _IN:
        return _interpolate(_frames(_IN[anim.entrance], k), (t - p.show) / (p.body - p.show))
    if t >= p.exit and anim.exit in _OUT and p.end > p.exit:
        return _interpolate(_frames(_OUT[anim.exit], k), (t - p.exit) / (p.end - p.exit))
    loop = _loop(anim)
    if loop:
        frames, period = loop
        return _interpolate(frames, ((t - p.body) % period) / period)
    return dict(NEUTRAL)


def _intervals(anim: CaptionAnimation, start: float, end: float) -> list[tuple[float, float, dict, dict]]:
    """``(t0, t1, state at t0, state at t1)`` pieces covering the visible part of a caption."""
    p = phases(anim, start, end)
    k = anim.intensity / 100
    out: list[tuple[float, float, dict, dict]] = []

    def add_frames(frames, t_start, t_end):
        span = t_end - t_start
        for (f0, a), (f1, b) in zip(frames, frames[1:]):
            if f1 > f0:
                out.append((t_start + f0 * span, t_start + f1 * span, a, b))

    if p.body > p.show:
        if anim.entrance in _IN:
            add_frames(_frames(_IN[anim.entrance], k), p.show, p.body)
        else:  # text reveal: no movement
            out.append((p.show, p.body, dict(NEUTRAL), dict(NEUTRAL)))
    loop = _loop(anim)
    if p.exit > p.body:
        if loop:
            frames, period = loop
            t = p.body
            while t < p.exit - 1e-6:
                piece_end = min(p.exit, t + period)
                f_end = (piece_end - t) / period
                trimmed = [(f, s) for f, s in frames if f < f_end] + [(f_end, _interpolate(frames, f_end))]
                add_frames(trimmed, t, t + period)
                t = piece_end
        else:
            out.append((p.body, p.exit, dict(NEUTRAL), dict(NEUTRAL)))
    if p.end > p.exit:
        add_frames(_frames(_OUT[anim.exit], k), p.exit, p.end)
    return out


# -- word and cluster timing ------------------------------------------------------------

@dataclass
class Unit:
    text: str             # a word or a grapheme cluster (escaped later)
    word: int             # index of the word it belongs to; -1 for spaces
    start: float          # reveal / highlight start (seconds)
    end: float            # highlight end (the next word's start)
    spoken: float = 0.0   # end of the spoken word (karaoke fill); 0 = same as ``end``


def _word_times(seg, words: list[str], t0: float, t1: float) -> list[tuple[float, float, float]]:
    """``(start, end, spoken_end)`` per word; ``end`` runs to the next word's start."""
    stamps = [w for w in getattr(seg, "words", []) if w.text.strip()]
    if stamps and len(stamps) == len(words):
        times = [(max(t0, w.start), max(t0, w.end)) for w in stamps]
        return [(s, max(s, times[i + 1][0] if i + 1 < len(times) else seg.end), max(s, e))
                for i, (s, e) in enumerate(times)]
    weights = [max(1, cluster_len(w)) for w in words]
    total = sum(weights) or 1
    out, t = [], t0
    for w in weights:
        span = (t1 - t0) * w / total
        out.append((t, t + span, t + span))
        t += span
    if out:
        out[-1] = (out[-1][0], seg.end, out[-1][2])
    return out


def text_units(seg, lines: list[str], anim: CaptionAnimation, p: Phases) -> list[list[Unit]]:
    """The wrapped lines split into words or clusters with their times (see the module notes)."""
    words = [w for line in lines for w in line.split(" ") if w]
    per_cluster = anim.word == "typewriter" or anim.entrance in ("typewriter", "char_reveal")
    if anim.entrance in TEXT_ENTRANCES:
        a, b = p.show, max(p.body, p.show + 0.001)
        if per_cluster:
            weights = [cluster_len(w) for w in words]
            total = max(1, sum(weights))
            times, t = [], a
            for w in weights:
                times.append((t, t + (b - a) * w / total))
                t = times[-1][1]
        else:
            n = max(1, len(words))
            times = [(a + (b - a) * i / n, a + (b - a) * (i + 1) / n) for i in range(len(words))]
        times = [(x, y, y) for x, y in times]
    else:
        times = _word_times(seg, words, p.show, p.show + (p.end - p.show) * 0.85)
    result, index = [], 0
    for line in lines:
        units: list[Unit] = []
        for n, word in enumerate(line.split(" ")):
            if n:
                units.append(Unit(" ", -1, 0.0, 0.0))
            if not word:
                continue
            ws, we, spoken = times[index]
            if per_cluster:
                clusters = graphemes(word)
                step = (spoken - ws) / max(1, len(clusters))
                units += [Unit(c, index, ws + i * step, we, spoken) for i, c in enumerate(clusters)]
            else:
                units.append(Unit(word, index, ws, we, spoken))
            index += 1
        result.append(units)
    return result


# -- ASS ----------------------------------------------------------------------------------

def _alpha(opacity: float) -> str:
    return f"&H{round(255 * (1 - max(0.0, min(1.0, opacity)))):02X}&"


def _cs(t: float) -> int:
    return int(round(t * 100))


def _anchor(style, width: int, height: int) -> tuple[float, float]:
    margin_h = round(width * style.margin_h / 100)
    margin_v = round(height * style.margin_v / 100)
    x = {"left": margin_h, "center": width / 2, "right": width - margin_h}[style.alignment]
    y = {"bottom": height - margin_v, "middle": height / 2, "top": margin_v}[style.vertical]
    return x, y


def _alpha_tags(state: dict, style, box: bool) -> str:
    o = state["o"]
    if box:
        return f"\\3a{_alpha(o * style.background_opacity / 100)}"
    return f"\\1a{_alpha(o)}\\3a{_alpha(o)}\\4a{_alpha(o * style.shadow_opacity / 100)}"


def _state_tags(state: dict, style, box: bool, k_px: float) -> str:
    tags = f"\\fscx{state['s']:.1f}\\fscy{state['s']:.1f}" + _alpha_tags(state, style, box)
    if state["b"] > 0.01:
        tags += f"\\blur{state['b'] * k_px:.2f}"
    if abs(state["rx"]) > 0.01:
        tags += f"\\frx{state['rx']:.1f}"
    if abs(state["rz"]) > 0.01:
        tags += f"\\frz{state['rz']:.1f}"
    return tags


def _transform_tags(a: dict, b: dict, style, box: bool, k_px: float) -> str:
    tags = ""
    if abs(a["s"] - b["s"]) > 0.01:
        tags += f"\\fscx{b['s']:.1f}\\fscy{b['s']:.1f}"
    if abs(a["o"] - b["o"]) > 1e-4:
        tags += _alpha_tags(b, style, box)
    if abs(a["b"] - b["b"]) > 0.01:
        tags += f"\\blur{b['b'] * k_px:.2f}"
    if abs(a["rx"] - b["rx"]) > 0.01:
        tags += f"\\frx{b['rx']:.1f}"
    if abs(a["rz"] - b["rz"]) > 0.01:
        tags += f"\\frz{b['rz']:.1f}"
    return tags


def _color(hex_color: str) -> str:
    return f"&H{hex_color[5:7]}{hex_color[3:5]}{hex_color[1:3]}&".upper()


def _text(units_by_line: list[list[Unit]], anim: CaptionAnimation, style, t0: float, t1: float, escape,
          base_alpha: str) -> str:
    """The event text from ``t0`` to ``t1`` with per-word / per-cluster tags (absolute times → ms)."""
    def ms(t: float) -> int:
        return max(0, int(round((t - t0) * 1000)))

    base_color, hl = _color(style.text_color), _color(style.highlight_color)
    hidden = "\\1a&HFF&\\3a&HFF&\\4a&HFF&"
    reveal = anim.entrance in TEXT_ENTRANCES or anim.word in ("word_reveal", "typewriter", "word_pop")
    lines = []
    for units in units_by_line:
        if anim.word == "karaoke":
            parts, running = [], 0
            for u in units:
                if u.word < 0:
                    parts.append(escape(u.text))
                    continue
                begin = max(running, _cs(u.start - t0))
                stop = max(begin, _cs((u.spoken or u.end) - t0))
                gap, running = begin - running, stop
                parts.append(f"{{\\k{max(0, gap)}}}" if gap > 0 else "")
                parts.append(f"{{\\kf{stop - begin}}}{escape(u.text)}")
            lines.append("".join(parts))
            continue
        parts, dirty = [], False
        for u in units:
            if u.word < 0:
                parts.append(escape(u.text))
                continue
            tags = ""
            if reveal and u.start > t0:
                if u.start < t1:
                    fade = 120 if anim.entrance == "char_reveal" else 150 if anim.entrance == "word_reveal" or \
                        anim.word in ("word_reveal", "word_pop") else 1
                    tags += f"{hidden}\\t({ms(u.start)},{ms(u.start) + fade},{base_alpha})"
                    if anim.word == "word_pop":
                        tags += f"\\fscy135\\t({ms(u.start)},{ms(u.start) + 160},\\fscy100)"
                else:
                    tags += hidden
            elif dirty:
                tags += base_alpha + ("\\fscy100" if anim.word == "word_pop" else "")
            if anim.word == "highlight":
                on = u.start <= t0 < u.end
                tags += f"\\1c{hl if on else base_color}"
                if t0 < u.start < t1:
                    tags += f"\\t({ms(u.start)},{ms(u.start) + 1},\\1c{hl})"
                if t0 < u.end < t1:
                    tags += f"\\t({ms(u.end)},{ms(u.end) + 1},\\1c{base_color})"
            if tags and reveal:
                dirty = True  # later words need their alpha back
            if tags:
                parts.append("{" + tags + "}")
            parts.append(escape(u.text))
        lines.append("".join(parts))
    return r"\N".join(lines)


def animated_events(seg, style, anim: CaptionAnimation, lines: list[str], width: int, height: int, name: str,
                    box_name: str | None, escape, ass_time) -> list[str]:
    """ASS Dialogue lines for one animated caption (``lines``: the wrapped text)."""
    from ai_video_studio.captions.ass import _ALIGN

    anim.validate()
    p = phases(anim, seg.start, seg.end)
    pieces = _intervals(anim, seg.start, seg.end)
    if len(pieces) > MAX_EVENTS_PER_CAPTION:
        raise AnimationError(f"The caption at {seg.start:.2f} s is too long for the chosen loop speed; choose a "
                             "slower loop.")
    k_px = height / 1080
    ax, ay = _anchor(style, width, height)
    an = _ALIGN[(style.vertical, style.alignment)]
    units = text_units(seg, lines, anim, p)
    plain = r"\N".join(escape(line) for line in lines)
    events = []
    for a_t, b_t, a, b in pieces:
        c0, c1 = _cs(a_t), _cs(b_t)
        if c1 <= c0:
            continue
        t0, t1 = c0 / 100, c1 / 100
        dur = (c1 - c0) * 10
        x0, y0 = ax + a["x"] * width, ay + a["y"] * height
        x1, y1 = ax + b["x"] * width, ay + b["y"] * height
        if abs(x0 - x1) > 0.05 or abs(y0 - y1) > 0.05:
            position = f"\\move({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f},0,{dur})"
        else:
            position = f"\\pos({x0:.1f},{y0:.1f})"
        org = f"\\org({ax:.1f},{ay:.1f})" if (a["rx"] or a["rz"] or b["rx"] or b["rz"]) else ""
        for layer, style_name, box in ((0, box_name, True), (1, name, False)):
            if style_name is None:
                continue
            tags = f"\\an{an}{position}{org}" + _state_tags(a, style, box, k_px)
            change = _transform_tags(a, b, style, box, k_px)
            if change:
                tags += f"\\t(0,{dur},{change})"
            if box:
                text = plain
            else:
                if anim.word == "karaoke":
                    tags += f"\\1c{_color(style.highlight_color)}\\2c{_color(style.text_color)}"
                text = _text(units, anim, style, t0, t1, escape, _alpha_tags(b, style, False))
            events.append(f"Dialogue: {layer},{ass_time(t0)},{ass_time(t1)},{style_name},,0,0,0,,{{{tags}}}{text}")
    return events


def animation_for(seg, default: CaptionAnimation | None) -> CaptionAnimation | None:
    """The animation a caption is drawn with: its own, else the default; ``None`` = static."""
    own = getattr(seg, "animation", None)
    anim = CaptionAnimation.from_dict(own) if own is not None else default
    return anim if anim is not None and anim.active else None


def choices(options: dict[str, str]) -> list[tuple[str, str]]:
    return list(options.items())
