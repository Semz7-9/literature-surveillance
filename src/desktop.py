"""Native desktop shell for the local Literature Surveillance application."""

import socket
import threading
import time
from collections.abc import Callable

import uvicorn

from .web.app import create_app


def find_free_local_port() -> int:
    """Ask Windows for an available loopback port instead of exposing a fixed port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class LocalApplicationServer:
    """Run Uvicorn behind the desktop window and own its full lifecycle."""

    def __init__(
        self,
        *,
        port: int | None = None,
        server_factory: Callable[[uvicorn.Config], uvicorn.Server] = uvicorn.Server,
    ):
        self.port = port or find_free_local_port()
        config = uvicorn.Config(
            create_app(), host="127.0.0.1", port=self.port,
            log_level="warning", access_log=False,
        )
        self.server = server_factory(config)
        self.thread = threading.Thread(
            target=self.server.run, name="literature-surveillance-local-server", daemon=True,
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise RuntimeError("本地应用服务启动失败")
            time.sleep(0.05)
        self.stop()
        raise TimeoutError("本地应用服务启动超时")

    def stop(self, timeout: float = 10.0) -> None:
        if not self.thread.is_alive():
            return
        self.server.should_exit = True
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(2)


def main() -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "缺少桌面窗口依赖。请先执行：pip install -e ."
        ) from exc

    server = LocalApplicationServer()
    server.start()
    try:
        webview.create_window(
            "学术领域维护系统",
            server.url,
            width=1240,
            height=820,
            min_size=(900, 620),
            background_color="#f6f6fc",
            text_select=True,
        )
        webview.start(debug=False)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
