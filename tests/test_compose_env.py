"""Web and jobs must agree on the paper universe the Ops page edits."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _service_env(text: str, service: str) -> dict[str, str]:
    """`KEY=value` lines under one top-level compose service (no YAML dependency)."""
    match = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z][\w-]*:\n|\Z)", text, re.M | re.S)
    assert match, f"service {service!r} not found in docker-compose.yml"
    env: dict[str, str] = {}
    for key, value in re.findall(r"^\s+- ([A-Z][A-Z0-9_]*)=(.*)$", match.group(1), re.M):
        env[key] = value.strip()
    return env


def test_web_and_jobs_share_paper_universe_env() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    web = _service_env(text, "web")
    jobs = _service_env(text, "jobs")
    for key in ("JARVISE_PAPER_UNIVERSE", "JARVISE_PAPER_TIMEFRAME"):
        assert key in web, f"web service is missing {key}"
        assert web[key] == jobs[key], f"{key} differs between web and jobs"


def test_jobs_receives_every_env_var_the_assist_reads() -> None:
    """Assist runs in jobs; a variable missing from compose silently reads as unset on the VPS."""
    pattern = re.compile(r"""(?:environ(?:\.get)?\(\s*|environ\[|getenv\(\s*)["']([A-Z][A-Z0-9_]*)["']""")
    read: set[str] = set()
    for path in (ROOT / "src" / "jarvise_assist").glob("*.py"):
        read.update(pattern.findall(path.read_text(encoding="utf-8")))
    assert "JARVISE_ASSIST_EXECUTE" in read
    assert "JARVISE_ASSIST_REDUCE" in read
    assert "JARVISE_ASSIST_CANCEL_OWNER" in read
    jobs = _service_env((ROOT / "docker-compose.yml").read_text(encoding="utf-8"), "jobs")
    missing = sorted(read - set(jobs))
    assert not missing, f"jobs service is missing {missing}"
    assert jobs.get("JARVISE_ASSIST_REDUCE") == "${JARVISE_ASSIST_REDUCE:-off}"
    assert jobs.get("JARVISE_ASSIST_CANCEL_OWNER") == "${JARVISE_ASSIST_CANCEL_OWNER:-off}"
    # bounded_int_env reads os.environ.get(name); the name is often a constant, not a literal.
    read |= _bounded_int_env_names(ROOT / "src" / "jarvise_assist")
    missing_bounded = sorted(read - set(jobs))
    assert not missing_bounded, f"jobs service is missing {missing_bounded}"


def _bounded_int_env_names(root: Path) -> set[str]:
    """Env names passed to bounded_int_env, including identifiers bound to string constants."""
    names: set[str] = set()
    call = re.compile(r"""bounded_int_env\(\s*(?:["']([A-Z][A-Z0-9_]*)["']|([A-Z][A-Z0-9_]*))""")
    const = re.compile(
        r"""^([A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=\s*["']([A-Z][A-Z0-9_]*)["']""",
        re.M,
    )
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        bound = dict(const.findall(text))
        for literal, ident in call.findall(text):
            if literal:
                names.add(literal)
            elif ident in bound:
                names.add(bound[ident])
    return names


def test_jobs_forwards_owner_tx_bounds() -> None:
    """Owner hist/live age settings are forwarded with the code defaults."""
    from jarvise_assist.partner_owner_settings import (
        OWNER_HIST_MAX_LOOKBACK_DAYS_DEFAULT,
        OWNER_HIST_MAX_LOOKBACK_DAYS_ENV,
        OWNER_TX_MAX_AGE_HOURS_DEFAULT,
        OWNER_TX_MAX_AGE_HOURS_ENV,
    )

    found = _bounded_int_env_names(ROOT / "src" / "jarvise_assist")
    assert OWNER_TX_MAX_AGE_HOURS_ENV in found
    assert OWNER_HIST_MAX_LOOKBACK_DAYS_ENV in found
    jobs = _service_env((ROOT / "docker-compose.yml").read_text(encoding="utf-8"), "jobs")
    assert jobs[OWNER_TX_MAX_AGE_HOURS_ENV] == (
        f"${{{OWNER_TX_MAX_AGE_HOURS_ENV}:-{OWNER_TX_MAX_AGE_HOURS_DEFAULT}}}"
    )
    assert jobs[OWNER_HIST_MAX_LOOKBACK_DAYS_ENV] == (
        f"${{{OWNER_HIST_MAX_LOOKBACK_DAYS_ENV}:-{OWNER_HIST_MAX_LOOKBACK_DAYS_DEFAULT}}}"
    )
    assert OWNER_TX_MAX_AGE_HOURS_DEFAULT == 6
    assert OWNER_HIST_MAX_LOOKBACK_DAYS_DEFAULT == 30


def test_assist_trade_key_env_is_jobs_only() -> None:
    """Futures assist trade key must not be forwarded into the web container."""
    trade_keys = (
        "BINANCE_TRADE_API_KEY",
        "BINANCE_TRADE_API_SECRET",
        "BINANCE_TRADE_PRIVATE_KEY_PATH",
        "BINANCE_TRADE_PRIVATE_KEY_PASSPHRASE",
    )
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    web = _service_env(text, "web")
    jobs = _service_env(text, "jobs")
    for key in trade_keys:
        assert key in jobs, f"jobs service is missing {key}"
        assert key not in web, f"web service must not receive {key}"
