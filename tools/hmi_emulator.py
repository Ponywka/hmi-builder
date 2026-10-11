#!/usr/bin/env python3
"""Launcher: ``python3 tools/hmi_emulator.py PROJECT_DIR --pty --http 8765`` (see docs/EMULATOR.md)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hmi_emu.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
