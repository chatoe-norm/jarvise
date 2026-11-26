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
# Longer than the adapter research deadline (600s) so silent upstream TLS does not cut mid-run.
_TUNNEL_IDLE_S = 900.0
_HEADER_READ_TIMEOUT_S = 30.0
_MAX_HEADER_LINES = 64


def connect_validated(
    addrs: list[str],
    port: int,
    *,
    timeout: float = 30.0,
) -> socket.socket:
    """Connect to a previously validated numeric address. Never re-resolve a hostname."""
    last_err: OSError | None = None
    allowed = {str(a) for a in addrs}
    for addr in addrs:
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((addr, port))
            peer = sock.getpeername()[0]
            # Normalize IPv6 peer presentation.
            peer_s = str(peer).split("%", 1)[0]
            if peer_s not in allowed and peer not in allowed:
                sock.close()
                raise OSError(f"peer_not_validated:{peer}")
            return sock
        except OSError as exc:
            last_err = exc
            try:
                sock.close()
            except OSError:
                pass
    if last_err is not None:
        raise last_err
    raise OSError("no_validated_addrs")


class _ProxyHandler(socketserver.StreamRequestHandler):
    resolve: Resolver | None = None
    on_deny: Callable[[str, str], None] | None = None
    # Test hook: records the address used for the upstream connect.
    last_connect_addr: str | None = None

    def handle(self) -> None:
        if self.connection is not None:
            try:
                self.connection.settimeout(_HEADER_READ_TIMEOUT_S)
            except OSError:
                pass
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
        ok, reason, addrs = allow_connect_target(host, port, resolve=self.resolve)
        if not ok:
            if self.on_deny:
                self.on_deny(target, reason)
            self._reply(403, b"Forbidden")
            return
        # Drain headers with a hard line cap (resource exhaustion).
        for _ in range(_MAX_HEADER_LINES):
            try:
                hdr = self.rfile.readline(65536)
            except OSError:
                return
            if not hdr or hdr in (b"\r\n", b"\n"):
                break
        else:
            self._reply(400, b"Bad Request")
            return
        try:
            upstream = connect_validated(addrs, port, timeout=30)
            type(self).last_connect_addr = upstream.getpeername()[0]
        except OSError:
            self._reply(502, b"Bad Gateway")
            return
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
            self._tunnel(self.connection, upstream)
        finally:
            upstream.close()

    def _split_host_port(self, target: str) -> tuple[str | None, str | None]:
        if target.startswith("["):
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
            readable, _, errored = select.select(sockets, [], sockets, _TUNNEL_IDLE_S)
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
        self.handler_cls: type[_ProxyHandler] | None = None

    def start(self) -> None:
        handler = type(
            "BoundProxyHandler",
            (_ProxyHandler,),
            {"resolve": self.resolve, "on_deny": None, "last_connect_addr": None},
        )
        self.handler_cls = handler
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
