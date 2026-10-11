"""Functional self-test of an installed / packaged copy of AI Video Studio.

Run ``AI-Video-Studio-Console.exe --self-test [--report FILE] [--assets DIR] [--whisper-model DIR]``.
It exercises the real runtime inside the executable: FFmpeg discovery and video loading, Qt
Multimedia playback, captions (SRT, ASS, burn-in export), face detection and auto reframe,
speech activity, Whisper transcription with CPU fallback, GPU/NVENC detection, project and
settings persistence, and temporary-file cleanup. Each check is reported as passed, failed
or skipped (with the reason), and the process exits with 1 if any check failed.

``--assets`` may contain ``astronaut.png`` (a face photo, for the real face-tracking check)
and ``speech_en.flac`` (speech, for the speech-activity and Whisper checks).
``--expect-no-ffmpeg`` checks the behaviour when FFmpeg is missing instead.
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path

from ai_video_studio import APP_NAME, __version__


class Skip(Exception):
    pass


class SelfTest:
    def __init__(self, assets: Path | None, whisper_model: str | None, expect_no_ffmpeg: bool):
        self.assets = assets
        self.whisper_model = whisper_model
        self.expect_no_ffmpeg = expect_no_ffmpeg
        self.checks: list[dict] = []
        self.work = Path(tempfile.mkdtemp(prefix="avs-selftest-"))
        self.services = None
        self.clip: Path | None = None
        self.face_clip: Path | None = None

    # -- helpers --------------------------------------------------------------------------------
    def check(self, name: str, fn) -> None:
        started = time.monotonic()
        entry = {"name": name}
        try:
            detail = fn()
            entry.update(status="passed", detail=detail)
        except Skip as exc:
            entry.update(status="skipped", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            entry.update(status="failed", detail=f"{type(exc).__name__}: {exc}",
                         traceback=traceback.format_exc()[-2000:])
        entry["seconds"] = round(time.monotonic() - started, 2)
        self.checks.append(entry)
        mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[entry["status"]]
        print(f"[{mark}] {name}: {entry.get('detail')}", flush=True)

    def run_job(self, kind: str, payload: dict, timeout: float = 600):
        from ai_video_studio.jobs.models import JobState

        jobs = self.services.jobs
        job = jobs.submit(kind, f"self-test {kind}", payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = jobs.store.get(job.id)
            if current.state.is_finished:
                if current.state is not JobState.COMPLETED:
                    raise AssertionError(f"{kind} job {current.state.value}: {current.error}")
                return current.result
            time.sleep(0.1)
        jobs.cancel(job.id)
        raise AssertionError(f"{kind} job timed out")

    def ffmpeg(self) -> Path:
        return self.services.ffmpeg_path()

    def ffmpeg_run(self, args: list[str]) -> None:
        from ai_video_studio.core.process import run_process

        result = run_process([str(self.ffmpeg()), "-y", "-v", "error", *args], timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr[-500:])

    # -- checks ---------------------------------------------------------------------------------
    def runtime(self):
        from ai_video_studio.core import paths

        data = paths.data_dir().resolve()
        install = paths.exe_dir().resolve()
        inside = install in data.parents and paths.portable_data_dir() is None
        if inside:
            raise AssertionError(f"user data {data} is inside the program folder {install}")
        return {"frozen": paths.is_frozen(), "executable": sys.executable, "python": platform.python_version(),
                "platform": platform.platform(), "data_dir": str(data), "models_dir": str(paths.models_dir()),
                "portable": paths.portable_data_dir() is not None}

    def imports(self):
        import av
        import ctranslate2
        import cv2
        import faster_whisper
        import keyring
        import numpy
        import onnxruntime
        import PySide6
        from PySide6 import QtMultimedia

        return {"PySide6": PySide6.__version__, "opencv": cv2.__version__, "ctranslate2": ctranslate2.__version__,
                "faster_whisper": faster_whisper.__version__, "onnxruntime": onnxruntime.__version__,
                "av": av.__version__, "numpy": numpy.__version__,
                "keyring_backend": type(keyring.get_keyring()).__name__, "qt_multimedia": QtMultimedia.__name__}

    def missing_ffmpeg(self):
        from ai_video_studio.core.errors import DependencyMissingError
        from ai_video_studio.core.ffmpeg_tools import find_tool

        found = {n: find_tool(n, self.services.settings.get("ffmpeg_dir")) for n in ("ffmpeg", "ffprobe")}
        if any(found.values()):
            raise AssertionError(f"FFmpeg was found although it should be missing: {found}")
        try:
            self.services.ffmpeg_path()
        except DependencyMissingError as exc:
            if "Settings" not in str(exc):
                raise AssertionError(f"unhelpful message: {exc}") from exc
            return {"message": str(exc)}
        raise AssertionError("no error for missing FFmpeg")

    def ffmpeg_discovery(self):
        from ai_video_studio.core import paths

        status = self.services.tool_status()
        if not status.ok:
            raise AssertionError("FFmpeg/FFprobe not found")
        bundled = paths.exe_dir().resolve() in Path(status.ffmpeg).resolve().parents
        return {"ffmpeg": str(status.ffmpeg), "ffprobe": str(status.ffprobe), "bundled": bundled,
                "version": status.ffmpeg_version}

    def make_clip(self):
        self.clip = self.work / "self test clip.mp4"
        self.ffmpeg_run(["-f", "lavfi", "-i", "color=c=0x303030:s=640x360:r=25:d=4",
                         "-f", "lavfi", "-i", "color=c=red:s=60x60:r=25:d=4",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                         "-filter_complex", "[0:v][1:v]overlay=x='40+120*t':y=150[v]", "-map", "[v]",
                         "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "4",
                         str(self.clip)])
        return {"clip": str(self.clip)}

    def probe(self):
        from ai_video_studio.jobs.handlers import PROBE_JOB

        meta = self.run_job(PROBE_JOB, {"path": str(self.clip)})["metadata"]
        if (meta["width"], meta["height"]) != (640, 360) or not meta["has_audio"]:
            raise AssertionError(f"unexpected metadata {meta}")
        return {"duration": meta["duration"], "fps": meta["fps"], "video": meta["video_codec"],
                "audio": meta["audio_codec"]}

    def playback(self):
        from PySide6.QtCore import QCoreApplication, QUrl
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

        frames = []
        player = QMediaPlayer()
        sink = QVideoSink()
        sink.videoFrameChanged.connect(lambda f: frames.append(f.size()) if f.isValid() else None)
        player.setVideoSink(sink)
        player.setSource(QUrl.fromLocalFile(str(self.clip)))
        player.play()
        deadline = time.monotonic() + 20
        while not frames and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)
        player.stop()
        error = player.errorString()
        player.deleteLater()
        sink.deleteLater()
        QCoreApplication.processEvents()
        if not frames:
            raise AssertionError(f"Qt Multimedia decoded no frame ({error or 'no error reported'})")
        return {"frame_size": f"{frames[0].width()}x{frames[0].height()}", "frames": len(frames)}

    def captions(self):
        from ai_video_studio.captions.ass import build_ass
        from ai_video_studio.captions.model import CaptionSegment, CaptionTrack
        from ai_video_studio.captions.srt import read_srt_file, write_srt_file
        from ai_video_studio.captions.style import CaptionStyle
        from ai_video_studio.jobs.caption_jobs import BURN_IN_JOB

        track = CaptionTrack(segments=[CaptionSegment(0.2, 1.8, "Hello from the self-test"),
                                       CaptionSegment(2.0, 3.5, "বাংলা লাইন")])
        srt = self.work / "captions.srt"
        write_srt_file(srt, track)
        back = [s.text for s in read_srt_file(srt).segments]
        if back != [s.text for s in track.segments]:
            raise AssertionError(f"SRT round trip changed text: {back}")
        out = self.work / "captioned.mp4"
        result = self.run_job(BURN_IN_JOB, {
            "path": str(self.clip), "output": str(out), "ass": build_ass(track.segments, CaptionStyle(), 640, 360),
            "duration": 4, "audio_codec": "aac", "has_audio": True,
            "encoder": self.services.settings.get("video_encoder"), "quality": 23})
        if not out.is_file() or out.stat().st_size < 1000:
            raise AssertionError("no burned-in output")
        return {"srt": "round trip ok", "encoder": result["encoder"], "warnings": result["warnings"]}

    def smart_cut(self):
        from ai_video_studio.core.ffmpeg_tools import probe_video
        from ai_video_studio.jobs.render_jobs import RENDER_JOB, SMARTCUT_ANALYZE_JOB
        from ai_video_studio.render.output import OutputSettings
        from ai_video_studio.smartcut.levels import AudioLevels
        from ai_video_studio.smartcut.model import SmartCutState

        clip = self.work / "tone gaps.mp4"
        on = "lt(mod(t,2),1)"
        self.ffmpeg_run(["-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=6", "-f", "lavfi", "-i",
                         f"aevalsrc='0.5*sin(2*PI*440*t)*{on}':s=48000:d=6", "-c:v", "libx264", "-pix_fmt",
                         "yuv420p", "-c:a", "aac", "-t", "6", str(clip)])
        levels = AudioLevels.from_dict(self.run_job(SMARTCUT_ANALYZE_JOB, {"path": str(clip), "duration": 6,
                                                                           "has_audio": True})["levels"])
        state = SmartCutState(levels=levels)
        state.detect()
        keep = state.apply()
        silences = sum(1 for s in state.segments if s.kind == "silence")
        if silences != 3:
            raise AssertionError(f"expected 3 silences, found {silences}")
        out = self.work / "cut.mp4"
        result = self.run_job(RENDER_JOB, {"path": str(clip), "output": str(out), "duration": 6, "fps": 25,
                                           "has_audio": True, "audio_codec": "aac",
                                           "keep_ranges": [list(r) for r in keep],
                                           "output_settings": OutputSettings(quality="small").to_dict()})
        kept = sum(b - a for a, b in keep)
        duration = probe_video(str(out), self.services.ffprobe_path()).duration
        if abs(duration - kept) > 0.15:
            raise AssertionError(f"output {duration:.2f} s, expected {kept:.2f} s")
        return {"silences": silences, "output_seconds": round(duration, 2), "encoder": result["encoder"]}

    def resize(self):
        from ai_video_studio.core.ffmpeg_tools import probe_video
        from ai_video_studio.jobs.render_jobs import RENDER_FRAME_JOB, RENDER_JOB
        from ai_video_studio.render.layout import ResizeSettings, compute_layout
        from ai_video_studio.render.output import OutputSettings

        layout = compute_layout(640, 360, ResizeSettings(preset="custom", width=360, height=640, mode="blur"))
        stage = {"type": "layout", "layout": layout.to_dict()}
        out = self.work / "vertical.mp4"
        result = self.run_job(RENDER_JOB, {"path": str(self.clip), "output": str(out), "duration": 4, "fps": 25,
                                           "has_audio": True, "audio_codec": "aac", "stage": stage,
                                           "output_settings": OutputSettings(quality="small").to_dict()})
        meta = probe_video(str(out), self.services.ffprobe_path())
        if (meta.width, meta.height) != (360, 640):
            raise AssertionError(f"output is {meta.width}x{meta.height}")
        frame = self.run_job(RENDER_FRAME_JOB, {"path": str(self.clip), "at": 1.0, "stage": stage})
        image = Path(frame["image"])
        if not image.is_file():
            raise AssertionError("no frame preview")
        image.unlink()
        return {"output": f"{meta.width}x{meta.height}", "encoder": result["encoder"], "background": layout.background}

    def batch(self):
        from ai_video_studio.render.batch import BUILTIN_RENDER_PRESETS
        from ai_video_studio.render.naming import path_key, plan_outputs, render_name

        names = [n for n, _builtin in self.services.render_presets.names()]
        missing = [n for n in BUILTIN_RENDER_PRESETS if n not in names]
        if missing:
            raise AssertionError(f"missing presets: {missing}")
        name = render_name("{original_name}_{resolution}", {"original_name": 'a:b"c', "resolution": "1080x1920"})
        if name != "a_b_c_1080x1920":
            raise AssertionError(f"unsafe name {name!r}")
        clash = plan_outputs([("x", self.clip)], "replace", {path_key(self.clip)}, set())[0]
        if clash.path == self.clip:
            raise AssertionError("a source video was planned as an output")
        return {"presets": len(names), "max_concurrent_renders": self.services.jobs.max_heavy}

    def ai_manager(self):
        import json

        from ai_video_studio.ai.profiles import ProviderProfile
        from ai_video_studio.ai.router import AIRouter
        from ai_video_studio.storage import secrets

        profiles = self.services.ai_providers.list()
        if not profiles:
            raise AssertionError("no AI provider profiles")
        calls = []

        def transport(url, headers, body, timeout):  # no network: a stand-in provider
            calls.append(url)
            if len(calls) == 1:
                return 503, {}
            return 200, {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}

        test = [ProviderProfile("A", "openai_compatible", "m", id="a", base_url="http://localhost:9/v1",
                                key_required=False, max_retries=0, capabilities=["text", "caption_correction"]),
                ProviderProfile("B", "openai_compatible", "m", id="b", base_url="http://localhost:9/v1",
                                key_required=False, capabilities=["text", "caption_correction"])]
        result = AIRouter(test, lambda p: None, transport=transport, sleep=lambda s: None).request_json(
            "caption_correction", "s", "u")
        if result.profile_id != "b" or len(calls) != 2:
            raise AssertionError("fallback routing did not work")
        return {"profiles": len(profiles), "secure_key_store": secrets.secure_store_available(),
                "fallback": "ok"}

    def unreadable_model_cache(self):
        """The main window opens while a Whisper cache snapshot raises WinError 448 (simulated).

        v0.7.0 crashed at start-up in this situation. Runs in a temporary data folder, so the user's
        real models and settings are not touched.
        """
        import os
        import pathlib

        from ai_video_studio.captions import models
        from ai_video_studio.services import Services
        from ai_video_studio.ui.main_window import MainWindow

        class UntrustedMountPoint(OSError):
            winerror = 448

        temp = self.work / "model-cache-test"
        previous = os.environ.get("AVS_DATA_DIR")
        os.environ["AVS_DATA_DIR"] = str(temp)
        original = pathlib.Path.is_file

        def is_file(path, *a, **k):
            if "snapshots" in str(path):
                raise UntrustedMountPoint(22, "untrusted mount point", str(path))
            return original(path, *a, **k)

        try:
            snapshot = models.legacy_cache("small") / "snapshots" / "rev"
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text("{}")
            pathlib.Path.is_file = is_file
            services = Services(temp / "studio.db")
            try:
                window = MainWindow(services)
                status = window.settings_page.models.table.item(list(models.WHISPER_MODELS).index("small"), 2).text()
                window.close()
                window.deleteLater()
            finally:
                services.close()
        finally:
            pathlib.Path.is_file = original
            if previous is None:
                os.environ.pop("AVS_DATA_DIR", None)
            else:
                os.environ["AVS_DATA_DIR"] = previous
        if not status.startswith("Cannot be read"):
            raise AssertionError(f"unexpected model status {status!r}")
        return {"settings_status": status}

    def gpu(self):
        from ai_video_studio.captions.transcriber import cuda_device_count, cuda_libraries, device_attempts
        from ai_video_studio.core.ffmpeg_commands import available_encoders, choose_video_encoder, nvenc_usable
        from ai_video_studio.reframe.detection import opencv_cuda_devices

        ffmpeg = self.ffmpeg()
        encoders = available_encoders(ffmpeg)
        devices = cuda_device_count()
        libraries = cuda_libraries()
        missing = [name for name, path in libraries.items() if not path]
        whisper_first = device_attempts("auto", devices, not missing)[0][0]
        return {"ctranslate2_cuda_devices": devices, "cuda_libraries_missing": missing,
                "whisper_auto_device": whisper_first,
                "nvenc_listed": "h264_nvenc" in encoders, "nvenc_usable": nvenc_usable(ffmpeg),
                "hevc_nvenc_listed": "hevc_nvenc" in encoders,
                "hevc_nvenc_usable": nvenc_usable(ffmpeg, "hevc_nvenc"),
                "libx265": "libx265" in encoders,
                "auto_export_encoder_h264": choose_video_encoder("auto", ffmpeg, "h264")[0],
                "opencv_cuda_devices": opencv_cuda_devices()}

    def face_detection(self):
        import numpy as np

        from ai_video_studio.core import paths
        from ai_video_studio.reframe.detection import YUNET_FILE, build_detectors

        bundled = paths.app_root() / "models" / YUNET_FILE
        if paths.is_frozen() and not bundled.is_file():
            raise AssertionError(f"the packaged YuNet model is missing: {bundled}")
        detectors = build_detectors(paths.models_dir(), detect_persons=True, allow_download=False)
        if "YuNet" not in detectors.description:
            if not paths.is_frozen():
                raise Skip("not packaged and the YuNet model is not downloaded yet")
            raise AssertionError(f"YuNet not used: {detectors.description} {detectors.warnings}")
        detectors(np.zeros((360, 640, 3), np.uint8))
        return {"detectors": detectors.description, "model": str(bundled if bundled.is_file() else "downloaded copy")}

    def reframe(self):
        from ai_video_studio.jobs.reframe_jobs import REFRAME_ANALYZE_JOB, REFRAME_EXPORT_JOB
        from ai_video_studio.reframe.framing import compute_crop_path
        from ai_video_studio.reframe.model import ReframeSettings, TrackingAnalysis

        image = self.assets / "astronaut.png" if self.assets else None
        if image is not None and image.is_file():
            clip = self.work / "face clip.mp4"
            self.ffmpeg_run(["-f", "lavfi", "-i", "color=c=0x283040:s=1280x720:r=25:d=5", "-loop", "1",
                             "-i", str(image), "-f", "lavfi", "-i", "sine=duration=5", "-filter_complex",
                             "[1:v]scale=300:300[f];[0:v][f]overlay=x='60+150*t':y=200:shortest=1[v]",
                             "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                             "-t", "5", str(clip)])
            size, duration = (1280, 720), 5.0
        else:
            clip, size, duration = self.clip, (640, 360), 4.0
        settings = ReframeSettings(tracking_mode="auto_face", caption_safe="off")
        result = self.run_job(REFRAME_ANALYZE_JOB, {
            "path": str(clip), "width": size[0], "height": size[1], "duration": duration, "sample_fps": 5,
            "features": settings.analysis_features(), "face_threshold": 0.6})
        analysis = TrackingAnalysis.from_dict(result["analysis"])
        faces = [t for t in analysis.tracks if t.kind == "face"]
        if clip is not self.clip and not faces:
            raise AssertionError("no face tracked in the face clip")
        if faces:
            path = compute_crop_path(analysis, settings, size[0], size[1], duration)
        else:
            path = compute_crop_path(None, ReframeSettings(tracking_mode="center"), size[0], size[1], duration)
        out = self.work / "reframed.mp4"
        export = self.run_job(REFRAME_EXPORT_JOB, {
            "path": str(clip), "output": str(out), "src_w": size[0], "src_h": size[1], "crop_w": path.crop_w,
            "crop_h": path.crop_h, "keyframes": path.keyframes(), "out_w": 360, "out_h": 640, "fps": 25,
            "duration": duration, "audio_codec": "aac", "has_audio": True, "encoder": "libx264", "quality": 26})
        from ai_video_studio.jobs.handlers import PROBE_JOB

        meta = self.run_job(PROBE_JOB, {"path": str(out)})["metadata"]
        if (meta["width"], meta["height"]) != (360, 640) or not meta["has_audio"]:
            raise AssertionError(f"unexpected reframed output {meta}")
        return {"face_tracks": len(faces), "moving_crop": export["moving_crop"], "detector": analysis.detector,
                "output": f"{meta['width']}x{meta['height']} {meta['fps']} fps"}

    def speech(self):
        from ai_video_studio.reframe.speech import analyse_speech, extract_speech_audio

        source = self.assets / "speech_en.flac" if self.assets else None
        if source is None or not source.is_file():
            raise Skip("no speech_en.flac in --assets")
        wav = self.work / "speech.wav"
        extract_speech_audio(self.ffmpeg(), str(source), str(wav))
        activity = analyse_speech(str(wav))
        segments = activity.segments(0.5)
        if not segments:
            raise AssertionError("no speech found in the speech sample")
        return {"detector": activity.detector, "segments": [(round(a, 2), round(b, 2)) for a, b in segments]}

    def whisper(self):
        from ai_video_studio.captions.models import WHISPER_MODELS, installed_path
        from ai_video_studio.captions.transcriber import TranscriptionOptions, transcribe_file
        from ai_video_studio.reframe.speech import extract_speech_audio

        source = self.assets / "speech_en.flac" if self.assets else None
        if source is None or not source.is_file():
            raise Skip("no speech_en.flac in --assets")
        model = self.whisper_model
        if model in WHISPER_MODELS:
            folder = installed_path(model)
            if folder is None:
                raise Skip(f"Whisper model '{model}' is not downloaded")
            model = str(folder)
        if not model or not Path(model).is_dir():
            raise Skip("no Whisper model given (--whisper-model NAME_OR_FOLDER)")
        wav = self.work / "whisper.wav"
        extract_speech_audio(self.ffmpeg(), str(source), str(wav))
        result = transcribe_file(str(wav), TranscriptionOptions(model_path=model, language="en", device="auto"))
        text = " ".join(s.text for s in result.track.segments).lower()
        if not any(w in text for w in ("hello", "welcome", "video")):
            raise AssertionError(f"unexpected transcript: {text!r}")
        return {"device": result.device, "compute_type": result.compute_type, "warnings": result.warnings,
                "text": text[:120]}

    def models(self):
        from ai_video_studio.captions.models import WHISPER_MODELS, model_status

        return {name: model_status(name)["downloaded"] for name in WHISPER_MODELS}

    def project(self):
        from ai_video_studio.core.media import MediaItem
        from ai_video_studio.reframe.model import ReframeSettings, ReframeState
        from ai_video_studio.storage.project import Project

        project = Project(name="Self-test project")
        item = MediaItem(path=str(self.clip or self.work / "missing.mp4"))
        item.reframe = ReframeState(ReframeSettings(tracking_mode="active_speaker", preset="1:1"))
        project.media.append(item)
        path = project.save(self.work / "self test.avsproj")
        loaded = Project.load(path)
        if loaded.media[0].reframe.settings.tracking_mode != "active_speaker":
            raise AssertionError("tracking mode not restored")
        return {"saved": str(path)}

    def settings_persistence(self):
        previous = self.services.settings.get("preview_volume")
        self.services.settings.set("preview_volume", 37)
        return {"preview_volume_before": previous, "now": 37,
                "persisted_from_previous_run": previous == 37}

    def temp_cleanup(self):
        from ai_video_studio.core import paths

        left = [p.name for p in paths.temp_dir().glob("job-*")]
        if left:
            raise AssertionError(f"job folders left in the temp folder: {left}")
        return {"temp_dir": str(paths.temp_dir())}

    # -- run ------------------------------------------------------------------------------------
    def run(self) -> dict:
        from ai_video_studio.services import Services

        self.services = Services()
        try:
            self.check("runtime", self.runtime)
            self.check("python modules", self.imports)
            if self.expect_no_ffmpeg:
                self.check("missing FFmpeg is reported", self.missing_ffmpeg)
                self.check("face detection (bundled YuNet)", self.face_detection)
                self.check("project save and reopen", self.project)
            else:
                self.check("FFmpeg and FFprobe discovery", self.ffmpeg_discovery)
                self.check("generate test video", self.make_clip)
                self.check("video metadata (probe job)", self.probe)
                self.check("Qt Multimedia playback", self.playback)
                self.check("captions: SRT and burn-in export", self.captions)
                self.check("GPU / CUDA / NVENC detection", self.gpu)
                self.check("face detection (bundled YuNet)", self.face_detection)
                self.check("auto reframe analysis and export", self.reframe)
                self.check("smart cut: silence detection and cut export", self.smart_cut)
                self.check("resize & crop: blur background export and exact frame", self.resize)
                self.check("bulk processing: presets, safe names, render limit", self.batch)
                self.check("speech activity (Silero VAD)", self.speech)
                self.check("Whisper transcription (device auto, CPU fallback)", self.whisper)
                self.check("project save and reopen", self.project)
                self.check("temporary files cleaned up", self.temp_cleanup)
            self.check("AI provider manager (stand-in provider, no network)", self.ai_manager)
            self.check("start-up with an unreadable model cache (WinError 448, simulated)",
                       self.unreadable_model_cache)
            self.check("downloaded Whisper models", self.models)
            self.check("settings persistence", self.settings_persistence)
        finally:
            self.services.close()
        failed = [c["name"] for c in self.checks if c["status"] == "failed"]
        return {"app": APP_NAME, "version": __version__, "ok": not failed, "failed": failed, "checks": self.checks}


def run_self_test(argv: list[str]) -> int:
    def value(flag: str) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None

    assets = Path(value("--assets")) if value("--assets") else None
    report_path = value("--report")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])  # Qt Multimedia needs an application object
    tester = SelfTest(assets, value("--whisper-model"), "--expect-no-ffmpeg" in argv)
    report = tester.run()
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    print(f"\nSelf-test {'PASSED' if report['ok'] else 'FAILED'}: "
          f"{sum(c['status'] == 'passed' for c in report['checks'])} passed, "
          f"{sum(c['status'] == 'skipped' for c in report['checks'])} skipped, {len(report['failed'])} failed",
          flush=True)
    del app
    return 0 if report["ok"] else 1
