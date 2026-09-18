"""Entry point for building with PyInstaller - portable (embedded) mode.

Runs the FastAPI app with a data dir next to the executable so a portable
build keeps its state with it rather than in the OS temp dir.
"""
import os
import sys
from pathlib import Path


def _guard_streams() -> None:
    """Make sure sys.stdout/stderr exist before uvicorn configures logging.

    A windowed (console=False) PyInstaller build runs with sys.stdout is None.
    uvicorn's *default* logging config builds a formatter that calls
    sys.stdout.isatty(), so `api.run()` - which passes no log_config - died at
    startup with "Unable to configure formatter 'default'" (older uvicorn:
    "'NoneType' object has no attribute 'isatty'"). This entry point cannot
    change api.run(), so it fixes the streams up front and reuses desktop.py's
    guard (which also opens a log file next to the data) so both entry points
    behave identically.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    from desktop import _ensure_streams  # same folder; bundled alongside
    _ensure_streams()


def main() -> None:
    if getattr(sys, "frozen", False):
        # Portability: keep data next to the exe.
        base = Path(sys.executable).parent
        os.environ.setdefault("PCRITUALS_DATA_DIR", str(base / "data"))
    _guard_streams()
    from pcrituals.api import run
    run()


if __name__ == "__main__":
    main()
