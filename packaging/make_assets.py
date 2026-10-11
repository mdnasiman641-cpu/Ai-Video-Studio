"""Generate build assets: the application icon (.ico with 16-256 px images, plus a PNG) and the
Windows version resource for the executables. Uses the same icon the running app draws
(``ui.theme.app_icon``), so the EXE, the window and the installer look the same."""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def ico_bytes(pngs: list[tuple[int, bytes]]) -> bytes:
    """An ICO file with PNG-compressed images (supported by Windows Vista and later)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in pngs:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


def render_icon(out_dir: Path) -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
    from PySide6.QtWidgets import QApplication

    from ai_video_studio.ui.theme import app_icon

    app = QApplication.instance() or QApplication([])
    icon = app_icon()
    pngs = []
    for size in ICON_SIZES:
        image = icon.pixmap(QSize(size, size)).toImage()
        buffer = QByteArray()
        device = QBuffer(buffer)
        device.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(device, "PNG")
        pngs.append((size, bytes(buffer.data())))
    out_dir.mkdir(parents=True, exist_ok=True)
    ico = out_dir / "app.ico"
    ico.write_bytes(ico_bytes(pngs))
    (out_dir / "app.png").write_bytes(pngs[-1][1])
    del app
    return ico


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int("".join(c for c in p if c.isdigit()) or 0) for p in version.split(".")[:4]]
    return tuple((parts + [0, 0, 0, 0])[:4])  # type: ignore[return-value]


def version_resource(version: str, name: str, internal: str, exe: str) -> str:
    """PyInstaller version file (Windows VERSIONINFO resource)."""
    v = version_tuple(version)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'AI Video Studio project'),
      StringStruct('FileDescription', {name!r}),
      StringStruct('FileVersion', {version!r}),
      StringStruct('InternalName', {internal!r}),
      StringStruct('LegalCopyright', 'AI Video Studio contributors'),
      StringStruct('OriginalFilename', {exe!r}),
      StringStruct('ProductName', 'AI Video Studio'),
      StringStruct('ProductVersion', {version!r})])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main(out_dir: str) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ai_video_studio import __version__

    out = Path(out_dir)
    render_icon(out)
    (out / "version_gui.txt").write_text(
        version_resource(__version__, "AI Video Studio", "AI-Video-Studio", "AI-Video-Studio.exe"), encoding="utf-8")
    (out / "version_console.txt").write_text(
        version_resource(__version__, "AI Video Studio (diagnostic console)", "AI-Video-Studio-Console",
                         "AI-Video-Studio-Console.exe"), encoding="utf-8")
    print(f"Assets written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "build/assets"))
