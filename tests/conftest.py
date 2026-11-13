"""Shared fixtures: offline socket guard, live mock sites, and storage."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass

import pytest
from werkzeug.serving import make_server

from linkpatrol.config import default_config
from linkpatrol.store import Store
from tests.densesite import create_dense_app
from tests.mocksite.app import create_mock_app
from tests.mocksite.extapp import create_ext_app

@pytest.fixture(scope="session", autouse=True)
def socket_guard():
    """Reject every TCP connection or DNS lookup that is not 127.0.0.1."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    monkeypatch = pytest.MonkeyPatch()

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host != "127.0.0.1":
            raise ConnectionRefusedError(f"offline test guard blocked connection to {host!r}")
        return real_connect(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host != "127.0.0.1":
            raise socket.gaierror(f"offline test guard blocked DNS lookup for {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
    monkeypatch.undo()

@dataclass
class LiveSite:
    base_url: str
    app: object
    server: object
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

def _start(app) -> LiveSite:
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return LiveSite(
        base_url=f"http://127.0.0.1:{server.server_port}",
        app=app,
        server=server,
        thread=thread,
    )

@pytest.fixture(scope="session")
def ext_site():
    site = _start(create_ext_app())
    yield site
    site.stop()

@pytest.fixture(scope="session")
def mocksite(ext_site):
    site = _start(create_mock_app(ext_base=ext_site.base_url))
    yield site
    site.stop()

@pytest.fixture(scope="session")
def dense_site(ext_site):
    site = _start(create_dense_app(ext_base=ext_site.base_url))
    yield site
    site.stop()

@pytest.fixture
def reset_dense(dense_site):
    dense_site.app.config["BROKEN_SHARED"].clear()
    return dense_site

@pytest.fixture
def reset_mock(mocksite):
    mocksite.app.config["COUNTERS"].clear()
    mocksite.app.config["REGRESSION_BROKEN"] = False
    return mocksite

@pytest.fixture
def make_config(tmp_path):
    def factory(site_url: str, **sections):
        from urllib.parse import urlsplit

        path = urlsplit(site_url).path
        seed = site_url.rstrip("/") + "/" if path in {"", "/"} else site_url
        config = default_config(seed)
        config["limits"].update(
            {
                "max_depth": 4,
                "max_pages": 80,
                "timeout_s": 2.0,
                "retries": 2,
                "delay_ms": 0,
                "concurrency": 1,
            }
        )
        for name, values in sections.items():
            config[name].update(values)
        config["output"]["database"] = str(tmp_path / "linkpatrol.db")
        config["output"]["dir"] = str(tmp_path / "reports")
        config["output"]["sitemap"] = str(tmp_path / "sitemap.xml")
        config["seed"] = seed
        return config

    return factory

@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "store.db")
    yield instance
    instance.close()
