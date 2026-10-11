"""Pages for features scheduled in later phases.

These pages intentionally contain no buttons: they state what is planned so the
navigation is complete without presenting unimplemented features as working.
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ai_video_studio.ui.widgets.common import Card, muted, page_title, section_title

PLANNED_PAGES: dict[str, tuple[int, list[str]]] = {
    "Bulk Processing": (6, [
        "Per-video and shared settings, presets",
        "Batch export with filename templates and duplicate protection",
    ]),
    "AI Models": (7, [
        "Gemini and OpenAI provider configuration (keys stored outside the repo)",
        "Primary/fallback providers, retries, rate-limit handling, usage display",
    ]),
}


class PlannedPage(QWidget):
    def __init__(self, title: str):
        super().__init__()
        phase, items = PLANNED_PAGES[title]
        card = Card()
        card.body.addWidget(section_title(f"Planned for Phase {phase} — not implemented yet"))
        for item in items:
            card.body.addWidget(muted(f"• {item}"))
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(title))
        layout.addWidget(card)
        layout.addStretch(1)
