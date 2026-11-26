"""HTTP CONNECT proxy that enforces the public-HTTPS policy."""

from __future__ import annotations

import select
import socket
import socketserver
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

from jarvise_intel.policy import Resolver, allow_connect_target

_BUFFER = 65536


class _ProxyHandler(socketserver.StreamRequestHandler):
    resolve: Resolver | None = None
    on_deny: Callable[[str, str], None] | None = None

    def handle(self) -> None:
        try:
            first = self.rfile.readline(65536)
        except OSError:
            return
        if not first:
            return
        line = first.decode("latin-1", errors="replace").strip()
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            self._reply(405, b"Method Not Allowed")
            return
        target = parts[1]
        host, port_s = self._split_host_port(target)
        if host is None or port_s is None:
            self._reply(400, b"Bad Request")
            return
        try:
            port = int(port_s)
        except ValueError:
            self._reply(400, b"Bad Request")
            return
        ok, reason = allow_connect_target(host, port, resolve=self.resolve)
        if not ok:
            if self.on_deny:
                self.on_deny(target, reason)
            self._reply(403, b"Forbidden")
            return
        # Drain headers
        while True:
            hdr = self.rfile.readline(65536)
            if not hdr or hdr in (b"\r\n", b"\n"):
                break
        try:
            upstream = socket.create_connection((host, port), timeout=30)
        except OSError:
            self._reply(502, b"Bad Gateway")
            return
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            self._tunnel(self.connection, upstream)
        finally:
            upstream.close()

    def _split_host_port(self, target: str) -> tuple[str | None, str | None]:
        if target.startswith("["):
            # [ipv6]:port
            end = target.find("]")
            if end < 0:
                return None, None
            host = target[1:end]
            rest = target[end + 1 :]
            if not rest.startswith(":"):
                return None, None
            return host, rest[1:]
        if ":" not in target:
            return None, None
        host, port = target.rsplit(":", 1)
        return host, port

    def _reply(self, code: int, body: bytes) -> None:
        reason = {
            400: "Bad Request",
            403: "Forbidden",
            405: "Method Not Allowed",
            502: "Bad Gateway",
        }.get(code, "Error")
        payload = (
            f"HTTP/1.1 {code} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii") + body
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            return

    def _tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        while True:
            readable, _, errored = select.select(sockets, [], sockets, 60)
            if errored or not readable:
                return
            for sock in readable:
                other = upstream if sock is client else client
                try:
                    data = sock.recv(_BUFFER)
                except OSError:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return


class EgressProxy:
    """Threading CONNECT proxy bound to a host/port."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8899,
        *,
        resolve: Resolver | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.resolve = resolve
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = type(
            "BoundProxyHandler",
            (_ProxyHandler,),
            {"resolve": self.resolve, "on_deny": None},
        )
        server = socketserver.ThreadingTCPServer((self.host, self.port), handler)
        server.daemon_threads = True
        server.allow_reuse_address = True
        self._server = server
        thread = threading.Thread(target=server.serve_forever, name="hermes-egress", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def proxy_url_from_env(raw: str) -> tuple[str, int] | None:
    """Parse http://host:port from HTTPS_PROXY-style values."""
    text = (raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if not parts.hostname or parts.port is None:
        return None
    return parts.hostname, int(parts.port)


def main() -> None:
    import os

    host = os.environ.get("HERMES_EGRESS_HOST", "0.0.0.0")
    port = int(os.environ.get("HERMES_EGRESS_PORT", "8899"))
    proxy = EgressProxy(host=host, port=port)
    proxy.start()
    print(f"hermes-egress listening on {host}:{port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        proxy.stop()


if __name__ == "__main__":
    main()
