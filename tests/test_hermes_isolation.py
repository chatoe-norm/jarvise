"""Optional Docker isolation proofs for Hermes Stage A.

Skipped when Docker is unavailable. Does not touch the VPS or production secrets.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hermes-isolation-check.sh"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=20,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(
    os.environ.get("JARVISE_HERMES_ISO") != "1",
    reason="set JARVISE_HERMES_ISO=1 to run the local Docker Hermes isolation script",
)
@pytest.mark.skipif(not _docker_available(), reason="Docker unavailable for Hermes isolation check")
def test_hermes_isolation_script() -> None:
    assert SCRIPT.is_file()
    # Git Bash / WSL / Linux. On native Windows without bash, skip clearly.
    bash = shutil.which("bash") or shutil.which("sh")
    if bash is None:
        pytest.skip("bash/sh required to run hermes-isolation-check.sh")
    result = subprocess.run(
        [bash, str(SCRIPT)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def _isolation_connect_ns() -> dict[str, Any]:
    """Exec the script's CONNECT helpers only. Does not open Docker."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("import os\nimport socket\nimport sys\n")
    end = text.index("\nfor target in", start)
    ns: dict[str, Any] = {}
    exec(text[start:end], ns)  # noqa: S102 — the script under test is the source
    return ns


def test_isolation_script_connect_read_is_a_bounded_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_HERMES_ISO_CONNECT_READ_MAX", raising=False)
    monkeypatch.delenv("JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S", raising=False)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "while total < read_max:" in text
    assert "s.recv(read_max - total)" in text
    assert 'if b"\\n" in piece:' in text
    assert "finally:" in text
    assert "JARVISE_HERMES_ISO_CONNECT_READ_MAX" in text
    assert "JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S" in text
    assert "${JARVISE_HERMES_ISO_CONNECT_READ_MAX:-256}" in text
    assert "${JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S:-15}" in text
    assert "def http_status(" in text
    ns = _isolation_connect_ns()
    assert ns["_positive_int_env"]("JARVISE_HERMES_ISO_CONNECT_READ_MAX", 256) == 256
    assert ns["_positive_int_env"]("JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S", 15) == 15


def test_connect_req_joins_a_split_status_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status line split across two sends is complete before http_status runs."""
    ns = _isolation_connect_ns()
    monkeypatch.delenv("JARVISE_HERMES_ISO_CONNECT_READ_MAX", raising=False)
    monkeypatch.delenv("JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S", raising=False)

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    server.settimeout(5)

    def serve() -> None:
        try:
            conn, _addr = server.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 ")
            time.sleep(0.2)
            conn.sendall(b"200 Connection Established\r\n\r\n")
            conn.settimeout(2)
            try:
                conn.recv(16)
            except OSError:
                pass
        finally:
            conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    real_connect = socket.create_connection

    def create_connection(addr: tuple[str, int], timeout: float | None = None) -> socket.socket:
        assert addr == ("hermes-egress", 8899)
        return real_connect(("127.0.0.1", port), timeout)

    monkeypatch.setattr(ns["socket"], "create_connection", create_connection)
    try:
        data = ns["connect_req"]("nvidia.com:443")
    finally:
        thread.join(timeout=5)
        server.close()
    assert ns["http_status"](data) == 200
    assert data.startswith(b"HTTP/1.1 200")


def test_connect_req_honors_read_cap_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    ns = _isolation_connect_ns()
    seen: dict[str, Any] = {}

    class _Sock:
        def settimeout(self, value: float) -> None:
            seen["timeout"] = value

        def sendall(self, data: bytes) -> None:
            seen["sent"] = data

        def recv(self, n: int) -> bytes:
            seen.setdefault("asks", []).append(n)
            return b"X" * n

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: _Sock())
    monkeypatch.setenv("JARVISE_HERMES_ISO_CONNECT_READ_MAX", "8")
    monkeypatch.setenv("JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S", "4")
    data = ns["connect_req"]("nvidia.com:443")
    assert data == b"X" * 8
    assert seen["timeout"] == 4
    assert seen["asks"] == [8]
    assert ns["http_status"](data) is None

    monkeypatch.setenv("JARVISE_HERMES_ISO_CONNECT_READ_MAX", "nope")
    monkeypatch.setenv("JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S", "0")
    seen.clear()

    class _OneLine:
        def settimeout(self, value: float) -> None:
            seen["timeout"] = value

        def sendall(self, _data: bytes) -> None:
            return None

        def recv(self, n: int) -> bytes:
            seen["n"] = n
            return b"HTTP/1.1 403 Forbidden\r\n"

        def close(self) -> None:
            return None

    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: _OneLine())
    data = ns["connect_req"]("example.com:443")
    assert seen["timeout"] == 15
    assert seen["n"] == 256
    assert ns["http_status"](data) == 403


def _clear_iso_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_HERMES_ISO_CONNECT_READ_MAX", raising=False)
    monkeypatch.delenv("JARVISE_HERMES_ISO_CONNECT_TIMEOUT_S", raising=False)


class _ScriptedSocket:
    """Fake socket. ``recv`` yields scripted bytes or raises the scripted error."""

    def __init__(self, steps: list[bytes | type[BaseException] | BaseException]) -> None:
        self._steps = list(steps)
        self.closed = False
        self.calls = 0

    def settimeout(self, _value: float) -> None:
        return None

    def sendall(self, _data: bytes) -> None:
        return None

    def recv(self, n: int) -> bytes:
        self.calls += 1
        if not self._steps:
            raise AssertionError("recv past script")
        step = self._steps.pop(0)
        if isinstance(step, type) and issubclass(step, BaseException):
            raise step()
        if isinstance(step, BaseException):
            raise step
        return step[:n]

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("code", [b"200", b"403"])
def test_http_status_rejects_a_partial_status_line(code: bytes) -> None:
    """No LF means the status line is incomplete, even when the digits are present."""
    ns = _isolation_connect_ns()
    partial = b"HTTP/1.1 " + code
    assert b"\n" not in partial
    assert ns["http_status"](partial) is None
    assert ns["http_status"](partial + b" OK") is None


def test_http_status_accepts_lf_terminated_lines() -> None:
    ns = _isolation_connect_ns()
    assert ns["http_status"](b"HTTP/1.1 200 OK\r\n") == 200
    assert ns["http_status"](b"HTTP/1.1 403 Forbidden\r\n") == 403


@pytest.mark.parametrize("code", [b"200", b"403"])
def test_connect_req_partial_status_then_timeout_is_not_a_code(
    monkeypatch: pytest.MonkeyPatch, code: bytes
) -> None:
    ns = _isolation_connect_ns()
    _clear_iso_env(monkeypatch)
    sock = _ScriptedSocket([b"HTTP/1.1 " + code, TimeoutError])
    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: sock)
    data = ns["connect_req"]("nvidia.com:443")
    assert data == b"HTTP/1.1 " + code
    assert ns["http_status"](data) is None
    assert sock.closed is True


@pytest.mark.parametrize("code", [b"200", b"403"])
def test_connect_req_partial_status_then_eof_is_not_a_code(
    monkeypatch: pytest.MonkeyPatch, code: bytes
) -> None:
    """Empty recv ends the read. A missing break must fail this test, not hang it."""
    ns = _isolation_connect_ns()
    _clear_iso_env(monkeypatch)
    stop = threading.Event()
    partial = b"HTTP/1.1 " + code

    class _Eof:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        def settimeout(self, _value: float) -> None:
            return None

        def sendall(self, _data: bytes) -> None:
            return None

        def recv(self, _n: int) -> bytes:
            if stop.is_set():
                raise RuntimeError("eof test guard")
            self.calls += 1
            if self.calls == 1:
                return partial
            return b""

        def close(self) -> None:
            self.closed = True

    sock = _Eof()
    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: sock)
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["data"] = ns["connect_req"]("nvidia.com:443")
        except Exception as exc:  # noqa: BLE001 — guard raises only if EOF never stops the read
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2.0)
    hung = thread.is_alive()
    stop.set()
    thread.join(1.0)
    assert not hung, "connect_req did not return after EOF"
    assert "error" not in box
    assert box["data"] == partial
    assert ns["http_status"](box["data"]) is None
    assert sock.closed is True
    assert sock.calls == 2


@pytest.mark.parametrize("code", [b"200", b"403"])
def test_connect_req_partial_status_at_read_cap_is_not_a_code(
    monkeypatch: pytest.MonkeyPatch, code: bytes
) -> None:
    ns = _isolation_connect_ns()
    _clear_iso_env(monkeypatch)
    partial = b"HTTP/1.1 " + code
    monkeypatch.setenv("JARVISE_HERMES_ISO_CONNECT_READ_MAX", str(len(partial)))
    sock = _ScriptedSocket([partial])
    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: sock)
    data = ns["connect_req"]("nvidia.com:443")
    assert data == partial
    assert b"\n" not in data
    assert ns["http_status"](data) is None
    assert sock.closed is True


def test_connect_req_split_complete_status_line_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HTTP/1.1 200 OK\\r\\n`` split across two recvs is still status 200."""
    ns = _isolation_connect_ns()
    _clear_iso_env(monkeypatch)
    sock = _ScriptedSocket([b"HTTP/1.1 200 OK\r", b"\n"])
    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: sock)
    data = ns["connect_req"]("nvidia.com:443")
    assert data == b"HTTP/1.1 200 OK\r\n"
    assert ns["http_status"](data) == 200
    assert sock.closed is True
    assert sock.calls == 2


def test_connect_req_complete_403_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    ns = _isolation_connect_ns()
    _clear_iso_env(monkeypatch)
    sock = _ScriptedSocket([b"HTTP/1.1 403 Forbidden\r\n"])
    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: sock)
    data = ns["connect_req"]("example.com:443")
    assert ns["http_status"](data) == 403
    assert sock.closed is True


def test_connect_req_closes_socket_when_send_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ns = _isolation_connect_ns()
    _clear_iso_env(monkeypatch)
    seen: dict[str, bool] = {}

    class _Boom:
        def settimeout(self, _value: float) -> None:
            return None

        def sendall(self, _data: bytes) -> None:
            raise RuntimeError("send failed")

        def recv(self, _n: int) -> bytes:
            raise AssertionError("recv after a failed send")

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(ns["socket"], "create_connection", lambda *_a, **_k: _Boom())
    with pytest.raises(RuntimeError, match="send failed"):
        ns["connect_req"]("nvidia.com:443")
    assert seen.get("closed") is True
