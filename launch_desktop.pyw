"""Double-click entry point for Windows."""

import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from src.desktop import main  # noqa: E402

main()
