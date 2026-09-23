"""Point-in-time universe membership.

Selecting today's top coins and backtesting them over five years is survivorship
bias. Membership is versioned by listed_at / delisted_at so a walk-forward can
ask which symbols were eligible at a decision time.
"""

from pathlib import Path

from jarvise_ingest.db import open_db, record_membership, universe_as_of
from jarvise_ingest.universe import PAPER_CORE, seed_paper_core


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
    assert (
        conn.execute("SELECT COUNT(*) FROM universe_membership").fetchone()[0] == 1
    )
    conn.close()


def test_seed_paper_core_lists_btc_and_eth_from_2021(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    n = seed_paper_core(conn)
    assert n >= 2

    symbols = universe_as_of(conn, PAPER_CORE, as_of_ms=1_700_000_000_000)
    assert symbols == ["BTCUSDT", "ETHUSDT"]
    conn.close()


def test_universe_as_of_unknown_universe_is_empty(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    assert universe_as_of(conn, "nope", as_of_ms=1_700_000_000_000) == []
    conn.close()
