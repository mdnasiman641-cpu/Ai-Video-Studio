"""Active-speaker estimation from audio-visual evidence.

For every visible face with mouth-movement data (``reframe.mouth``) and a window of about
1.2 s around a moment, two measures are combined:

- **Mouth activity during speech**: mouth movement while the audio contains speech, relative
  to typical mouth movement during silence (one baseline shared by all faces). A listener's
  mouth is mostly still.
- **Audio-visual synchrony**: correlation between the face's mouth movement and the loudness
  envelope of the audio (allowing ±2 frames of lag). The person whose mouth moves with the
  rhythm of the speech is the likely speaker.

The **speaker evidence score** (0..1) is the mean of the two. A face is named the speaker only
when speech is present, its score is at least ``MIN_EVIDENCE`` and clearly above every other
visible face (``MIN_MARGIN``). Otherwise the moment is "silence" or "uncertain" (off-screen
narration, overlapping speech, faces without visible mouths). The score is a heuristic
measure, not a calibrated probability, and this is not a trained active-speaker model.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field

from ai_video_studio.reframe.model import SpeechActivity, Track, TrackingAnalysis

WINDOW = 1.2          # seconds of evidence around each moment
MIN_EVIDENCE = 0.35   # minimum speaker evidence score to name a speaker
MIN_MARGIN = 0.15     # the speaker must beat the next face by this much
MIN_SPEECH = 0.3      # share of the window that must contain speech
MAX_LAG = 2           # frames of audio/video lag tried in the correlation


@dataclass
class SpeakerMoment:
    state: str                          # speaker | silence | uncertain
    track_id: int | None = None
    evidence: dict[int, float] = field(default_factory=dict)  # per visible face with enough data
    speech: float = 0.0                 # share of the window with speech

    @property
    def score(self) -> float | None:
        return self.evidence.get(self.track_id) if self.track_id is not None else None


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


class SpeakerEvidence:
    def __init__(self, analysis: TrackingAnalysis, speech_threshold: float = 0.5):
        if analysis.speech is None:
            raise ValueError("speech activity is required")
        self.analysis = analysis
        self.speech: SpeechActivity = analysis.speech
        self.threshold = speech_threshold
        self.dt = analysis.sample_interval
        self._series: dict[int, tuple[list[float], list[float]]] = {}
        quiet: list[float] = []
        everything: list[float] = []
        for track in analysis.tracks:
            if track.kind != "face":
                continue
            pts = [(s.t, s.mouth) for s in track.samples if s.mouth is not None]
            if not pts:
                continue
            times, values = [p[0] for p in pts], [p[1] for p in pts]
            self._series[track.id] = (times, values)
            quiet += [v for t, v in pts if self.speech.prob_at(t) < speech_threshold]
            everything += values
        # One baseline for all faces (mouth movement during silence, or the quietest quarter of
        # all values): faces showing the same evidence then get the same score, so overlapping
        # speech is reported as uncertain rather than decided by a per-face normalisation.
        pool = quiet if len(quiet) >= 10 else sorted(everything)[: max(1, len(everything) // 4)]
        self._baseline = statistics.median(pool) if pool else 0.0

    def track_evidence(self, track: Track, t: float) -> float | None:
        series = self._series.get(track.id)
        if series is None:
            return None
        times, values = series
        a = bisect.bisect_left(times, t - WINDOW / 2)
        b = bisect.bisect_right(times, t + WINDOW / 2)
        expected = WINDOW / self.dt
        if b - a < max(4, 0.4 * expected):
            return None
        ts, ms = times[a:b], values[a:b]
        speaking = [m for tt, m in zip(ts, ms) if self.speech.prob_at(tt) >= self.threshold]
        if not speaking:
            return 0.0
        ratio = (sum(speaking) / len(speaking)) / (self._baseline + 0.02)
        activity = min(1.0, max(0.0, (ratio - 1.2) / 2.0))
        best = 0.0
        for lag in range(-MAX_LAG, MAX_LAG + 1):
            energy = [self.speech.energy_at(tt + lag * self.dt) for tt in ts]
            best = max(best, _pearson(ms, energy))
        return 0.5 * activity + 0.5 * min(1.0, best)

    def moment(self, t: float, visible: list[Track]) -> SpeakerMoment:
        speech = self.speech.speech_fraction(t - WINDOW / 2, t + WINDOW / 2, self.threshold)
        if speech < MIN_SPEECH:
            return SpeakerMoment("silence", speech=speech)
        evidence = {}
        for track in visible:
            score = self.track_evidence(track, t)
            if score is not None:
                evidence[track.id] = round(score, 3)
        ranked = sorted(evidence.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked or ranked[0][1] < MIN_EVIDENCE:
            return SpeakerMoment("uncertain", evidence=evidence, speech=speech)
        if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < MIN_MARGIN:
            return SpeakerMoment("uncertain", evidence=evidence, speech=speech)  # overlapping or unclear
        return SpeakerMoment("speaker", ranked[0][0], evidence, speech)
