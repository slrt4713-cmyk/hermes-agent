"""Loopback relay that carries a cloud browser's CDP websocket through the HTTP(S) proxy.

agent-browser dials ``--cdp wss://...`` directly and ignores HTTPS_PROXY for that socket. In a
container whose only egress is an HTTP proxy (no DNS, direct TCP dropped) every cloud navigation
then failed with "failed to lookup address information". When a proxy applies to the CDP host,
:func:`relay_cdp_url` returns a ``ws://127.0.0.1:<port>/...`` URL served by a local listener that
opens a CONNECT tunnel through the proxy, adds TLS for ``wss``, rewrites the Host header and pipes
bytes both ways. Without a proxy the URL is returned unchanged."""

from __future__ import annotations

import base64
import logging
import socket
import ssl
import threading
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_HEAD_LIMIT = 64 * 1024
_CONNECT_TIMEOUT = 15
_relays: dict[tuple[str, str, int], "_Relay"] = {}
_relays_lock = threading.Lock()


def _proxy_for(scheme: str, host: str) -> Optional[str]:
    if urllib.request.proxy_bypass(host):
        return None
    proxies = urllib.request.getproxies()
    return proxies.get("https" if scheme == "wss" else "http") or proxies.get("all")


def relay_cdp_url(cdp_url: str) -> str:
    """The CDP URL to hand agent-browser: a loopback relay when a proxy applies, else unchanged."""
    parsed = urllib.parse.urlsplit(cdp_url or "")
    scheme, host = parsed.scheme.lower(), parsed.hostname or ""
    if scheme not in ("ws", "wss") or not host or host in ("127.0.0.1", "localhost", "::1"):
        return cdp_url
    proxy = _proxy_for(scheme, host)
    if not proxy:
        return cdp_url
    port = parsed.port or (443 if scheme == "wss" else 80)
    key = (scheme, host, port)
    with _relays_lock:
        relay = _relays.get(key)
        if relay is None or relay.proxy != proxy:
            if relay is not None:
                relay.close()
            relay = _relays[key] = _Relay(scheme, host, port, proxy)
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(("ws", f"127.0.0.1:{relay.local_port}", path, parsed.query, ""))


class _Relay:
    def __init__(self, scheme: str, host: str, port: int, proxy: str) -> None:
        self.scheme, self.host, self.port, self.proxy = scheme, host, port, proxy
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.local_port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, name=f"cdp-relay-{host}", daemon=True).start()

    def close(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        try:
            head = _read_head(client)
            upstream = self._open_upstream()
            upstream.sendall(_rewrite_host(head, self.host, self.port, self.scheme))
            _pipe(client, upstream)
        except Exception as exc:  # one failed connection must not kill the listener
            logger.warning("CDP relay to %s:%s failed: %s", self.host, self.port, exc)
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _open_upstream(self) -> socket.socket:
        proxy = urllib.parse.urlsplit(self.proxy)
        sock = socket.create_connection((proxy.hostname, proxy.port or 80), timeout=_CONNECT_TIMEOUT)
        request = f"CONNECT {self.host}:{self.port} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
        if proxy.username:
            credentials = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or '')}"
            request += f"Proxy-Authorization: Basic {base64.b64encode(credentials.encode()).decode()}\r\n"
        sock.sendall((request + "\r\n").encode())
        status = _read_head(sock).split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[1] != b"200":
            sock.close()
            raise ConnectionError(f"proxy refused CONNECT: {b' '.join(status[:3]).decode(errors='replace')}")
        if self.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(None)
        return sock


def _read_head(sock: socket.socket) -> bytes:
    """Read one HTTP message head byte by byte, so nothing past the blank line is consumed."""
    data = bytearray()
    while not data.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("connection closed before the HTTP head ended")
        data += chunk
        if len(data) > _HEAD_LIMIT:
            raise ConnectionError("HTTP head too large")
    return bytes(data)


def _rewrite_host(head: bytes, host: str, port: int, scheme: str) -> bytes:
    default_port = 443 if scheme == "wss" else 80
    authority = host if port == default_port else f"{host}:{port}"
    lines = head.split(b"\r\n")
    for index, line in enumerate(lines[1:], start=1):
        if line.lower().startswith(b"host:"):
            lines[index] = b"Host: " + authority.encode()
    return b"\r\n".join(lines)


def _pipe(left: socket.socket, right: socket.socket) -> None:
    def forward(source: socket.socket, target: socket.socket) -> None:
        try:
            while chunk := source.recv(65536):
                target.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (source, target):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    other = threading.Thread(target=forward, args=(right, left), daemon=True)
    other.start()
    forward(left, right)
    other.join()
    left.close()
    right.close()
