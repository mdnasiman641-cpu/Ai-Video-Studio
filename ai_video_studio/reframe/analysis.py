"""Video analysis: decode sampled, downscaled frames with FFmpeg, detect and track subjects.

FFmpeg does the decoding (any format the export pipeline supports, rotation applied, one
frame in memory at a time) and its ``fps`` filter picks the frame shown at each multiple
of ``1 / sample_fps``, so analysed frame ``i`` is the picture at ``i / sample_fps`` on the
same clock the export filters use. This also holds for variable-frame-rate sources.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ai_video_studio.core.errors import MediaError
from ai_video_studio.core.media import format_duration
from ai_video_studio.core.process import run_frames
from ai_video_studio.reframe.detection import appearance_descriptor
from ai_video_studio.reframe.framing import even, suitable_subjects
from ai_video_studio.reframe.model import Detection, TrackingAnalysis
from ai_video_studio.reframe.mouth import MouthMotion
from ai_video_studio.reframe.tracker import IouTracker

log = logging.getLogger(__name__)

ANALYSIS_LONG_SIDE = 960  # analysed frames are downscaled so their longer side is at most this


def analysis_frame_size(width: int, height: int, long_side: int = ANALYSIS_LONG_SIDE) -> tuple[int, int]:
    scale = min(1.0, long_side / max(width, height))
    return even(width * scale), even(height * scale)


def decode_args(ffmpeg: Path | str, source: str, sample_fps: float, width: int, height: int) -> list[str]:
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error",
        "-i", str(source),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        # round=up: output frame k is the source frame on screen at time k / sample_fps. With the
        # default (round=near) FFmpeg emits a frame up to half an output interval late.
        "-vf", f"fps={sample_fps:g}:round=up,scale={width}:{height}:flags=area",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
    ]


def analyze_video(
    ffmpeg: Path | str,
    source: str,
    width: int,
    height: int,
    duration: float,
    sample_fps: float,
    detector: Callable[[object], list[Detection]],
    detector_name: str = "",
    progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    long_side: int = ANALYSIS_LONG_SIDE,
    features: set[str] | frozenset[str] = frozenset({"faces"}),
    face_threshold: float = 0.6,
) -> TrackingAnalysis:
    """Analyse ``source`` (displayed size ``width`` x ``height``) and return tracked subjects.

    ``features`` may include ``"mouth"`` (mouth-region movement per face, for active-speaker
    detection; needs YuNet landmarks) and ``"persons"`` (recorded when the detector looks for
    full bodies). Speech is analysed separately from the audio (``reframe.speech``).
    """
    import numpy as np

    if width <= 0 or height <= 0:
        raise MediaError("The video has no valid frame size.")
    if sample_fps <= 0:
        raise MediaError("The analysis rate must be positive.")
    aw, ah = analysis_frame_size(width, height, long_side)
    tracker = IouTracker(aspect=width / height, max_missed_seconds=max(1.5, 3 / sample_fps))
    expected = max(1.0, duration * sample_fps) if duration and duration > 0 else None
    state = {"frames": 0, "detections": 0, "landmarks": 0}
    want_mouth = "mouth" in features
    mouth = MouthMotion() if want_mouth else None

    def on_frame(data: bytes) -> None:
        index = state["frames"]
        frame = np.frombuffer(data, dtype=np.uint8).reshape(ah, aw, 3)
        detections = [_with_appearance(frame, d) for d in detector(frame)]
        assigned = tracker.update(index / sample_fps, detections)
        if mouth is not None and assigned:
            gray = _gray(frame)
            for key, det in assigned:
                if det.landmarks is not None:
                    state["landmarks"] += 1
                    tracker.annotate_mouth(key, mouth.measure(key, index, gray, det))
        state["frames"] = index + 1
        state["detections"] += len(detections)
        if progress is not None and expected:
            progress(min(0.999, state["frames"] / expected))

    result, frames = run_frames(decode_args(ffmpeg, source, sample_fps, aw, ah), aw * ah * 3, on_frame,
                                cancel_event=cancel_event)
    detail = next((ln.strip() for ln in reversed(result.stderr.splitlines()) if ln.strip()), "")
    if frames == 0:
        if result.returncode != 0:
            raise MediaError(f"The video could not be decoded for analysis: {detail or 'unknown FFmpeg error'}")
        raise MediaError("No video frames could be decoded for analysis.")
    warnings: list[str] = []
    if result.returncode != 0:
        warnings.append(f"Decoding stopped early at {format_duration(frames / sample_fps)} ({detail}). "
                        f"Only that part of the video was analysed; the file may be damaged.")
    tracks = tracker.finish()
    recorded = {"faces"} | ({"persons"} & set(features))
    if want_mouth:
        if state["detections"] and not state["landmarks"]:
            warnings.append("Mouth movement could not be measured: the face detector in use gives no landmarks "
                            "(the Haar fallback). Active-speaker mode needs the YuNet model.")
        else:
            recorded.add("mouth")
    analysis = TrackingAnalysis(width=width, height=height, duration=duration or frames / sample_fps,
                                sample_fps=sample_fps, frames_analyzed=frames, detector=detector_name,
                                tracks=tracks, warnings=warnings, features=sorted(recorded),
                                face_threshold=face_threshold)
    add_quality_notes(analysis)
    log.info("Video analysed", extra={"frames": frames, "detections": state["detections"],
                                      "tracks": len(tracks), "sample_fps": sample_fps})
    return analysis


def add_quality_notes(analysis: TrackingAnalysis) -> None:
    """Warnings about tracking reliability, measured while each subject is on screen.

    (A subject who appears in only part of the video is not unreliable; that is reported as
    a note, not a warning.)
    """
    tracks = analysis.tracks
    if not tracks:
        analysis.warnings.append("No faces or people were detected. Auto reframe cannot follow a subject in "
                                 "this video; use Center crop or adjust the framing manually.")
        return
    if not any(t.kind == "face" for t in tracks):
        analysis.warnings.append("No faces were detected; following full-body detections, which are "
                                 "less precise.")
    best = suitable_subjects(analysis)[0]
    rate = analysis.detection_rate(best)
    if rate < 0.5:
        analysis.warnings.append(f"Subject #{best.id} was detected in only {rate:.0%} of the analysed frames while on "
                                 f"screen (turned away, small, blurred or hidden). Expect held or "
                                 f"centred framing in the gaps.")
    share = analysis.screen_share(best)
    if share < 0.6:
        analysis.notes.append(f"Subject #{best.id} is on screen for about {share:.0%} of the video.")
    if best.mean_confidence < 0.5:
        analysis.warnings.append(f"Detection confidence for subject #{best.id} is low "
                                 f"({best.mean_confidence:.2f}); check the preview carefully.")


def _with_appearance(frame, det: Detection) -> Detection:
    try:
        appearance = appearance_descriptor(frame, det)
    except Exception:  # noqa: BLE001 - identity evidence is optional
        return det
    return replace(det, appearance=appearance)


def _gray(frame):
    from ai_video_studio.reframe.detection import load_cv2

    cv2 = load_cv2()
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
