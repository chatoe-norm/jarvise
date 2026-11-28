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


def test_isolation_script_connect_read_is_a_bounded_loop() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "while total < read_max:" in text
    assert "s.recv(read_max - total)" in text
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
