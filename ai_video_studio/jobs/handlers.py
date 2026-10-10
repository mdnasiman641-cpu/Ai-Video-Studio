"""Job handlers. Each handler runs on a worker thread and returns a JSON-serialisable result."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ai_video_studio.core.ffmpeg_tools import probe_video
from ai_video_studio.jobs.manager import JobContext
from ai_video_studio.jobs.models import Job

PROBE_JOB = "probe_media"


def make_probe_handler(get_ffprobe: Callable[[], Path]):
    """Read metadata for ``payload['path']`` with FFprobe."""

    def handle(job: Job, ctx: JobContext) -> dict:
        ffprobe = get_ffprobe()
        ctx.report_progress(0.1)
        meta = probe_video(job.payload["path"], ffprobe, cancel_event=ctx.cancel_event)
        return {"media_id": job.payload.get("media_id"), "metadata": meta.to_dict()}

    return handle
