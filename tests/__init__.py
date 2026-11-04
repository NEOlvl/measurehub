"""Tests."""

import os

# Qt must not look for a display (CI, headless runs): set before Qt is first imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
