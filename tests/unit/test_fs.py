import os

from ai_video_studio.core import fs


def test_remove_file_retries_until_unlocked(tmp_path, monkeypatch):
    target = tmp_path / "out.part.mp4"
    target.write_bytes(b"x")
    real_unlink = os.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked by another process")  # what Windows reports
        return real_unlink(self)

    monkeypatch.setattr(type(target), "unlink", flaky_unlink)
    assert fs.remove_file(target, delay=0)
    assert calls["n"] == 3 and not target.exists()


def test_remove_file_gives_up_and_reports(tmp_path, monkeypatch):
    target = tmp_path / "stuck"
    target.write_bytes(b"x")
    monkeypatch.setattr(type(target), "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert fs.remove_file(target, attempts=3, delay=0) is False


def test_remove_missing_and_tree(tmp_path):
    assert fs.remove_file(tmp_path / "missing")
    folder = tmp_path / "job"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "a.txt").write_text("a")
    assert fs.remove_tree(folder) and not folder.exists()
    assert fs.remove_tree(folder)
