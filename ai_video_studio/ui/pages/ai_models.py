"""AI Models page: provider profiles, priorities, keys, tests, routing and usage.

Keys are written to the Windows Credential Manager (or read from an environment variable the
profile names) and are never shown again, logged or saved in the database. Tests and requests
run as background jobs so the window stays responsive.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.ai.profiles import (
    CAPABILITIES,
    DEFAULT_BASE_URLS,
    DEFAULT_CAPABILITIES,
    DEFAULT_MODELS,
    MEDIA_CAPABILITIES,
    MEDIA_NOTE,
    PROVIDER_TYPES,
    ProviderProfile,
)
from ai_video_studio.core.errors import StudioError
from ai_video_studio.jobs.ai_jobs import AI_TEST_JOB
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.services import Services
from ai_video_studio.storage import secrets
from ai_video_studio.ui.project_controller import JobBridge
from ai_video_studio.ui.widgets.common import Card, muted, page_title, section_title

log = logging.getLogger(__name__)

COLUMNS = ["#", "Name", "Type", "Model", "Enabled", "Capabilities", "API key", "Last test"]
APP_FEATURES = {"caption_correction": "Caption correction (Captions → AI Check)",
                "titles": "Title and description (Captions → Suggest Title)"}
SHORT_CAPS = {"text": "text", "json": "JSON", "caption_correction": "captions", "titles": "titles"}


def _price_box() -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(-1, 10_000)
    box.setDecimals(4)
    box.setSpecialValueText("Not set")
    box.setValue(-1)
    box.setSuffix(" per 1M tokens")
    return box


class ProfileDialog(QDialog):
    def __init__(self, parent: QWidget | None, profile: ProviderProfile | None, key_source: str | None):
        super().__init__(parent)
        self.setWindowTitle("Edit AI Provider" if profile else "Add AI Provider")
        self.is_new = profile is None
        self.profile = profile or ProviderProfile("", "openai_compatible", "", capabilities=list(
            DEFAULT_CAPABILITIES["openai_compatible"]), key_required=False)
        p = self.profile
        self.name = QLineEdit(p.display_name)
        self.type = QComboBox()
        for key, label in PROVIDER_TYPES.items():
            self.type.addItem(label, key)
        self.type.setCurrentIndex(self.type.findData(p.provider_type))
        self.type.setEnabled(self.is_new)  # changing the type of a saved profile would mix up its settings
        self.base_url = QLineEdit(p.base_url)
        self.model = QLineEdit(p.model)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(p.enabled)
        self.caps: dict[str, QCheckBox] = {}
        for cap, label in CAPABILITIES.items():
            box = QCheckBox(label)
            box.setChecked(cap in p.capabilities)
            if cap in MEDIA_CAPABILITIES:
                box.setEnabled(False)
                box.setToolTip(MEDIA_NOTE)
            self.caps[cap] = box
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 600)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(int(p.timeout))
        self.retries = QSpinBox()
        self.retries.setRange(0, 10)
        self.retries.setValue(int(p.max_retries))
        self.rate = QSpinBox()
        self.rate.setRange(0, 10_000)
        self.rate.setSpecialValueText("No limit")
        self.rate.setSuffix(" requests/min")
        self.rate.setValue(int(p.rate_limit_per_minute))
        self.cost_in, self.cost_out = _price_box(), _price_box()
        if p.cost_input_per_million is not None:
            self.cost_in.setValue(p.cost_input_per_million)
        if p.cost_output_per_million is not None:
            self.cost_out.setValue(p.cost_output_per_million)
        self.key_env = QLineEdit(p.key_env)
        self.key_env.setPlaceholderText("optional, e.g. MY_PROVIDER_KEY")
        self.key_required = QCheckBox("This provider needs an API key")
        self.key_required.setChecked(p.key_required)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Paste a new key to store it (leave empty to keep the current one)")
        self.remove_key = QCheckBox("Remove the stored key")
        store_ok = secrets.secure_store_available()
        self.key.setEnabled(store_ok)
        self.remove_key.setEnabled(store_ok and bool(key_source))
        status = f"Current key: {key_source}." if key_source else "No key is configured."
        if not store_ok:
            status += (" No secure credential store is available on this system, so keys cannot be saved here. "
                       "Put the key in an environment variable and enter its name above.")

        form = QFormLayout()
        form.addRow("Display name:", self.name)
        form.addRow("Provider type:", self.type)
        form.addRow("API base URL:", self.base_url)
        form.addRow("Model:", self.model)
        form.addRow("", self.enabled)
        caps = QVBoxLayout()
        for box in self.caps.values():
            caps.addWidget(box)
        form.addRow("Capabilities:", caps)
        form.addRow("Request timeout:", self.timeout)
        form.addRow("Retries on temporary errors:", self.retries)
        form.addRow("Local rate limit:", self.rate)
        form.addRow("Your input price:", self.cost_in)
        form.addRow("Your output price:", self.cost_out)
        form.addRow("Key environment variable:", self.key_env)
        form.addRow("", self.key_required)
        form.addRow("API key:", self.key)
        form.addRow("", self.remove_key)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        info = muted(status + " Keys are stored in the Windows Credential Manager, never in files or logs. "
                     "Prices are optional and only used to estimate costs; they are not billing data.")
        layout.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.type.currentIndexChanged.connect(self._on_type)
        self._on_type()

    def _on_type(self) -> None:
        kind = self.type.currentData()
        self.base_url.setPlaceholderText(DEFAULT_BASE_URLS[kind] + "  (default)")
        if kind in ("gemini", "openai"):
            self.key_required.setChecked(True)
            self.key_required.setEnabled(False)
        else:
            self.key_required.setEnabled(True)
        if self.is_new:
            self.model.setPlaceholderText(DEFAULT_MODELS[kind] or "model name used by your server")
            for cap, box in self.caps.items():
                box.setChecked(cap in DEFAULT_CAPABILITIES[kind])

    def result_profile(self) -> ProviderProfile:
        kind = self.type.currentData()
        profile = ProviderProfile(
            display_name=self.name.text().strip(), provider_type=kind,
            model=self.model.text().strip() or (DEFAULT_MODELS[kind] if self.is_new else ""),
            base_url=self.base_url.text().strip(), id=self.profile.id, enabled=self.enabled.isChecked(),
            capabilities=[c for c, box in self.caps.items() if box.isChecked()], timeout=self.timeout.value(),
            max_retries=self.retries.value(), rate_limit_per_minute=self.rate.value(),
            cost_input_per_million=self.cost_in.value() if self.cost_in.value() >= 0 else None,
            cost_output_per_million=self.cost_out.value() if self.cost_out.value() >= 0 else None,
            key_env=self.key_env.text().strip(), key_required=self.key_required.isChecked())
        profile.validate()
        return profile

    def accept(self) -> None:  # noqa: D102
        try:
            self.saved = self.result_profile()
        except StudioError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        super().accept()


class AIModelsPage(QWidget):
    def __init__(self, services: Services, bridge: JobBridge):
        super().__init__()
        self.services = services
        self.store = services.ai_providers
        self._tests: dict[str, str] = {}       # job id -> profile id
        self._results: dict[str, str] = {}     # profile id -> last test text

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.add_button = QPushButton("Add…")
        self.edit_button = QPushButton("Edit…")
        self.delete_button = QPushButton("Delete")
        self.toggle_button = QPushButton("Enable / Disable")
        self.up_button = QPushButton("Move Up")
        self.down_button = QPushButton("Move Down")
        self.primary_button = QPushButton("Set as Primary")
        self.test_connection_button = QPushButton("Test Connection")
        self.test_model_button = QPushButton("Test Model")
        self.defaults_button = QPushButton("Restore Defaults")
        self.routing = QLabel("")
        self.routing.setWordWrap(True)
        self.status = muted("")
        self.totals = QTableWidget(0, 7)
        self.totals.setHorizontalHeaderLabels(["Provider", "Model", "Requests", "Failed", "Tokens (reported)",
                                               "Tokens (estimated)", "Cost"])
        self.history = QTableWidget(0, 8)
        self.history.setHorizontalHeaderLabels(["Time", "Provider", "Model", "Task", "Result", "Duration",
                                                "Tokens", "Cost"])
        for t in (self.totals, self.history):
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            t.verticalHeader().setVisible(False)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.clear_history_button = QPushButton("Clear History")

        buttons = QHBoxLayout()
        for b in (self.add_button, self.edit_button, self.delete_button, self.toggle_button, self.up_button,
                  self.down_button, self.primary_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        tests = QHBoxLayout()
        for b in (self.test_connection_button, self.test_model_button):
            tests.addWidget(b)
        tests.addStretch(1)
        tests.addWidget(self.defaults_button)
        providers = Card()
        providers.body.addWidget(section_title("Providers (in priority order: the first suitable one is the primary)"))
        providers.body.addLayout(buttons)
        providers.body.addWidget(self.table, 1)
        providers.body.addLayout(tests)
        providers.body.addWidget(self.status)
        providers.body.addWidget(self.routing)
        providers.body.addWidget(muted(
            "Only text is sent, and only when you start an AI task (for example AI Check). " + MEDIA_NOTE +
            " A request is retried on temporary errors (rate limit, server, network, timeout) and then tried on "
            "the next provider; a request a provider rejects as invalid is not sent elsewhere."))
        usage = Card()
        usage.body.addWidget(section_title("Usage"))
        usage.body.addWidget(muted(
            "Token counts are marked as reported by the provider or estimated here (about 4 characters per "
            "token). Costs appear only for profiles where you entered prices and are estimates, not billing "
            "data; check your provider's dashboard for actual charges. Prompts and answers are not stored."))
        usage.body.addWidget(self.totals)
        usage.body.addWidget(self.history, 1)
        usage.body.addWidget(self.clear_history_button)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(providers)
        splitter.addWidget(usage)
        layout = QVBoxLayout(self)
        layout.addWidget(page_title("AI Models"))
        layout.addWidget(splitter, 1)

        self.add_button.clicked.connect(self.add_profile)
        self.edit_button.clicked.connect(self.edit_profile)
        self.delete_button.clicked.connect(self.delete_profile)
        self.toggle_button.clicked.connect(self.toggle_enabled)
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button.clicked.connect(lambda: self.move(1))
        self.primary_button.clicked.connect(self.set_primary)
        self.test_connection_button.clicked.connect(lambda: self.test("connection"))
        self.test_model_button.clicked.connect(lambda: self.test("model"))
        self.defaults_button.clicked.connect(self.restore_defaults)
        self.clear_history_button.clicked.connect(self.clear_history)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_profile())
        bridge.job_changed.connect(self._on_job_changed)
        self.refresh()

    # ------------------------------------------------------------------ view
    def profiles(self) -> list[ProviderProfile]:
        return self.store.list()

    def selected(self) -> ProviderProfile | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        profile_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        try:
            return self.store.get(profile_id)
        except StudioError:
            return None

    def key_status(self, profile: ProviderProfile) -> str:
        source = secrets.profile_key_source(profile.id, profile.key_env)
        if source:
            return f"Set ({source})"
        return "Not needed" if not profile.key_required else "Missing"

    def refresh(self, select: str | None = None) -> None:
        current = select or (self.selected().id if self.selected() else None)
        self.table.setRowCount(0)
        for n, p in enumerate(self.profiles()):
            row = self.table.rowCount()
            self.table.insertRow(row)
            caps = ", ".join(SHORT_CAPS[c] for c in CAPABILITIES if c in p.capabilities and c in SHORT_CAPS)
            values = [str(n + 1), p.display_name, p.type_label, p.model, "Yes" if p.enabled else "No", caps,
                      self.key_status(p), self._results.get(p.id, "")]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, p.id)
                if col in (6, 7):
                    cell.setToolTip(value)
                if col == 6 and value == "Missing":
                    cell.setForeground(Qt.GlobalColor.red)
                self.table.setItem(row, col, cell)
            if p.id == current:
                self.table.selectRow(row)
        self._update_routing()
        self.refresh_usage()
        self._update_buttons()

    def _update_routing(self) -> None:
        lines = []
        for capability, label in APP_FEATURES.items():
            profiles, skipped = self.services.ai_plan(capability)
            if profiles:
                route = " → ".join(f"{p.display_name} ({p.model})" for p in profiles)
                lines.append(f"<b>{label}:</b> {route}")
            else:
                lines.append(f"<b>{label}:</b> <span style='color:#ffb84d'>not available</span>"
                             + (f" ({'; '.join(skipped)})" if skipped else ""))
        self.routing.setText("<br>".join(lines))

    def refresh_usage(self) -> None:
        log_ = self.services.ai_usage
        self.totals.setRowCount(0)
        for t in log_.totals():
            row = self.totals.rowCount()
            self.totals.insertRow(row)
            cost = f"≈ {t['cost']:.4f} (estimate)" if t["cost"] is not None else "unknown"
            if t["cost"] is not None and t["unknown_cost"]:
                cost += f", {t['unknown_cost']} unknown"
            for col, value in enumerate([t["provider_name"], t["model"], str(t["requests"]), str(t["failures"] or 0),
                                         str(t["reported_tokens"] or 0), str(t["estimated_tokens"] or 0), cost]):
                self.totals.setItem(row, col, QTableWidgetItem(value))
        self.history.setRowCount(0)
        for r in log_.recent(200):
            row = self.history.rowCount()
            self.history.insertRow(row)
            tokens = "-"
            if r["input_tokens"] is not None:
                tokens = f"{r['input_tokens']}+{r['output_tokens']} ({r['usage_source']})"
            result = "OK" if r["outcome"] == "success" else f"failed: {r['error_kind'] or '?'}"
            cost = f"≈ {r['cost']:.5f}" if r["cost"] is not None else "unknown"
            for col, value in enumerate([r["created_at"].replace("T", " ")[:19], r["provider_name"], r["model"],
                                         r["capability"], result, f"{r['duration_ms'] / 1000:.1f} s", tokens, cost]):
                self.history.setItem(row, col, QTableWidgetItem(value))

    def _update_buttons(self) -> None:
        p = self.selected()
        has = p is not None
        for b in (self.edit_button, self.delete_button, self.toggle_button, self.test_connection_button,
                  self.test_model_button):
            b.setEnabled(has)
        ids = [x.id for x in self.profiles()]
        self.up_button.setEnabled(has and ids.index(p.id) > 0)
        self.down_button.setEnabled(has and ids.index(p.id) < len(ids) - 1)
        self.primary_button.setEnabled(has and ids.index(p.id) > 0)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()

    # ------------------------------------------------------------------ editing
    def _save_from_dialog(self, dialog: ProfileDialog) -> bool:
        profile = dialog.saved
        try:
            self.store.save(profile)
            if dialog.remove_key.isChecked():
                secrets.delete_api_key(profile.id)
            if dialog.key.text().strip():
                secrets.set_api_key(profile.id, dialog.key.text())
        except StudioError as exc:
            QMessageBox.warning(self, "AI Provider", str(exc))
            return False
        finally:
            dialog.key.clear()
        self.refresh(profile.id)
        return True

    def add_profile(self) -> None:
        dialog = ProfileDialog(self, None, None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save_from_dialog(dialog)

    def edit_profile(self) -> None:
        p = self.selected()
        if p is None:
            return
        dialog = ProfileDialog(self, p, secrets.profile_key_source(p.id, p.key_env))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save_from_dialog(dialog)

    def delete_profile(self) -> None:
        p = self.selected()
        if p is None or QMessageBox.question(self, "Delete AI Provider", f"Delete '{p.display_name}' and its stored "
                                             "API key?") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete(p.id)
        secrets.delete_api_key(p.id)
        self.refresh()

    def toggle_enabled(self) -> None:
        p = self.selected()
        if p is not None:
            p.enabled = not p.enabled
            self.store.save(p)
            self.refresh(p.id)

    def move(self, step: int) -> None:
        p = self.selected()
        if p is not None:
            self.store.move(p.id, step)
            self.refresh(p.id)

    def set_primary(self) -> None:
        p = self.selected()
        if p is not None:
            self.store.set_primary(p.id)
            self.refresh(p.id)

    def restore_defaults(self) -> None:
        if QMessageBox.question(self, "Restore Defaults", "Replace all provider profiles with the standard Gemini "
                                "and OpenAI profiles (disabled)? Stored keys of the standard profiles are kept.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.store.restore_defaults()
        self.refresh()

    def clear_history(self) -> None:
        self.services.ai_usage.clear()
        self.refresh_usage()

    # ------------------------------------------------------------------ tests
    def test(self, mode: str) -> Job | None:
        p = self.selected()
        if p is None:
            return None
        if mode == "model" and QMessageBox.question(
                self, "Test Model", f"Send a tiny test prompt to {p.display_name} ({p.model})? This may use a few "
                "tokens of your quota.") != QMessageBox.StandardButton.Yes:
            return None
        job = self.services.jobs.submit(AI_TEST_JOB, f"AI {mode} test: {p.display_name}",
                                        {"profile": p.to_dict(), "mode": mode})
        self._tests[job.id] = p.id
        self._results[p.id] = "Testing…"
        self.refresh(p.id)
        return job

    def _on_job_changed(self, job: Job) -> None:
        if job.id not in self._tests or not job.state.is_finished:
            return
        profile_id = self._tests.pop(job.id)
        if job.state is JobState.COMPLETED and job.result:
            text = "✔ " + job.result["message"]
        elif job.state is JobState.FAILED:
            text = "✘ " + (job.error or "failed")
        else:
            text = "Cancelled"
        self._results[profile_id] = text
        self.status.setText(text)
        self.refresh(profile_id)
