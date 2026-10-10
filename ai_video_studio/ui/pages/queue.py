"""Export queue page: background job list with pause/resume, cancel, retry, clear."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.core.errors import StudioError
from ai_video_studio.jobs.models import Job, JobState
from ai_video_studio.services import Services
from ai_video_studio.ui.project_controller import JobBridge
from ai_video_studio.ui.widgets.common import muted, page_title

HISTORY_LIMIT = 500  # rows shown; older finished jobs stay in the database until cleared
COLUMNS = ["Job", "State", "Progress", "Attempts", "Created", "Finished", "Message"]
KIND_LABELS = {
    "probe_media": "Metadata",
    "transcribe": "Captions",
    "burn_in": "Export",
    "caption_frame_preview": "Preview",
    "caption_correction": "AI check",
}


def _short_time(iso: str | None) -> str:
    if not iso:
        return "-"
    return iso.replace("T", " ")[:19]


class QueuePage(QWidget):
    def __init__(self, services: Services, bridge: JobBridge):
        super().__init__()
        self.services = services
        self._rows: dict[str, int] = {}

        self.pause_button = QPushButton("Pause Queue")
        self.cancel_button = QPushButton("Cancel Selected")
        self.retry_button = QPushButton("Retry Selected")
        self.clear_button = QPushButton("Clear Finished")
        self.summary = muted("", wrap=False)

        toolbar = QHBoxLayout()
        for b in (self.pause_button, self.cancel_button, self.retry_button, self.clear_button):
            toolbar.addWidget(b)
        toolbar.addStretch(1)
        toolbar.addWidget(self.summary)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        layout = QVBoxLayout(self)
        layout.addWidget(page_title("Export Queue"))
        layout.addWidget(muted(
            "Background jobs run here. Queued jobs are kept across restarts; jobs interrupted by "
            "closing the app are marked failed and can be retried. Rendering jobs are added in later phases."
        ))
        layout.addLayout(toolbar)
        layout.addWidget(self.table, 1)

        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.retry_button.clicked.connect(self.retry_selected)
        self.clear_button.clicked.connect(self.clear_finished)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        bridge.job_changed.connect(self.on_job_changed)
        self.reload()

    # -- actions -------------------------------------------------------------
    def toggle_pause(self) -> None:
        if self.services.jobs.paused:
            self.services.jobs.resume()
        else:
            self.services.jobs.pause()
        self._update_buttons()

    def cancel_selected(self) -> None:
        self._apply(self.services.jobs.cancel, {JobState.QUEUED, JobState.RUNNING})

    def retry_selected(self) -> None:
        self._apply(self.services.jobs.retry, {JobState.FAILED, JobState.CANCELLED})

    def clear_finished(self) -> None:
        self.services.jobs.store.delete_finished()
        self.reload()

    def _apply(self, action, allowed: set[JobState]) -> None:
        errors = []
        for job in self.selected_jobs():
            if job.state in allowed:
                try:
                    action(job.id)
                except StudioError as exc:
                    errors.append(f"{job.title}: {exc}")
        if errors:
            QMessageBox.warning(self, "Queue", "\n".join(errors))
        self._update_buttons()

    # -- view ----------------------------------------------------------------
    def selected_jobs(self) -> list[Job]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        jobs = []
        for r in rows:
            job_id = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            try:
                jobs.append(self.services.jobs.store.get(job_id))
            except KeyError:
                pass
        return jobs

    def reload(self) -> None:
        jobs = self.services.jobs.store.list(limit=HISTORY_LIMIT, newest_first=True)
        self.table.setRowCount(0)
        self._rows.clear()
        for job in jobs:
            self._insert(job)
        self._update_buttons()

    def on_job_changed(self, job: Job) -> None:
        row = self._rows.get(job.id)
        if row is None:
            self._insert(job, top=True)
        else:
            self._fill(row, job)
        self._update_buttons()

    def _insert(self, job: Job, top: bool = False) -> None:
        row = 0 if top else self.table.rowCount()
        self.table.insertRow(row)
        if top:
            self._rows = {k: v + 1 for k, v in self._rows.items()}
        self._rows[job.id] = row
        bar = QProgressBar()
        bar.setRange(0, 100)
        self.table.setCellWidget(row, 2, bar)
        self._fill(row, job)

    def _fill(self, row: int, job: Job) -> None:
        kind = KIND_LABELS.get(job.kind, job.kind)
        values = [
            f"[{kind}] {job.title}",
            job.state.value.capitalize(),
            None,
            str(job.attempts),
            _short_time(job.created_at),
            _short_time(job.finished_at),
            job.error or "",
        ]
        for col, value in enumerate(values):
            if value is None:
                continue
            cell = QTableWidgetItem(value)
            if col == 0:
                cell.setData(Qt.ItemDataRole.UserRole, job.id)
            if col == 1 and job.state is JobState.FAILED:
                cell.setForeground(Qt.GlobalColor.red)
            if col == 6 and value:
                cell.setToolTip(value)
            self.table.setItem(row, col, cell)
        bar = self.table.cellWidget(row, 2)
        if isinstance(bar, QProgressBar):
            bar.setValue(int(job.progress * 100))

    def _update_buttons(self) -> None:
        jobs = self.selected_jobs()
        states = {j.state for j in jobs}
        self.cancel_button.setEnabled(bool(states & {JobState.QUEUED, JobState.RUNNING}))
        self.retry_button.setEnabled(bool(states & {JobState.FAILED, JobState.CANCELLED}))
        self.pause_button.setText("Resume Queue" if self.services.jobs.paused else "Pause Queue")
        counts = self.services.jobs.store.counts()
        self.clear_button.setEnabled(any(counts[s] for s in JobState if s.is_finished))
        paused = " (paused)" if self.services.jobs.paused else ""
        self.summary.setText(
            f"{counts[JobState.RUNNING]} running · {counts[JobState.QUEUED]} queued{paused} · "
            f"{counts[JobState.COMPLETED]} completed · {counts[JobState.FAILED]} failed · "
            f"{counts[JobState.CANCELLED]} cancelled"
        )
