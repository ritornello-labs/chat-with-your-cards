#!/usr/bin/env python3
"""Launch disposable Anki for interactive design iteration on macOS.

anki-addon-workbench 0.4.x's `launch` waits for the Anki window via
xdotool, which cannot see native macOS windows (X11 only). This wrapper
reuses the whole workbench launch pipeline but swaps the window wait for
a time-based one. Delete once the workbench handles macOS natively.

Usage (typically backgrounded, with --hold to keep Anki alive):
    uv run --group dev python scripts/dev_launch.py --artifact-dir .tmp-gui-workbench --hold
"""

from __future__ import annotations

import sys
import time
from typing import Any

import anki_addon_workbench.runner as runner
from anki_addon_workbench.cli import main

STARTUP_WAIT_SECONDS = 15.0


def _wait_without_xdotool(
    process: Any,
    env: dict[str, str],
    *,
    title: str = "Anki",
    timeout: int = 45,
) -> int | None:
    deadline = time.monotonic() + min(float(timeout), STARTUP_WAIT_SECONDS)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        time.sleep(0.5)
    return 0  # pseudo window id; the caller only checks for None


runner.wait_for_window = _wait_without_xdotool

if __name__ == "__main__":
    sys.exit(main(["launch", *sys.argv[1:]]))
