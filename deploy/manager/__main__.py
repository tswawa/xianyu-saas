#!/usr/bin/env python3
"""Executable entry point for source runs and PyInstaller."""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from deploy.manager.cli import main
else:
    from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
