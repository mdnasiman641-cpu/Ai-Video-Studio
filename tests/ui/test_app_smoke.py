"""Start the real application entry point in a separate process."""

import os
import subprocess
import sys


def test_app_smoke_test_starts_and_exits_cleanly(tmp_path):
    env = dict(os.environ, AVS_DATA_DIR=str(tmp_path / "data"))
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    proc = subprocess.run([sys.executable, "-m", "ai_video_studio", "--smoke-test"], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    log = (tmp_path / "data" / "logs" / "ai_video_studio.log").read_text(encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "Smoke test passed" in log
    assert '"level": "CRITICAL"' not in log
