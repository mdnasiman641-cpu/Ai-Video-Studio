"""Dark theme stylesheet and generated application icon."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap

ACCENT = "#4c8dff"

STYLESHEET = f"""
QWidget {{
    background-color: #17191d;
    color: #e3e6eb;
    font-size: 10pt;
}}
QMainWindow::separator {{ background: #23262c; width: 1px; height: 1px; }}
QLabel#PageTitle {{ font-size: 16pt; font-weight: 600; padding: 4px 0 8px 0; }}
QLabel#SectionTitle {{ font-size: 11pt; font-weight: 600; color: #c9cdd4; }}
QLabel#Muted {{ color: #8b919c; }}
QLabel#Error {{ color: #ff6b6b; }}
QLabel#Ok {{ color: #5fd38d; }}
QLabel#Warning {{ color: #ffb84d; }}
QFrame#Card {{
    background-color: #1e2126;
    border: 1px solid #2a2e35;
    border-radius: 8px;
}}
QFrame#Card QLabel, QFrame#Card QCheckBox {{ background: transparent; }}
QListWidget#Sidebar {{
    background-color: #121418;
    border: none;
    border-right: 1px solid #23262c;
    padding: 8px 4px;
    outline: 0;
}}
QListWidget#Sidebar::item {{
    padding: 8px 10px;
    border-radius: 6px;
    margin: 1px 4px;
}}
QListWidget#Sidebar::item:selected {{ background-color: #24324d; color: #ffffff; }}
QListWidget#Sidebar::item:hover:!selected {{ background-color: #1d2026; }}
QPushButton {{
    background-color: #2a2e36;
    border: 1px solid #353a43;
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background-color: #323743; }}
QPushButton:pressed {{ background-color: #262a31; }}
QPushButton:disabled {{ color: #5d636e; background-color: #1f2227; border-color: #2a2d33; }}
QPushButton#Primary {{ background-color: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#Primary:hover {{ background-color: #5e9bff; }}
QPushButton#Primary:disabled {{ background-color: #2b3a55; border-color: #2b3a55; color: #8796b3; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background-color: #111316;
    border: 1px solid #2f333b;
    border-radius: 5px;
    padding: 4px 6px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QTableWidget, QTableView, QListWidget, QTreeView {{
    background-color: #131518;
    alternate-background-color: #171a1e;
    border: 1px solid #2a2e35;
    border-radius: 6px;
    gridline-color: #23262c;
    selection-background-color: #2b4777;
    selection-color: #ffffff;
}}
QHeaderView::section {{
    background-color: #1c1f24;
    color: #aab0ba;
    border: none;
    border-bottom: 1px solid #2a2e35;
    padding: 5px 6px;
}}
QProgressBar {{
    background-color: #111316;
    border: 1px solid #2a2e35;
    border-radius: 4px;
    text-align: center;
    height: 14px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: #2f333b; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: #e3e6eb; width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
}}
QStatusBar {{ background-color: #121418; border-top: 1px solid #23262c; }}
QMenuBar {{ background-color: #121418; }}
QMenuBar::item:selected, QMenu::item:selected {{ background-color: #2b4777; }}
QMenu {{ background-color: #1e2126; border: 1px solid #2a2e35; }}
QToolTip {{ background-color: #1e2126; color: #e3e6eb; border: 1px solid #353a43; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #353a43; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""


def app_icon() -> QIcon:
    """Simple generated icon so no binary asset is required."""
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        painter.fillPath(path, QColor(ACCENT))
        # play triangle
        tri = QPainterPath()
        tri.moveTo(size * 0.36, size * 0.26)
        tri.lineTo(size * 0.76, size * 0.5)
        tri.lineTo(size * 0.36, size * 0.74)
        tri.closeSubpath()
        painter.fillPath(tri, QColor("white"))
        painter.end()
        icon.addPixmap(pix)
    return icon


def title_font() -> QFont:
    font = QFont()
    font.setPointSize(16)
    font.setBold(True)
    return font


def glyph_icon(glyph: str, color: str = "#c9cdd4") -> QIcon:
    """Monochrome icon drawn from a Unicode symbol (readable on the dark theme, no image assets)."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48):
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = QFont()
        font.setFamilies(["Segoe UI Symbol", "Segoe UI", "DejaVu Sans", "Noto Sans Symbols2"])
        font.setPixelSize(int(size * 0.8))
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        painter.end()
        icon.addPixmap(pix)
    return icon
