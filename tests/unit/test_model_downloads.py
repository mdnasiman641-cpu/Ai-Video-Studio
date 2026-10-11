"""Model downloads against a local HTTP server that imitates the Hugging Face API.

Real downloads from huggingface.co are tested by the Windows build workflow, through the
packaged executable (``AI-Video-Studio-Console.exe --download-model tiny``).
"""

import hashlib
import http.server
import json
import threading

import pytest

from ai_video_studio.captions import models
from ai_video_studio.captions.models import ModelMissingError, download_model, installed_path, model_status, remove_model
from ai_video_studio.core.download import DownloadError, download_file, git_blob_sha1
from ai_video_studio.core.errors import JobCancelled
from ai_video_studio.jobs.caption_jobs import MODEL_DOWNLOAD_JOB, TRANSCRIBE_JOB, make_model_download_handler, make_transcribe_handler
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.store import JobStore

FILES = {
    "model.bin": b"M" * 300_000,
    "config.json": b'{"alignment_heads": []}',
    "tokenizer.json": b'{"version": "1.0"}',
    "vocabulary.txt": b"a\nb\n",
    "README.md": b"# readme",
}
LFS = {"model.bin"}


def blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class FakeHub:
    """Serves /api/models/<repo>/revision/main and /<repo>/resolve/<rev>/<file>."""

    def __init__(self):
        self.requests: list[tuple[str, str | None]] = []
        self.drop_after: dict[str, int] = {}      # file -> cut the connection after N bytes (once)
        self.corrupt: set[str] = set()            # serve wrong bytes (always)
        self.missing: set[str] = set()
        hub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                hub.requests.append((self.path, self.headers.get("Range")))
                if "/api/models/" in self.path:
                    siblings = []
                    for name, data in FILES.items():
                        entry = {"rfilename": name, "size": len(data), "blobId": blob_sha1(data)}
                        if name in LFS:
                            entry["lfs"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                        siblings.append(entry)
                    body = json.dumps({"sha": "abc123", "siblings": siblings}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                name = self.path.rsplit("/", 1)[-1]
                if name in hub.missing or name not in FILES:
                    self.send_error(404)
                    return
                data = FILES[name]
                if name in hub.corrupt:
                    data = b"X" * len(data)
                start = 0
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.split("=")[1].split("-")[0])
                    self.send_response(206)
                else:
                    self.send_response(200)
                body = data[start:]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                cut = hub.drop_after.pop(name, None)
                if cut is not None:
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hub():
    server = FakeHub()
    yield server
    server.close()


def no_sleep(_s):
    pass


def test_download_verifies_and_records_the_model(hub, monkeypatch):
    monkeypatch.setattr("ai_video_studio.core.download.time.sleep", no_sleep)
    assert installed_path("tiny") is None
    seen = []
    folder = download_model("tiny", progress=lambda f, text: seen.append((f, text)), base_url=hub.url)
    assert folder == models.whisper_root() / "tiny" and installed_path("tiny") == folder
    assert (folder / "model.bin").read_bytes() == FILES["model.bin"]
    assert not (folder / "README.md").exists()
    manifest = json.loads((folder / "download.json").read_text())
    assert manifest["revision"] == "abc123"
    assert seen and seen[-1][0] == pytest.approx(1.0, abs=0.01) and "tiny" in seen[-1][1]
    assert any("/resolve/abc123/model.bin" in path for path, _ in hub.requests)  # pinned to the revision
    status = model_status("tiny")
    assert status["downloaded"] and status["size_mb"] == 0
    hub.requests.clear()
    assert download_model("tiny", base_url=hub.url) == folder and not hub.requests  # reused, offline
    assert remove_model("tiny") and installed_path("tiny") is None


def test_interrupted_download_resumes_with_a_range_request(hub, monkeypatch):
    monkeypatch.setattr("ai_video_studio.core.download.time.sleep", no_sleep)
    hub.drop_after["model.bin"] = 100_000
    folder = download_model("tiny", base_url=hub.url)
    assert (folder / "model.bin").read_bytes() == FILES["model.bin"]
    ranges = [r for path, r in hub.requests if path.endswith("model.bin")]
    assert ranges[0] is None and ranges[1] and ranges[1].startswith("bytes=")


def test_corrupt_download_is_rejected(hub, monkeypatch):
    monkeypatch.setattr("ai_video_studio.core.download.time.sleep", no_sleep)
    hub.corrupt.add("model.bin")
    with pytest.raises(DownloadError, match="checksum"):
        download_model("tiny", base_url=hub.url)
    assert installed_path("tiny") is None


def test_small_files_are_checked_against_the_git_object_id(hub, monkeypatch):
    monkeypatch.setattr("ai_video_studio.core.download.time.sleep", no_sleep)
    hub.corrupt.add("config.json")
    with pytest.raises(DownloadError, match="checksum"):
        download_model("tiny", base_url=hub.url)


def test_missing_file_and_unreachable_server(hub, monkeypatch):
    monkeypatch.setattr("ai_video_studio.core.download.time.sleep", no_sleep)
    hub.missing.add("tokenizer.json")
    with pytest.raises(DownloadError, match="tokenizer.json"):
        download_model("tiny", base_url=hub.url)
    with pytest.raises(DownloadError, match="Could not reach"):
        download_model("base", base_url="http://127.0.0.1:9")


def test_not_enough_disk_space(hub, monkeypatch):
    class Usage:
        free = 10

    monkeypatch.setattr(models.shutil, "disk_usage", lambda _p: Usage())
    with pytest.raises(DownloadError, match="disk space"):
        download_model("tiny", base_url=hub.url)


def test_cancelled_download_keeps_the_partial_file_for_resuming(hub, tmp_path):
    calls = {"n": 0}

    def cancel_after_first_chunk():
        calls["n"] += 1
        if calls["n"] > 3:
            raise JobCancelled("Cancelled by user")

    FILES_BIG = b"Z" * (3 << 20)
    target = tmp_path / "big.bin"

    class Response:
        status = 200
        headers = {"Content-Length": str(len(FILES_BIG))}

        def __init__(self):
            self.pos = 0

        def read(self, n):
            chunk = FILES_BIG[self.pos:self.pos + n]
            self.pos += n
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with pytest.raises(JobCancelled):
        download_file("http://x/big.bin", target, size=len(FILES_BIG), opener=lambda r, t: Response(),
                      check_cancelled=cancel_after_first_chunk)
    assert not target.exists() and (tmp_path / "big.bin.part").exists()


def test_models_from_earlier_versions_are_reused(monkeypatch):
    from ai_video_studio.core import paths

    snapshot = paths.models_dir() / "models--Systran--faster-whisper-small" / "snapshots" / "deadbeef"
    snapshot.mkdir(parents=True)
    for name in ("model.bin", "config.json", "vocabulary.txt"):
        (snapshot / name).write_bytes(b"x")
    assert installed_path("small") == snapshot


def test_git_blob_sha1_matches_git(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello\n")
    assert git_blob_sha1(path) == "ce013625030ba8dba906f756967f9e9ca394464a"  # `git hash-object`


def test_transcription_never_downloads_without_consent(db, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    manager = JobManager(JobStore(db), 1)
    manager.register(TRANSCRIBE_JOB, make_transcribe_handler(lambda: "ffmpeg"))
    try:
        job = manager.submit(TRANSCRIBE_JOB, "t", {"path": str(video), "model": "small", "language": "en"})
        assert manager.wait_idle(30)
        job = manager.store.get(job.id)
    finally:
        manager.shutdown()
    assert job.state is JobState.FAILED and "not downloaded yet" in job.error and "485 MB" in job.error


def test_model_download_job(db, hub, monkeypatch):
    monkeypatch.setattr("ai_video_studio.core.download.time.sleep", no_sleep)
    manager = JobManager(JobStore(db), 1)
    manager.register(MODEL_DOWNLOAD_JOB, make_model_download_handler(base_url=hub.url))
    try:
        job = manager.submit(MODEL_DOWNLOAD_JOB, "d", {"model": "tiny"})
        assert manager.wait_idle(30)
        job = manager.store.get(job.id)
    finally:
        manager.shutdown()
    assert job.state is JobState.COMPLETED, job.error
    assert installed_path("tiny") is not None


def test_unknown_model_is_refused():
    with pytest.raises(ModelMissingError):
        download_model("huge-v9")
