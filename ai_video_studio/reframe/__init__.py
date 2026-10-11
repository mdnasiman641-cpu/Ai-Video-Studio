"""Face/person tracking and auto reframe (no Qt).

- ``model``: detections, tracks, analysis results and reframe settings (serialisable).
- ``detection``: OpenCV face (YuNet, Haar fallback) and person (HOG) detectors.
- ``tracker``: IoU/centre-distance tracker that assigns stable track IDs.
- ``analysis``: decodes sampled, downscaled frames with FFmpeg and runs detection + tracking.
- ``framing``: aspect presets, crop geometry and the smoothed crop path.
- ``export``: FFmpeg ``sendcmd`` script and filter graph for the time-varying crop.
"""
