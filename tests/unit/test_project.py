import json

import pytest

from ai_video_studio.core.errors import ProjectError
from ai_video_studio.core.ffmpeg_tools import VideoMetadata
from ai_video_studio.core.media import MediaStatus, validate_video_path
from ai_video_studio.storage.project import PROJECT_VERSION, Project, RecentProjects


def _video(tmp_path, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"\0" * 10)
    return path


def test_save_and_load_round_trip(tmp_path):
    clip = _video(tmp_path)
    project = Project(name="Demo")
    item = project.add_media(str(clip))
    item.metadata = VideoMetadata(duration=2.0, width=1920, height=1080, fps=30, video_codec="h264")
    assert project.dirty
    saved = project.save(tmp_path / "proj" / "demo")
    assert saved.suffix == ".avsproj" and not project.dirty

    loaded = Project.load(saved)
    assert loaded.id == project.id and loaded.name == "Demo"
    assert len(loaded.media) == 1
    assert loaded.media[0].metadata == item.metadata
    assert loaded.media[0].status is MediaStatus.READY


def test_duplicate_media_is_ignored(tmp_path):
    clip = _video(tmp_path)
    project = Project(name="Demo")
    assert project.add_media(str(clip)) is not None
    assert project.add_media(str(tmp_path / "." / clip.name)) is None
    assert len(project.media) == 1


def test_remove_media(tmp_path):
    project = Project(name="Demo")
    a = project.add_media(str(_video(tmp_path, "a.mp4")))
    project.add_media(str(_video(tmp_path, "b.mp4")))
    project.dirty = False
    assert project.remove_media({a.id}) == 1
    assert [m.name for m in project.media] == ["b.mp4"] and project.dirty


def test_missing_media_status_and_relative_relink(tmp_path):
    folder = tmp_path / "orig"
    folder.mkdir()
    clip = _video(folder)
    project = Project(name="Move")
    project.add_media(str(clip))
    project.save(folder / "move.avsproj")

    moved = tmp_path / "moved"
    folder.rename(moved)
    loaded = Project.load(moved / "move.avsproj")
    assert loaded.media[0].path == str((moved / "clip.mp4").resolve())
    assert loaded.media[0].status is MediaStatus.PENDING

    (moved / "clip.mp4").unlink()
    assert Project.load(moved / "move.avsproj").media[0].status is MediaStatus.MISSING


@pytest.mark.parametrize("content,message", [
    ("{not json", "not valid JSON"),
    ("[]", "Not an AI Video Studio"),
    (json.dumps({"format": "other"}), "Not an AI Video Studio"),
    (json.dumps({"format": "ai-video-studio-project", "version": PROJECT_VERSION + 1}), "newer"),
    (json.dumps({"format": "ai-video-studio-project", "version": 1}), "damaged"),
])
def test_malformed_project_files(tmp_path, content, message):
    path = tmp_path / "bad.avsproj"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ProjectError, match=message):
        Project.load(path)


def test_load_missing_file(tmp_path):
    with pytest.raises(ProjectError):
        Project.load(tmp_path / "nope.avsproj")


def test_save_failure_keeps_previous_state(tmp_path):
    project = Project(name="X")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ProjectError):
        project.save(blocker / "sub" / "x.avsproj")
    assert project.path is None


def test_recent_projects(db, tmp_path):
    recent = RecentProjects(db)
    project = Project(name="R")
    project.save(tmp_path / "r.avsproj")
    recent.touch(project)
    entries = recent.list()
    assert entries[0]["name"] == "R" and entries[0]["exists"]
    recent.remove(entries[0]["path"])
    assert recent.list() == []


def test_validate_video_path(tmp_path):
    assert validate_video_path(tmp_path / "none.mp4") == "File does not exist"
    assert validate_video_path(tmp_path) == "Not a file"
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    assert "Unsupported" in validate_video_path(txt)
    empty = tmp_path / "e.mp4"
    empty.write_bytes(b"")
    assert validate_video_path(empty) == "File is empty"
    assert validate_video_path(_video(tmp_path, "ok.MP4")) is None
