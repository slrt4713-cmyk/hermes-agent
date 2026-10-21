"""The CDP relay carries a cloud browser websocket through an HTTP CONNECT proxy."""

from __future__ import annotations

import socket
import threading

import pytest

from tools import browser_cdp_relay as relay


def _serve_once(handler) -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run() -> None:
        conn, _ = listener.accept()
        try:
            handler(conn)
        finally:
            conn.close()
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


@pytest.fixture(autouse=True)
def _fresh_relays(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(relay, "_relays", {})
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)


def test_without_proxy_the_url_is_unchanged() -> None:
    url = "wss://connect.browserbase.com/?signingKey=abc"
    assert relay.relay_cdp_url(url) == url


def test_loopback_and_bypassed_hosts_are_not_relayed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("NO_PROXY", "internal.example")
    assert relay.relay_cdp_url("ws://127.0.0.1:9222/devtools/browser/x") == "ws://127.0.0.1:9222/devtools/browser/x"
    assert relay.relay_cdp_url("wss://internal.example/x") == "wss://internal.example/x"


def test_relay_tunnels_through_connect_and_rewrites_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bytes] = {}

    def upstream(conn: socket.socket) -> None:
        seen["head"] = relay._read_head(conn)
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n" + conn.recv(5)[::-1])

    upstream_port = _serve_once(upstream)

    def proxy(conn: socket.socket) -> None:
        seen["connect"] = relay._read_head(conn)
        target = socket.create_connection(("127.0.0.1", upstream_port))
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        relay._pipe(conn, target)

    proxy_port = _serve_once(proxy)
    monkeypatch.setenv("http_proxy", f"http://user:pa%40ss@127.0.0.1:{proxy_port}")
    # The fake upstream listens on loopback; resolve the cloud name there for the CONNECT target.
    monkeypatch.setattr(relay.urllib.request, "proxy_bypass", lambda host: False)

    local = relay.relay_cdp_url(f"ws://cloud.example:{upstream_port}/devtools?signingKey=k1")
    assert local.startswith("ws://127.0.0.1:") and local.endswith("/devtools?signingKey=k1")

    port = int(local.split(":")[2].split("/")[0])
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(b"GET /devtools?signingKey=k1 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n\r\nhello" % port)
    reply = b""
    while not reply.endswith(b"olleh"):
        chunk = client.recv(1024)
        assert chunk, reply
        reply += chunk
    client.close()

    assert seen["connect"].startswith(b"CONNECT cloud.example:%d HTTP/1.1" % upstream_port)
    assert b"Proxy-Authorization: Basic dXNlcjpwYUBzcw==" in seen["connect"]
    assert b"Host: cloud.example:%d\r\n" % upstream_port in seen["head"]
    assert reply.startswith(b"HTTP/1.1 101")


def test_proxy_refusal_closes_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def proxy(conn: socket.socket) -> None:
        relay._read_head(conn)
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")

    proxy_port = _serve_once(proxy)
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setattr(relay.urllib.request, "proxy_bypass", lambda host: False)
    local = relay.relay_cdp_url("wss://cloud.example/x")
    port = int(local.split(":")[2].split("/")[0])
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(b"GET /x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    assert client.recv(1024) == b""
