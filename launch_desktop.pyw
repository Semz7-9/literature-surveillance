"""Windows double-click entry point with visible startup diagnostics."""

import ctypes
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PYTHONW = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
STARTUP_LOG = PROJECT_ROOT / "data" / "desktop-startup.log"


def show_error(message: str) -> None:
    """Show errors that pythonw would otherwise hide from the user."""
    ctypes.windll.user32.MessageBoxW(None, message, "文献助手启动失败", 0x10)


def run() -> None:
    os.chdir(PROJECT_ROOT)

    # A .pyw file may be associated with a system Python that does not have the
    # project dependencies. Hand the process to the repository virtualenv.
    if VENV_PYTHONW.exists() and Path(sys.executable).resolve() != VENV_PYTHONW.resolve():
        subprocess.Popen([str(VENV_PYTHONW), str(Path(__file__).resolve())], cwd=PROJECT_ROOT)
        return

    try:
        from src.desktop import main

        main()
    except BaseException as exc:
        STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        STARTUP_LOG.write_text(
            f"{datetime.now().isoformat()}\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        show_error(
            f"程序未能启动：{exc}\n\n"
            f"诊断日志已保存到：\n{STARTUP_LOG}\n\n"
            "也可以双击“启动文献助手.cmd”查看完整错误。"
        )


run()
