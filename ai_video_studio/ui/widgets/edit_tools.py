"""Tool panels of the Edit page (Smart Cut, Captions, Resize / Crop, Auto Reframe, Speed, Audio,
Final Export).

Every panel edits the video's own saved settings, the same ones the dedicated pages use, so there
is one copy of each setting: Smart Cut state, the caption track and style, Resize & Crop settings
(their ``use_cuts``/``burn_captions``/``enabled`` switches say what the final export includes),
Auto Reframe settings and the speed/audio effects. The panels only call existing controller
methods; the algorithms live where they always did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_video_studio.captions.model import CaptionError, CaptionTrack, format_clock, parse_clock
from ai_video_studio.core.errors import StudioError
from ai_video_studio.core.media import MediaItem, format_duration
from ai_video_studio.jobs.reframe_jobs import REFRAME_ANALYZE_JOB
from ai_video_studio.jobs.render_jobs import SMARTCUT_ANALYZE_JOB
from ai_video_studio.reframe.model import TRACKING_MODES
from ai_video_studio.render.layout import MODES, PRESETS
from ai_video_studio.render.pipeline import kept_duration
from ai_video_studio.smartcut.model import SmartCutSettings
from ai_video_studio.ui.widgets.caption_style_editor import CaptionStyleEditor
from ai_video_studio.ui.widgets.caption_templates import CaptionTemplateGrid
from ai_video_studio.ui.widgets.common import muted, section_title

if TYPE_CHECKING:
    from ai_video_studio.ui.pages.edit import EditPage

RESIZE_MODES = [m for m in MODES if m != "reframe"]


class ToolPanel(QWidget):
    title = ""

    def __init__(self, page: "EditPage"):
        super().__init__()
        self.page = page
        self.controller = page.controller
        self._filling = False
        self.status = muted("")
        self.body = QVBoxLayout(self)
        self.body.addWidget(section_title(self.title))

    def media_id(self) -> str | None:
        item = self.page.current()
        return item.id if item is not None else None

    def guarded(self, action, *args) -> bool:
        """Run a settings change; show expected errors in the panel instead of raising."""
        if self._filling or self.media_id() is None:
            return False
        try:
            action(*args)  # the change reloads the panels, which show the new state
        except (StudioError, ValueError) as exc:
            self.status.setText(f"⚠ {exc}")
            return False
        return True

    def load(self, item: MediaItem | None) -> None:
        self._filling = True
        try:
            self.setEnabled(item is not None and item.metadata is not None)
            if item is not None and item.metadata is not None:
                self.fill(item)
        finally:
            self._filling = False

    def fill(self, item: MediaItem) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


# -- Smart Cut ---------------------------------------------------------------------------------

class SmartCutPanel(ToolPanel):
    title = "Smart Cut — silence removal"

    def __init__(self, page):
        super().__init__(page)
        self.include = QCheckBox("Include the cuts in the final export")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-90, -5)
        self.threshold.setDecimals(0)
        self.threshold.setSuffix(" dB")
        self.threshold.setToolTip("Sound below this level counts as silence.")
        self.min_silence = QDoubleSpinBox()
        self.min_silence.setRange(0.1, 30)
        self.min_silence.setSingleStep(0.1)
        self.min_silence.setSuffix(" s")
        self.min_silence.setToolTip("Shorter pauses are kept.")
        self.analyse_button = QPushButton("Analyse Audio")
        self.apply_button = QPushButton("Apply Detected Cuts")
        self.remove_button = QPushButton("Remove Cuts")
        self.cuts = QListWidget()
        self.cuts.setMaximumHeight(160)
        self.cuts.setToolTip("Parts proposed for removal. Click one to jump there in the preview.")
        form = QFormLayout()
        form.addRow("Silence below:", self.threshold)
        form.addRow("Minimum silence:", self.min_silence)
        buttons = QHBoxLayout()
        for b in (self.analyse_button, self.apply_button, self.remove_button):
            buttons.addWidget(b)
        self.body.addWidget(self.include)
        self.body.addLayout(form)
        self.body.addLayout(buttons)
        self.body.addWidget(self.status)
        self.body.addWidget(QLabel("Proposed cuts:"))
        self.body.addWidget(self.cuts)
        self.body.addWidget(muted("Changing the settings detects the silence again (manual changes made on the "
                                  "Smart Cut page are replaced). Fine-tune single cuts on the Smart Cut page."))
        self.body.addStretch(1)
        self.include.toggled.connect(lambda on: self.guarded(lambda: self.controller.update_resize(
            self.media_id(), use_cuts=on)))
        self.threshold.valueChanged.connect(self._on_settings)
        self.min_silence.valueChanged.connect(self._on_settings)
        self.analyse_button.clicked.connect(self.analyse)
        self.apply_button.clicked.connect(self.apply_cuts)
        self.remove_button.clicked.connect(self.remove_cuts)
        self.cuts.itemClicked.connect(lambda it: self.page.preview.seek(int(it.data(Qt.ItemDataRole.UserRole) * 1000)))

    def fill(self, item: MediaItem) -> None:
        state = self.controller.smartcut_state(item.id)
        ops = self.controller.final_operations(item.id)
        self.threshold.setValue(state.settings.threshold_db)
        self.min_silence.setValue(state.settings.min_silence)
        self.include.setEnabled(ops["cuts_available"])
        self.include.setChecked(ops["use_cuts"])
        has_audio = item.metadata.has_audio
        analysing = bool(self.controller.active_jobs(item.id, SMARTCUT_ANALYZE_JOB))
        self.analyse_button.setEnabled(has_audio and not analysing)
        self.apply_button.setEnabled(bool(state.segments))
        self.remove_button.setEnabled(ops["cuts_available"])
        self.cuts.clear()
        removed = [s for s in state.segments if s.remove]
        for seg in removed:
            entry = QListWidgetItem(f"{format_clock(seg.start)} – {format_clock(seg.end)}  ({seg.duration:.2f} s)")
            entry.setData(Qt.ItemDataRole.UserRole, seg.start)
            self.cuts.addItem(entry)
        if not has_audio:
            text = "This video has no audio track, so there is no silence to detect."
        elif analysing:
            text = "Analysing the audio…"
        elif state.levels is None:
            text = "Analyse the audio to find silence."
        else:
            text = (f"{len(removed)} part(s) proposed for removal "
                    f"({sum(s.duration for s in removed):.1f} s).")
            cuts = self.controller.applied_cuts(item.id)
            text += (f" Applied: {len(cuts)} kept part(s), {format_duration(kept_duration(cuts))}." if cuts
                     else " No cuts applied yet.")
            if state.has_pending_changes:
                text += " The proposal differs from the applied cuts: apply it to use it."
        self.info = text
        self.status.setText(text)

    def _on_settings(self, *_args) -> None:
        def change() -> None:
            state = self.controller.smartcut_state(self.media_id())
            settings = SmartCutSettings.from_dict(state.settings.to_dict())
            settings.threshold_db, settings.min_silence = self.threshold.value(), self.min_silence.value()
            settings.validate()
            state.settings = settings
            if state.levels is not None:
                state.detect()
            self.controller.set_smartcut_state(self.media_id(), state)
        self.guarded(change)

    def analyse(self) -> None:
        self.guarded(lambda: self.controller.submit_smartcut_analysis(self.media_id()))
        self.page.reload()

    def apply_cuts(self) -> None:
        def apply() -> None:
            state = self.controller.smartcut_state(self.media_id())
            state.apply()
            self.controller.set_smartcut_state(self.media_id(), state)
            self.controller.update_resize(self.media_id(), use_cuts=True)
        self.guarded(apply)

    def remove_cuts(self) -> None:
        def remove() -> None:
            state = self.controller.smartcut_state(self.media_id())
            state.applied = None
            self.controller.set_smartcut_state(self.media_id(), state)
        self.guarded(remove)


# -- Captions ----------------------------------------------------------------------------------

class CaptionsPanel(ToolPanel):
    title = "Captions"

    def __init__(self, page):
        super().__init__(page)
        self.burn = QCheckBox("Burn captions into the final export")
        self.generate_button = QPushButton("Generate…")
        self.import_button = QPushButton("Import SRT…")
        self.export_button = QPushButton("Export SRT…")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Start", "End", "Text"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setMinimumHeight(160)
        self.add_button = QPushButton("Add at Playhead")
        self.delete_button = QPushButton("Delete Selected")
        self.templates = CaptionTemplateGrid(columns=2)
        self._template: str | None = None
        self.apply_selected_button = QPushButton("Style → Selected")
        self.apply_all_button = QPushButton("Style → All Captions")
        self.style_editor = CaptionStyleEditor()
        row = QHBoxLayout()
        for b in (self.generate_button, self.import_button, self.export_button):
            row.addWidget(b)
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.add_button)
        edit_row.addWidget(self.delete_button)
        apply_row = QHBoxLayout()
        apply_row.addWidget(self.apply_selected_button)
        apply_row.addWidget(self.apply_all_button)
        style_box = QGroupBox("Video caption style")
        QVBoxLayout(style_box).addWidget(self.style_editor)
        for w in (self.burn, row, self.status, self.table, edit_row, QLabel("Templates:"), self.templates, apply_row,
                  style_box):
            (self.body.addLayout if isinstance(w, QHBoxLayout) else self.body.addWidget)(w)
        self.body.addWidget(muted("Double-click a cell to edit the text or the time (mm:ss.mmm). Split, merge, undo "
                                  "and AI checks are on the Captions page."))
        self.burn.toggled.connect(lambda on: self.guarded(lambda: self.controller.update_resize(
            self.media_id(), burn_captions=on, burn_set=True)))
        self.generate_button.clicked.connect(lambda: self.page.captions_action("generate"))
        self.import_button.clicked.connect(lambda: self.page.captions_action("import_srt"))
        self.export_button.clicked.connect(lambda: self.page.captions_action("export_srt"))
        self.table.itemChanged.connect(self._on_cell)
        self.add_button.clicked.connect(self.add_caption)
        self.delete_button.clicked.connect(self.delete_selected)
        self.templates.chosen.connect(self._on_template)
        self.apply_selected_button.clicked.connect(self.apply_to_selected)
        self.apply_all_button.clicked.connect(self.apply_to_all)
        self.style_editor.style_changed.connect(
            lambda style: self.guarded(lambda: self.controller.set_caption_style([self.media_id()], style)))
        self.reload_templates()

    def reload_templates(self) -> None:
        presets = self.controller.services.presets
        entries = []
        for name, builtin in presets.names():
            try:
                entries.append((name, presets.get(name), builtin))
            except StudioError:
                continue  # damaged saved preset: the Captions page explains it
        self.templates.set_templates(entries, self._template)

    def selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows if self.table.item(r, 0)]

    def fill(self, item: MediaItem) -> None:
        ops = self.controller.final_operations(item.id)
        self.burn.setChecked(ops["burn_captions"] or ops["captions_missing"])  # can be switched on before captions exist
        selected = set(self.selected_ids())
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        segments = item.captions.segments if item.captions else []
        for seg in segments:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for col, value in enumerate((format_clock(seg.start), format_clock(seg.end), seg.text)):
                cell = QTableWidgetItem(value)
                if col == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, seg.id)
                if col == 2 and seg.style:
                    cell.setToolTip("This caption has its own style.")
                self.table.setItem(r, col, cell)
            if seg.id in selected:
                self.table.selectRow(r)
        self.table.blockSignals(False)
        self.export_button.setEnabled(bool(segments))
        if segments:
            self.status.setText(f"{len(segments)} caption(s)." + ("" if ops["burn_captions"] else
                                                                   " Not burned in (switch it on above)."))
        else:
            self.status.setText(("⚠ Burn-in is on, but there are no captions yet: " if ops["captions_missing"] else
                                 "No captions yet: ") + "generate them or import an SRT file.")
        self.style_editor.set_style(item.effective_caption_style)

    def _edit_track(self, operation) -> None:
        def run() -> None:
            item = self.page.current()
            track = item.captions.copy() if item.captions else CaptionTrack()
            operation(track, item)
            self.controller.set_captions(item.id, track)
        if not self.guarded(run):
            message = self.status.text()
            self.page.reload()  # show the unchanged captions again, keep the explanation
            self.status.setText(message)

    def _on_cell(self, cell: QTableWidgetItem) -> None:
        seg_id = self.table.item(cell.row(), 0).data(Qt.ItemDataRole.UserRole)

        def change(track: CaptionTrack, item: MediaItem) -> None:
            seg = track.find(seg_id)
            try:
                if cell.column() == 2:
                    track.set_text(seg_id, cell.text())
                    return
                start = parse_clock(cell.text()) if cell.column() == 0 else seg.start
                end = parse_clock(cell.text()) if cell.column() == 1 else seg.end
            except (CaptionError, ValueError) as exc:
                raise StudioError(f"Invalid time: {exc}") from exc
            track.set_times(seg_id, start, end, item.metadata.duration)
        self._edit_track(change)

    def add_caption(self) -> None:
        at = self.page.preview.position_seconds()
        self._edit_track(lambda track, item: track.add(at, text="New caption", video_duration=item.metadata.duration))

    def delete_selected(self) -> None:
        ids = set(self.selected_ids())
        if ids:
            self._edit_track(lambda track, _item: track.delete(ids))

    def _on_template(self, name: str) -> None:
        self._template = name
        self.status.setText(f"Template “{name}” chosen: apply it to the selected captions or to all captions.")

    def _template_style(self):
        if not self._template:
            self.status.setText("Choose a template first.")
            return None
        try:
            return self.controller.services.presets.get(self._template)
        except StudioError as exc:
            self.status.setText(f"⚠ {exc}")
            return None

    def apply_to_selected(self) -> None:
        style, ids = self._template_style(), set(self.selected_ids())
        if style is None:
            return
        if not ids:
            self.status.setText("Select captions in the list first.")
            return

        def change(track: CaptionTrack, _item) -> None:
            for seg in track.segments:
                if seg.id in ids:
                    seg.style = style.to_dict()
        self._edit_track(change)

    def apply_to_all(self) -> None:
        style = self._template_style()
        item = self.page.current()
        if style is None or item is None:
            return
        if item.captions and any(s.style for s in item.captions.segments):
            def clear(track: CaptionTrack, _item) -> None:
                for seg in track.segments:
                    seg.style = None
            self._edit_track(clear)
        self.guarded(lambda: self.controller.set_caption_style([item.id], style))


# -- Resize / Crop ------------------------------------------------------------------------------

class ResizePanel(ToolPanel):
    title = "Resize / Crop"

    def __init__(self, page):
        super().__init__(page)
        self.enabled = QCheckBox("Resize / crop in the final export")
        self.size = QComboBox()
        for key, (label, _s) in PRESETS.items():
            self.size.addItem(label, key)
        self.width_spin, self.height_spin = QSpinBox(), QSpinBox()
        for box, limit in ((self.width_spin, 7680), (self.height_spin, 4320)):
            box.setRange(64, limit)
            box.setSingleStep(2)
            box.setKeyboardTracking(False)
        self.mode = QComboBox()
        for key in RESIZE_MODES:
            self.mode.addItem(MODES[key].split(" —")[0], key)
        self.pos_x, self.pos_y = QSlider(Qt.Orientation.Horizontal), QSlider(Qt.Orientation.Horizontal)
        for s in (self.pos_x, self.pos_y):
            s.setRange(0, 100)
        self.zoom = QSpinBox()
        self.zoom.setRange(10, 400)
        self.zoom.setSuffix(" %")
        self.crop = [QDoubleSpinBox() for _ in range(4)]
        for box in self.crop:
            box.setRange(0, 100)
            box.setDecimals(1)
            box.setSuffix(" %")
            box.setKeyboardTracking(False)
        self.blur = QSpinBox()
        self.blur.setRange(1, 60)
        self.reframe_note = muted("Auto Reframe is on (Auto Reframe tool): faces decide the crop. The output size "
                                  "below still applies.")
        self.preview_button = QPushButton("Preview Frame")
        self.preview_button.setToolTip("Render the frame at the playhead exactly as the final export will look.")
        form = QFormLayout()
        form.addRow("Output size:", self.size)
        dims = QHBoxLayout()
        dims.addWidget(self.width_spin)
        dims.addWidget(QLabel("×"))
        dims.addWidget(self.height_spin)
        form.addRow("Custom size:", dims)
        form.addRow("Mode:", self.mode)
        form.addRow("Horizontal position:", self.pos_x)
        form.addRow("Vertical position:", self.pos_y)
        form.addRow("Size (custom):", self.zoom)
        for label, box in zip(("Crop left:", "Crop top:", "Crop width:", "Crop height:"), self.crop):
            form.addRow(label, box)
        form.addRow("Blur strength:", self.blur)
        self.body.addWidget(self.enabled)
        self.body.addWidget(self.reframe_note)
        self.body.addLayout(form)
        self.body.addWidget(self.preview_button)
        self.body.addWidget(self.status)
        self.body.addStretch(1)
        self.enabled.toggled.connect(lambda on: self.guarded(lambda: self.controller.update_resize(
            self.media_id(), enabled=on)))
        for box in (self.size, self.mode):
            box.currentIndexChanged.connect(self._on_change)
        for w in [self.width_spin, self.height_spin, self.zoom, self.blur, self.pos_x, self.pos_y] + self.crop:
            w.valueChanged.connect(self._on_change)
        self.preview_button.clicked.connect(self.page.preview_frame)

    def fill(self, item: MediaItem) -> None:
        ops = self.controller.final_operations(item.id)
        s = self.controller.resize_settings(item.id)
        self.enabled.setChecked(ops["use_resize"])
        self.size.setCurrentIndex(max(0, self.size.findData(s.preset)))
        self.width_spin.setValue(int(s.width))
        self.height_spin.setValue(int(s.height))
        reframe = s.mode == "reframe"
        if not reframe:
            self.mode.setCurrentIndex(max(0, self.mode.findData(s.mode)))
        self.pos_x.setValue(round(s.pos_x * 100))
        self.pos_y.setValue(round(s.pos_y * 100))
        self.zoom.setValue(round(s.zoom * 100))
        for box, value in zip(self.crop, (s.crop_x, s.crop_y, s.crop_w, s.crop_h)):
            box.setValue(value * 100)
        self.blur.setValue(int(s.blur))
        self.reframe_note.setVisible(reframe)
        mode = s.mode
        custom = s.preset == "custom"
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        self.mode.setEnabled(not reframe)
        self.pos_x.setEnabled(mode in ("fill", "custom"))
        self.pos_y.setEnabled(mode in ("fill", "custom"))
        self.zoom.setEnabled(mode == "custom")
        for box in self.crop:
            box.setEnabled(mode == "crop")
        self.blur.setEnabled(mode == "blur")
        try:
            layout = self.controller.resize_layout(item.id, s)
            self.status.setText(f"Output {layout.out_w}×{layout.out_h}." if ops["use_resize"] else
                            f"⚠ Switched off: the final export keeps the original frame. Tick “Resize / crop in "
                            f"the final export” to export {layout.out_w}×{layout.out_h}.")
        except StudioError as exc:
            self.status.setText(f"⚠ {exc}")

    def _on_change(self, *_args) -> None:
        def change() -> None:
            s = self.controller.resize_settings(self.media_id())
            s.preset = self.size.currentData()
            s.width, s.height = self.width_spin.value(), self.height_spin.value()
            if s.mode != "reframe":
                s.mode = self.mode.currentData()
            s.pos_x, s.pos_y = self.pos_x.value() / 100, self.pos_y.value() / 100
            s.zoom = self.zoom.value() / 100
            s.crop_x, s.crop_y, s.crop_w, s.crop_h = (b.value() / 100 for b in self.crop)
            s.blur = self.blur.value()
            s.enabled = True  # changing the framing switches it on (it may have been off, e.g. never resized)
            self.controller.set_resize_settings(self.media_id(), s)
        self.guarded(change)


# -- Auto Reframe --------------------------------------------------------------------------------

class ReframePanel(ToolPanel):
    title = "Auto Reframe — follow faces"

    def __init__(self, page):
        super().__init__(page)
        self.use = QCheckBox("Use Auto Reframe for the final export's framing")
        self.tracking = QComboBox()
        for key, info in TRACKING_MODES.items():
            self.tracking.addItem(info.label, key)
        self.analyse_button = QPushButton("Analyse Faces")
        self.preview_button = QPushButton("Preview Frame")
        form = QFormLayout()
        form.addRow("Tracking mode:", self.tracking)
        row = QHBoxLayout()
        row.addWidget(self.analyse_button)
        row.addWidget(self.preview_button)
        self.body.addWidget(self.use)
        self.body.addLayout(form)
        self.body.addLayout(row)
        self.body.addWidget(self.status)
        self.body.addWidget(muted("The output size is set in Resize / Crop and must be 16:9, 9:16, 1:1 or 4:5. "
                                  "Subject choice and advanced tracking options are on the Auto Reframe page."))
        self.body.addStretch(1)
        self.use.toggled.connect(self._on_use)
        self.tracking.currentIndexChanged.connect(self._on_tracking)
        self.analyse_button.clicked.connect(self.analyse)
        self.preview_button.clicked.connect(self.page.preview_frame)

    def fill(self, item: MediaItem) -> None:
        ops = self.controller.final_operations(item.id)
        state = self.controller.reframe_state(item.id)
        self.use.setChecked(ops["reframe"])
        self.tracking.setCurrentIndex(max(0, self.tracking.findData(state.settings.tracking_mode)))
        analysing = bool(self.controller.active_jobs(item.id, REFRAME_ANALYZE_JOB))
        self.analyse_button.setEnabled(not analysing)
        if analysing:
            text = "Analysing faces…"
        elif not ops["reframe"]:
            text = "Off: the framing comes from Resize / Crop." + (
                "" if state.analysis is None else " (A face analysis is available.)")
        else:
            try:
                layout = self.controller.resize_layout(item.id)
                path = self.controller.reframe_path_for(item.id, layout.out_w, layout.out_h, ops["burn_captions"])
                text = f"Ready: {layout.out_w}×{layout.out_h}, {TRACKING_MODES[state.settings.tracking_mode].label}."
                if path.warnings:
                    text += " " + " ".join(path.warnings)
            except StudioError as exc:
                text = f"⚠ {exc}"
        self.status.setText(text)

    def _on_use(self, on: bool) -> None:
        def change() -> None:
            s = self.controller.resize_settings(self.media_id())
            if on:
                before = s.mode if s.mode != "reframe" else s.mode_before_reframe
                self.controller.update_resize(self.media_id(), mode="reframe", enabled=True,
                                              mode_before_reframe=before)
            elif s.mode == "reframe":  # back to the user's own fixed framing (saved with the project)
                back = s.mode_before_reframe if s.mode_before_reframe in MODES and \
                    s.mode_before_reframe != "reframe" else "fill"
                self.controller.update_resize(self.media_id(), mode=back)
        self.guarded(change)

    def _on_tracking(self, *_args) -> None:
        def change() -> None:
            settings = self.controller.reframe_state(self.media_id()).settings
            settings.tracking_mode = self.tracking.currentData()
            self.controller.set_reframe_settings(self.media_id(), settings)
        self.guarded(change)

    def analyse(self) -> None:
        self.guarded(lambda: self.controller.submit_reframe_analysis(self.media_id()))
        self.page.reload()


# -- Speed and Audio (one EffectsForm, its two groups shown in two panels) -------------------------

class GroupPanel(ToolPanel):
    def __init__(self, page, title: str, group: QWidget, note: str):
        self.title = title
        super().__init__(page)
        self.body.addWidget(group)
        self.body.addWidget(self.status)
        self.body.addWidget(muted(note))
        self.body.addStretch(1)

    def fill(self, item: MediaItem) -> None:
        summary = self.controller.effects(item.id).summary()
        warning = "" if item.metadata.has_audio or not self.title.startswith("Audio") else \
            "⚠ This video has no audio track: audio settings have no effect. "
        self.status.setText(warning + ("Now: " + ", ".join(summary) if summary else "Now: unchanged."))
