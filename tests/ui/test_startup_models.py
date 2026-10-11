"""The main window must open even when Whisper model folders cannot be inspected (v0.7.0 crash)."""

import logging
import pathlib

from ai_video_studio.captions import models
from ai_video_studio.services import Services
from ai_video_studio.ui.main_window import MainWindow


class UntrustedMountPoint(OSError):
    winerror = 448


def test_window_opens_and_settings_explain_unreadable_models(qtbot, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    snapshot = models.legacy_cache("small") / "snapshots" / "536b0662742c02347bc0e980a01041f333bce120"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    original = pathlib.Path.is_file

    def is_file(self, *a, **k):
        if "snapshots" in str(self):
            raise UntrustedMountPoint(22, "The path cannot be traversed because it contains an untrusted "
                                          "mount point", str(self))
        return original(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "is_file", is_file)
    services = Services(tmp_path / "ui.db")
    window = MainWindow(services)  # raised OSError [WinError 448] in v0.7.0
    window.show()
    card = window.settings_page.models
    row = list(models.WHISPER_MODELS).index("small")
    assert card.table.item(row, 2).text().startswith("Cannot be read")
    assert "WinError 448" in card.problems.text()
    logged = [r for r in caplog.records if r.getMessage() == "Whisper model is not usable"]
    assert [(r.model, r.state) for r in logged] == [("small", "inaccessible")]
    card.refresh()  # the same problem is logged once, not on every refresh
    assert len([r for r in caplog.records if r.getMessage() == "Whisper model is not usable"]) == 1
    window.navigate("Captions")  # other pages keep working
    window.navigate("Smart Cut")
    window.close()
    window.deleteLater()
    services.close()
