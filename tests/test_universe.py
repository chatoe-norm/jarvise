"""Point-in-time universe membership.

Selecting today's top coins and backtesting them over five years is survivorship
bias. Membership is versioned by listed_at / delisted_at so a walk-forward can
ask which symbols were eligible at a decision time.
"""

import time
from pathlib import Path

from jarvise_ingest.db import open_db, record_membership, universe_as_of
from jarvise_ingest.universe import PAPER_CORE, seed_named_universe, seed_paper_core


def test_universe_as_of_excludes_not_yet_listed(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    record_membership(
        conn,
        universe_id="paper_core",
        symbol="BTCUSDT",
        asset_class="crypto",
        listed_at=1_609_459_200_000,  # 2021-01-01
    )
    record_membership(
        conn,
        universe_id="paper_core",
        symbol="SOLUSDT",
        asset_class="crypto",
        listed_at=1_641_000_000_000,  # ~2022-01
    )

    early = universe_as_of(conn, "paper_core", as_of_ms=1_620_000_000_000)
    late = universe_as_of(conn, "paper_core", as_of_ms=1_650_000_000_000)

    assert early == ["BTCUSDT"]
    assert late == ["BTCUSDT", "SOLUSDT"]
    conn.close()


def test_universe_as_of_excludes_delisted(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    record_membership(
        conn,
        universe_id="paper_core",
        symbol="LUNAUSDT",
        asset_class="crypto",
        listed_at=1_600_000_000_000,
        delisted_at=1_652_300_000_000,
    )

    during = universe_as_of(conn, "paper_core", as_of_ms=1_640_000_000_000)
    after = universe_as_of(conn, "paper_core", as_of_ms=1_660_000_000_000)

    assert during == ["LUNAUSDT"]
    assert after == []
    conn.close()


def test_record_membership_is_idempotent_for_same_interval(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    kwargs = dict(
        universe_id="paper_core",
        symbol="ETHUSDT",
        asset_class="crypto",
        listed_at=1_609_459_200_000,
    )
    assert record_membership(conn, **kwargs) == 1
    assert record_membership(conn, **kwargs) == 0
    assert conn.execute("SELECT COUNT(*) FROM universe_membership").fetchone()[0] == 1
    conn.close()


def test_seed_paper_core_lists_btc_and_eth_from_2021(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    n = seed_paper_core(conn)
    assert n >= 2

    symbols = universe_as_of(conn, PAPER_CORE, as_of_ms=1_700_000_000_000)
    assert symbols == ["BTCUSDT", "ETHUSDT"]
    conn.close()


def test_seed_paper_liquid_lists_btc_eth_sol_bnb(tmp_path: Path):
    from jarvise_ingest.universe import PAPER_LIQUID, seed_named_universe, seed_paper_liquid

    conn = open_db(tmp_path / "t.db")
    assert seed_paper_liquid(conn) >= 4
    assert seed_named_universe(conn, PAPER_LIQUID) == 0
    symbols = universe_as_of(conn, PAPER_LIQUID, as_of_ms=1_700_000_000_000)
    assert set(symbols) == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
    assert universe_as_of(conn, PAPER_CORE, as_of_ms=1_700_000_000_000) == []
    conn.close()


def test_universe_as_of_unknown_universe_is_empty(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "t.db")
    assert universe_as_of(conn, "nope", as_of_ms=1_700_000_000_000) == []
    conn.close()


def test_universe_add_remove_and_list_memberships(tmp_path: Path) -> None:
    from jarvise_ingest.db import (
        add_universe_symbol,
        list_all_known_symbols,
        list_universe_memberships,
        remove_universe_symbol,
    )

    conn = open_db(tmp_path / "t.db")
    assert add_universe_symbol(conn, universe_id="paper_liquid", symbol="ADAUSDT") is True
    assert add_universe_symbol(conn, universe_id="paper_liquid", symbol="XRPUSDT") is True

    items = list_universe_memberships(conn, universe_id="paper_liquid")
    symbols = [item["symbol"] for item in items]
    assert "ADAUSDT" in symbols
    assert "XRPUSDT" in symbols

    known = list_all_known_symbols(conn)
    assert "ADAUSDT" in known

    # Remove ADAUSDT: the interval is closed, not deleted (point-in-time history).
    assert remove_universe_symbol(conn, universe_id="paper_liquid", symbol="ADAUSDT") is True
    now_ms = int(time.time() * 1000) + 1
    assert "ADAUSDT" not in universe_as_of(conn, "paper_liquid", now_ms)
    assert "XRPUSDT" in universe_as_of(conn, "paper_liquid", now_ms)
    ada = [m for m in list_universe_memberships(conn, universe_id="paper_liquid") if m["symbol"] == "ADAUSDT"]
    assert len(ada) == 1 and ada[0]["delisted_at"] is not None
    conn.close()


def test_universe_remove_survives_reseed_and_keeps_history(tmp_path: Path) -> None:
    """Seeding on every read used to re-insert a removed preset symbol."""
    from jarvise_ingest.db import remove_universe_symbol

    conn = open_db(tmp_path / "t.db")
    seed_named_universe(conn, "paper_liquid")
    removed_at = 1_800_000_000_000
    assert remove_universe_symbol(conn, universe_id="paper_liquid", symbol="BNBUSDT", now_ms=removed_at)
    seed_named_universe(conn, "paper_liquid")  # what GET /api/universe does
    assert "BNBUSDT" not in universe_as_of(conn, "paper_liquid", removed_at + 1)
    # A replay before the removal still sees BNB as a member.
    assert "BNBUSDT" in universe_as_of(conn, "paper_liquid", removed_at - 1)
    conn.close()


def test_universe_readd_opens_new_interval_from_now(tmp_path: Path) -> None:
    from jarvise_ingest.db import add_universe_symbol, remove_universe_symbol

    conn = open_db(tmp_path / "t.db")
    seed_named_universe(conn, "paper_liquid")
    t_remove, t_readd = 1_800_000_000_000, 1_800_000_500_000
    remove_universe_symbol(conn, universe_id="paper_liquid", symbol="SOLUSDT", now_ms=t_remove)
    assert add_universe_symbol(conn, universe_id="paper_liquid", symbol="SOLUSDT", now_ms=t_readd)
    assert "SOLUSDT" not in universe_as_of(conn, "paper_liquid", (t_remove + t_readd) // 2)
    assert "SOLUSDT" in universe_as_of(conn, "paper_liquid", t_readd)
    # Adding an already-active symbol is a no-op.
    assert add_universe_symbol(conn, universe_id="paper_liquid", symbol="SOLUSDT", now_ms=t_readd + 10)
    rows = conn.execute(
        "SELECT COUNT(*) FROM universe_membership WHERE universe_id='paper_liquid' AND symbol='SOLUSDT'"
    ).fetchone()[0]
    assert rows == 2
    conn.close()


def test_reset_universe_members_closes_extras_and_reopens_preset(tmp_path: Path) -> None:
    from jarvise_ingest.db import add_universe_symbol, reset_universe_members
    from jarvise_ingest.universe import preset_members

    conn = open_db(tmp_path / "t.db")
    seed_named_universe(conn, "paper_liquid")
    add_universe_symbol(conn, universe_id="paper_liquid", symbol="ADAUSDT", now_ms=1_800_000_000_000)
    t1 = 1_800_000_100_000
    changes = reset_universe_members(
        conn, universe_id="paper_liquid", members=preset_members("paper_core"), now_ms=t1
    )
    assert sorted(changes["removed"]) == ["ADAUSDT", "BNBUSDT", "SOLUSDT"]
    assert changes["added"] == []
    assert universe_as_of(conn, "paper_liquid", t1) == ["BTCUSDT", "ETHUSDT"]

    t2 = t1 + 1000
    changes = reset_universe_members(
        conn, universe_id="paper_liquid", members=preset_members("paper_liquid"), now_ms=t2
    )
    assert sorted(changes["added"]) == ["BNBUSDT", "SOLUSDT"]
    assert universe_as_of(conn, "paper_liquid", t2) == ["BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]
    conn.close()
