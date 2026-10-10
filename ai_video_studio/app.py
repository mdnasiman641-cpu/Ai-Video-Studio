"""Application entry point."""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback

from ai_video_studio import APP_ID, APP_NAME, __version__
from ai_video_studio.core import paths
from ai_video_studio.core.logging_setup import configure_logging

log = logging.getLogger("ai_video_studio")

SMOKE_TEST_FLAG = "--smoke-test"
_unhandled_errors: list[str] = []


def ensure_std_streams() -> None:
    """Make stdout/stderr safe to write to.

    Under ``pythonw.exe`` (used by run.bat) and PyInstaller windowed builds both are
    ``None``; libraries that print progress (huggingface_hub/tqdm while downloading a
    Whisper model) would then crash. Redirected output on Windows also uses the ANSI code
    page, so unencodable characters (e.g. Bengali file names) are escaped instead of raising.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - lives for the process
        elif hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def _install_exception_hooks(show_dialog: bool = True) -> None:
    def gui_hook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        _unhandled_errors.append(f"{exc_type.__name__}: {exc}")
        if not show_dialog:
            return
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
        _unhandled_errors.append(f"{args.exc_type.__name__}: {args.exc_value}")
        log.critical("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = gui_hook
    threading.excepthook = thread_hook


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    smoke_test = SMOKE_TEST_FLAG in argv
    argv = [a for a in argv if a != SMOKE_TEST_FLAG]
    ensure_std_streams()
    log_path = configure_logging(paths.logs_dir())
    _install_exception_hooks(show_dialog=not smoke_test)
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
    if smoke_test:
        # Start-up check used by CI and setup verification: build the full UI, visit every
        # page, run the event loop briefly, then exit 0 only if nothing raised.
        from PySide6.QtCore import QTimer

        from ai_video_studio.ui.main_window import NAVIGATION

        def visit_pages():
            for name, _icon in NAVIGATION:
                window.navigate(name)
                app.processEvents()

        QTimer.singleShot(200, visit_pages)
        QTimer.singleShot(2500, app.quit)
    code = app.exec()
    if smoke_test:
        window.controller.project = None  # never prompt in smoke-test mode
        code = 1 if _unhandled_errors else 0
        log.info("Smoke test %s", "passed" if code == 0 else "FAILED: " + "; ".join(_unhandled_errors),
                 extra={"platform": app.platformName()})
    clean = services.close()
    log.info("Shut down")
    if not clean:
        # A worker thread is stuck in native code (e.g. model download/decoding) and would keep
        # the process alive after the window closed. Exit now; the job is recovered next start.
        logging.shutdown()
        os._exit(code)
    return code
