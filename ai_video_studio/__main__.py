import sys


def _run() -> int:
    try:
        from ai_video_studio.app import main
    except BaseException:  # noqa: BLE001
        # Under pythonw.exe there is no console, so an import-time failure would be invisible.
        # Leave a crash report in the data folder (or the temp folder as a last resort).
        import os
        import tempfile
        import traceback

        text = traceback.format_exc()
        base = os.environ.get("AVS_DATA_DIR") or os.path.join(
            os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "AIVideoStudio")
        try:
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "startup_error.log"), "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        if sys.stderr is not None:
            sys.stderr.write(text)
        return 1
    return main()


if __name__ == "__main__":
    raise SystemExit(_run())
