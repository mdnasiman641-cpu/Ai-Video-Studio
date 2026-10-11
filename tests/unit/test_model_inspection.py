"""Whisper model discovery must never crash, whatever state the model folders are in.

Regression tests for the v0.7.0 start-up crash: ``Path.is_file()`` raised
``OSError: [WinError 448] ... untrusted mount point`` on a Hugging Face cache snapshot (whose
files are symbolic links into ``blobs``) while the Settings page was being built. Windows-only
errors are simulated by patching ``Path.is_file`` so the tests run on every platform.
"""

import hashlib
import os
import pathlib
import sys

import pytest

from ai_video_studio.captions import models
from ai_video_studio.core.download import DownloadError, git_blob_sha1
from ai_video_studio.storage import settings as settings_module

needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs Developer Mode")


class UntrustedMountPoint(OSError):
    """What Windows raises for WinError 448 (``winerror`` only exists on Windows otherwise)."""

    winerror = 448

    def __init__(self, path):
        super().__init__(22, "The path cannot be traversed because it contains an untrusted mount point", str(path))


MODEL_BIN = b"model weights" * 1000
CONFIG = b'{"alignment_heads": []}'
TOKENIZER = b'{"version": "1.0"}'


def write_model(folder, files=("model.bin", "config.json", "tokenizer.json")):
    folder.mkdir(parents=True, exist_ok=True)
    data = {"model.bin": MODEL_BIN, "config.json": CONFIG, "tokenizer.json": TOKENIZER}
    for name in files:
        (folder / name).write_bytes(data[name])
    return folder


def write_hf_cache(name="small", links=True, corrupt=False):
    """The layout huggingface_hub creates: snapshots/<rev>/<file> -> ../../blobs/<checksum>."""
    cache = models.legacy_cache(name)
    blobs = cache / "blobs"
    snapshot = cache / "snapshots" / "536b0662742c02347bc0e980a01041f333bce120"
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    for filename, data in (("model.bin", MODEL_BIN), ("config.json", CONFIG), ("tokenizer.json", TOKENIZER)):
        blob = blobs / "tmp"
        blob.write_bytes(data)
        blob_name = hashlib.sha256(data).hexdigest() if filename == "model.bin" else git_blob_sha1(blob)
        blob.rename(blobs / blob_name)
        if corrupt and filename == "model.bin":
            (blobs / blob_name).write_bytes(b"damaged")
        if links:
            os.symlink(os.path.join("..", "..", "blobs", blob_name), snapshot / filename)
        else:
            (snapshot / filename).write_bytes(data)
    return cache, snapshot


@pytest.fixture
def refuse(monkeypatch):
    """Make ``Path.is_file``/``is_dir`` raise for paths that match a predicate."""
    original_file, original_dir = pathlib.Path.is_file, pathlib.Path.is_dir

    def install(match, error=UntrustedMountPoint, dirs=False):
        def is_file(self, *a, **k):
            if match(self):
                raise error(self)
            return original_file(self, *a, **k)

        def is_dir(self, *a, **k):
            if dirs and match(self):
                raise error(self)
            return original_dir(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "is_file", is_file)
        monkeypatch.setattr(pathlib.Path, "is_dir", is_dir)

    return install


def test_valid_model_and_missing_model():
    assert models.inspect_model("tiny").state == "not_downloaded"
    status = models.model_status("tiny")
    assert status["state"] == "not_downloaded" and not status["downloaded"] and status["problem"] == ""
    write_model(models.whisper_root() / "tiny")
    info = models.inspect_model("tiny")
    assert info.state == "installed" and info.path == models.whisper_root() / "tiny"
    assert models.model_status("tiny")["size_mb"] is not None


def test_missing_model_bin_is_incomplete():
    write_model(models.whisper_root() / "base", files=("config.json", "tokenizer.json"))
    info = models.inspect_model("base")
    assert info.state == "incomplete" and info.path is None and "model.bin" in info.problems[0]


def test_win_error_448_on_snapshot_is_reported_not_raised(refuse):
    write_model(models.legacy_cache("small") / "snapshots" / "rev", files=("config.json", "tokenizer.json"))
    refuse(lambda p: "snapshots" in str(p) and p.name == "model.bin")
    status = models.model_status("small")  # crashed with OSError before the fix
    assert status["state"] == "inaccessible" and not status["downloaded"]
    assert "WinError 448" in status["problem"] and "untrusted mount point" in status["problem"]
    assert models.installed_path("small") is None


def test_permission_error_is_reported(refuse):
    write_model(models.whisper_root() / "medium")
    refuse(lambda p: p.name == "model.bin", error=lambda p: PermissionError(13, "Access is denied", str(p)))
    info = models.inspect_model("medium")
    assert info.state == "inaccessible" and "denied" in info.problems[0]


def test_unreadable_models_folder_itself(refuse):
    refuse(lambda p: p.name == "small" and "whisper" in str(p), dirs=True)
    info = models.inspect_model("small")
    assert info.state == "inaccessible" and "WinError 448" in info.problems[0]


def test_programming_errors_are_not_hidden(monkeypatch):
    monkeypatch.setattr(models, "inspect_folder", lambda folder: (_ for _ in ()).throw(TypeError("bug")))
    with pytest.raises(TypeError):
        models.inspect_model("tiny")


@needs_symlinks
def test_readable_hf_cache_with_links_is_used_in_place():
    _cache, snapshot = write_hf_cache()
    info = models.inspect_model("small")
    assert info.state == "installed" and info.path == snapshot


@needs_symlinks
def test_broken_links_are_not_a_model():
    cache, _snapshot = write_hf_cache()
    for blob in (cache / "blobs").iterdir():
        blob.unlink()
    info = models.inspect_model("small")
    assert info.state != "installed" and info.importable is None
    assert models.model_status("small")["downloaded"] is False


@needs_symlinks
def test_untraversable_cache_is_imported_without_following_links(refuse):
    cache, snapshot = write_hf_cache()
    refuse(lambda p: "snapshots" in str(p))  # Windows refuses every path through the snapshot folder
    info = models.inspect_model("small")
    assert info.state == "inaccessible" and info.importable == snapshot
    assert "can be imported" in models.model_status("small")["problem"]

    def no_network(*a, **k):
        raise AssertionError("must not download")

    target = models.download_model("small", opener=no_network)
    assert target == models.whisper_root() / "small"
    assert (target / "model.bin").read_bytes() == MODEL_BIN and not (target / "model.bin").is_symlink()
    assert models.inspect_model("small").state == "installed"
    # The old cache is untouched.
    assert (snapshot / "model.bin").is_symlink() and len(list((cache / "blobs").iterdir())) == 3


@needs_symlinks
def test_damaged_cached_blob_is_not_imported(refuse):
    _cache, snapshot = write_hf_cache(corrupt=True)
    refuse(lambda p: "snapshots" in str(p))
    with pytest.raises(DownloadError, match="damaged"):
        models.import_from_cache("small", snapshot)
    assert not (models.whisper_root() / "small").exists()


@needs_symlinks
def test_links_outside_the_cache_are_refused(tmp_path, refuse):
    cache, snapshot = write_hf_cache()
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(MODEL_BIN)
    (snapshot / "model.bin").unlink()
    os.symlink(outside, snapshot / "model.bin")
    refuse(lambda p: "snapshots" in str(p))
    assert models.inspect_model("small").importable is None


def test_plain_file_cache_copy_is_used_in_place():
    _cache, snapshot = write_hf_cache(links=False)
    assert models.inspect_model("small").path == snapshot


def test_custom_model_folder_status(tmp_path, refuse):
    good = write_model(tmp_path / "custom")
    assert models.custom_folder_status(str(good))[0] == "installed"
    assert models.custom_folder_status(str(tmp_path / "missing"))[0] == "not_downloaded"
    refuse(lambda p: p.name == "model.bin")
    state, message = models.custom_folder_status(str(good))
    assert state == "inaccessible" and "WinError 448" in message


def test_settings_folder_validation_never_raises(refuse, tmp_path):
    refuse(lambda p: p.name == "locked", dirs=True)
    assert "cannot be accessed" in settings_module._optional_dir(str(tmp_path / "locked"))
    assert settings_module._optional_dir("") is None
