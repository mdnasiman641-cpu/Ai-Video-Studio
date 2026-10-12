"""Speed and audio effects for an edited export (no Qt).

:class:`EditEffects` holds one video's speed and audio settings. The render pipeline turns
them into FFmpeg filters inside the same filter graph as the cuts, the resize/reframe stage
and the subtitles, so everything is encoded once.

Speed: the picture is retimed with ``setpts`` after the cuts and the video stage; captions
are retimed by the same factor (see ``captions.model.retime``). Linked audio uses the same
factor, so audio and video stay in sync. With **preserve pitch** the tempo is changed with
``atempo`` (chained, because one ``atempo`` only accepts 0.5–2.0); without it the samples are
played faster or slower (``asetrate``), which also changes the pitch, like a tape.
Independent (unlinked) audio speed is defined as: the audio is changed by its own factor and
then cut, or padded with silence, to the length of the video. It is no longer in sync with the
picture, which is only useful for music or ambience.

Audio filters run in a fixed order: noise reduction → speed → voice effect → bass/treble →
volume → loudness normalisation → (unlinked: pad/cut to the video length) → fades.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from ai_video_studio.core.errors import StudioError

SPEED_CHOICES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0, 4.0)
MIN_SPEED, MAX_SPEED = 0.25, 4.0
NOISE_REDUCTION = {"off": "Off", "light": "Light", "medium": "Medium", "strong": "Strong"}
NOISE_LEVELS_DB = {"light": 6, "medium": 12, "strong": 20}  # afftdn noise reduction amount
VOICE_EFFECTS = {"none": "None", "radio": "Radio / phone", "echo": "Echo", "deeper": "Deeper voice",
                 "higher": "Higher voice"}
VOICE_PITCH = {"deeper": 0.85, "higher": 1.2}
AUDIO_RATE = 48000


class EffectsError(StudioError):
    pass


def atempo_chain(factor: float) -> list[str]:
    """``atempo`` filters for a tempo factor; each stays within the 0.5–2.0 every FFmpeg build accepts."""
    parts = []
    while factor > 2.0 + 1e-9:
        parts.append(2.0)
        factor /= 2.0
    while factor < 0.5 - 1e-9:
        parts.append(0.5)
        factor /= 0.5
    parts.append(factor)
    return [f"atempo={p:.6g}" for p in parts if abs(p - 1.0) > 1e-9]


@dataclass
class EditEffects:
    speed_enabled: bool = False
    video_speed: float = 1.0
    link_audio: bool = True        # audio follows the video speed (stays in sync)
    audio_speed: float = 1.0       # used when not linked
    preserve_pitch: bool = True
    audio_enabled: bool = False
    volume: int = 100              # percent
    noise_reduction: str = "off"
    fade_in: float = 0.0           # seconds
    fade_out: float = 0.0
    normalize: bool = False        # EBU R128 loudness normalisation (loudnorm)
    bass_db: float = 0.0
    treble_db: float = 0.0
    voice: str = "none"

    def validate(self) -> None:
        errors = []
        for name, value in (("Video speed", self.video_speed), ("Audio speed", self.audio_speed)):
            if not MIN_SPEED <= float(value) <= MAX_SPEED:
                errors.append(f"{name} must be between {MIN_SPEED:g}× and {MAX_SPEED:g}×")
        if not 0 <= int(self.volume) <= 400:
            errors.append("Volume must be between 0% and 400%")
        if self.noise_reduction not in NOISE_REDUCTION:
            errors.append(f"Unknown noise reduction '{self.noise_reduction}'")
        for name, value in (("Fade-in", self.fade_in), ("Fade-out", self.fade_out)):
            if not 0 <= float(value) <= 10:
                errors.append(f"{name} must be between 0 and 10 seconds")
        for name, value in (("Bass", self.bass_db), ("Treble", self.treble_db)):
            if not -12 <= float(value) <= 12:
                errors.append(f"{name} must be between -12 and +12 dB")
        if self.voice not in VOICE_EFFECTS:
            errors.append(f"Unknown voice effect '{self.voice}'")
        if errors:
            raise EffectsError("; ".join(errors))

    # -- what is active -----------------------------------------------------------
    @property
    def video_factor(self) -> float:
        return float(self.video_speed) if self.speed_enabled else 1.0

    @property
    def audio_factor(self) -> float:
        if not self.speed_enabled:
            return 1.0
        return float(self.video_speed) if self.link_audio else float(self.audio_speed)

    @property
    def unlinked(self) -> bool:
        return self.speed_enabled and not self.link_audio and abs(self.audio_factor - self.video_factor) > 1e-9

    @property
    def audio_active(self) -> bool:
        return self.audio_enabled and (
            int(self.volume) != 100 or self.noise_reduction != "off" or self.fade_in > 0 or self.fade_out > 0
            or self.normalize or self.bass_db != 0 or self.treble_db != 0 or self.voice != "none")

    @property
    def changes_audio(self) -> bool:
        """True when the audio has to be filtered (and therefore re-encoded)."""
        return self.audio_active or abs(self.audio_factor - 1.0) > 1e-9 or self.unlinked

    @property
    def active(self) -> bool:
        return self.changes_audio or abs(self.video_factor - 1.0) > 1e-9

    def output_duration(self, edited_duration: float | None) -> float | None:
        return edited_duration / self.video_factor if edited_duration else edited_duration

    # -- FFmpeg ---------------------------------------------------------------------
    def video_filters(self) -> list[str]:
        f = self.video_factor
        return [] if abs(f - 1.0) < 1e-9 else [f"setpts=PTS/{f:.6g}"]

    def audio_filters(self, video_duration: float | None) -> list[str]:
        """The audio filter chain; ``video_duration`` is the length of the output video."""
        self.validate()
        if not self.changes_audio:
            return []
        chain = [f"aresample={AUDIO_RATE}"]
        a = self.audio_enabled
        if a and self.noise_reduction != "off":
            chain.append(f"afftdn=nr={NOISE_LEVELS_DB[self.noise_reduction]}")
        factor = self.audio_factor
        if abs(factor - 1.0) > 1e-9:
            if self.preserve_pitch:
                chain += atempo_chain(factor)
            else:
                chain += [f"asetrate={round(AUDIO_RATE * factor)}", f"aresample={AUDIO_RATE}"]
        if a and self.voice == "radio":
            chain += ["highpass=f=300", "lowpass=f=3400"]
        elif a and self.voice == "echo":
            chain.append("aecho=0.8:0.88:60:0.4")
        elif a and self.voice in VOICE_PITCH:
            p = VOICE_PITCH[self.voice]
            chain += [f"asetrate={round(AUDIO_RATE * p)}", f"aresample={AUDIO_RATE}"] + atempo_chain(1 / p)
        if a and self.bass_db:
            chain.append(f"bass=g={float(self.bass_db):g}")
        if a and self.treble_db:
            chain.append(f"treble=g={float(self.treble_db):g}")
        if a and int(self.volume) != 100:
            chain.append(f"volume={int(self.volume) / 100:g}")
        if a and self.normalize:
            chain += ["loudnorm=I=-16:TP=-1.5:LRA=11", f"aresample={AUDIO_RATE}"]
        if self.unlinked:
            if not video_duration:
                raise EffectsError("Independent audio speed needs the video length, which is unknown.")
            chain += ["apad", f"atrim=end={video_duration:.6f}"]
        if a and self.fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={float(self.fade_in):g}")
        if a and self.fade_out > 0 and video_duration:
            start = max(0.0, video_duration - float(self.fade_out))
            chain.append(f"afade=t=out:st={start:.3f}:d={float(self.fade_out):g}")
        return chain

    def required_filters(self) -> set[str]:
        names = {f.split("=", 1)[0] for f in self.audio_filters(1.0) + self.video_filters()}
        return names

    def summary(self) -> list[str]:
        """Short descriptions of what is applied, for the user."""
        parts = []
        if abs(self.video_factor - 1.0) > 1e-9 or abs(self.audio_factor - 1.0) > 1e-9:
            if self.unlinked:
                parts.append(f"video {self.video_factor:g}×, audio {self.audio_factor:g}× (not in sync)")
            else:
                parts.append(f"speed {self.video_factor:g}×" + ("" if self.preserve_pitch or
                                                                   abs(self.audio_factor - 1) < 1e-9
                                                                   else " (pitch changes)"))
        if self.audio_enabled:
            if int(self.volume) != 100:
                parts.append(f"volume {int(self.volume)}%")
            if self.noise_reduction != "off":
                parts.append(f"noise reduction {NOISE_REDUCTION[self.noise_reduction].lower()}")
            if self.voice != "none":
                parts.append(VOICE_EFFECTS[self.voice].lower())
            if self.bass_db or self.treble_db:
                parts.append(f"bass {float(self.bass_db):+g} dB, treble {float(self.treble_db):+g} dB")
            if self.normalize:
                parts.append("loudness normalised")
            if self.fade_in or self.fade_out:
                parts.append(f"fade in {float(self.fade_in):g} s / out {float(self.fade_out):g} s")
        return parts

    # -- serialisation --------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> "EditEffects":
        """Read stored settings. Damaged data gives "no effects", or raises with ``strict``."""
        defaults = cls()
        values = {}
        for f in fields(cls):
            if f.name not in (data or {}):
                continue
            default = getattr(defaults, f.name)
            try:
                value = data[f.name]
                values[f.name] = bool(value) if isinstance(default, bool) else type(default)(value)
            except (TypeError, ValueError):
                if strict:
                    raise EffectsError(f"Invalid value for {f.name}: {data[f.name]!r}") from None
                continue
        effects = cls(**values)
        try:
            effects.validate()
        except EffectsError:
            if strict:
                raise
            return cls()  # damaged data falls back to "no effects" rather than breaking a project
        return effects

    def copy(self) -> "EditEffects":
        return EditEffects.from_dict(self.to_dict())
