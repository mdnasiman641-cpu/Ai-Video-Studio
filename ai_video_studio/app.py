"""Application entry point."""

from __future__ import annotations

import logging
import sys
import threading
import traceback

from ai_video_studio import APP_ID, APP_NAME, __version__
from ai_video_studio.core import paths
from ai_video_studio.core.logging_setup import configure_logging

log = logging.getLogger("ai_video_studio")


def _install_exception_hooks() -> None:
    def gui_hook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                detail = "".join(traceback.format_exception(exc_type, exc, tb))[-3000:]
                box = QMessageBox(QMessageBox.Icon.Critical, APP_NAME,
                                  f"An unexpected error occurred:\n{exc}\n\nDetails were written to the log file.")
                box.setDetailedText(detail)
                box.exec()
        except Exception:  # noqa: BLE001 - never raise from the excepthook
            pass

    def thread_hook(args: threading.ExceptHookArgs):
        log.critical("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = gui_hook
    threading.excepthook = thread_hook


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    log_path = configure_logging(paths.logs_dir())
    _install_exception_hooks()
    log.info("Starting %s %s", APP_NAME, __version__, extra={"python": sys.version.split()[0],
                                                              "platform": sys.platform})

    if sys.platform == "win32":
        try:  # separate taskbar icon/grouping from python.exe
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass

    from PySide6.QtWidgets import QApplication

    from ai_video_studio.services import Services
    from ai_video_studio.ui.main_window import MainWindow
    from ai_video_studio.ui.theme import STYLESHEET, app_icon

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_ID)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app_icon())

    services = Services()
    from ai_video_studio.core.logging_setup import set_level

    set_level(services.settings.get("log_level"))
    services.jobs.recover()

    window = MainWindow(services)
    window.show()

    # A project path on the command line (e.g. double-clicked .avsproj) wins over "reopen last".
    project_arg = next((a for a in argv[1:] if a.lower().endswith(".avsproj")), None)
    if project_arg:
        window.open_project(project_arg, confirmed=True)
    elif services.settings.get("reopen_last_project"):
        recent = [r for r in services.recent.list() if r["exists"]]
        if recent:
            window.open_project(recent[0]["path"], confirmed=True)
    services.jobs.dispatch()

    log.info("Log file: %s", log_path)
    try:
        return app.exec()
    finally:
        services.close()
        log.info("Shut down")
