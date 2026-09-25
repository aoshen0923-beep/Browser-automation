"""Entry point for the PyInstaller build (the frozen quizpilot.exe)."""

import sys

from quizpilot.cli import main

if __name__ == "__main__":
    sys.exit(main())
