"""Optional Docker isolation proofs for Hermes Stage A.

Skipped when Docker is unavailable. Does not touch the VPS or production secrets.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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
