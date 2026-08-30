"""Desktop shell lifecycle tests without opening a real GUI."""

import time

from src.desktop import LocalApplicationServer, find_free_local_port


class FakeServer:
    def __init__(self, config):
        self.config = config
        self.started = False
        self.should_exit = False
        self.force_exit = False

    def run(self):
        self.started = True
        while not self.should_exit and not self.force_exit:
            time.sleep(0.005)


def test_desktop_server_uses_loopback_and_stops_cleanly():
    port = find_free_local_port()
    server = LocalApplicationServer(port=port, server_factory=FakeServer)
    assert server.server.config.host == "127.0.0.1"
    assert server.url == f"http://127.0.0.1:{port}"
    server.start(timeout=1)
    assert server.thread.is_alive()
    server.stop(timeout=1)
    assert not server.thread.is_alive()
