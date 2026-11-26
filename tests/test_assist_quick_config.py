"""Quick Plan A0: registry, parse, clamps, advice, migration 39."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist.quick import get_quick_settings, write_quick_settings
from jarvise_assist.quick_config import (
    QUICK_CONFIG,
    Advice,
    canonical_string,
    effective_quick_values,
    get_quick_config,
    needs_confirm,
    parse_value,
    quick_config_active_warnings,
    quick_config_advice,
    quick_config_history,
    reset_quick_config,
    set_quick_config,
)
from jarvise_ingest.db import SCHEMA_VERSION, migrate, open_db, schema_version


def _defaults() -> dict[str, Any]:
    return {e.key: e.default for e in QUICK_CONFIG}


def test_registry_defaults_within_bounds() -> None:
    for e in QUICK_CONFIG:
        v, err = parse_value(e.key, canonical_string(e.key, e.default))
        assert err is None, e.key
        assert v is not None


@pytest.mark.parametrize(
    ("key", "ok", "bad"),
    [
        ("quick_risk_pct", "0.05", "0.04"),
        ("quick_risk_pct", "2.0", "2.01"),
        ("quick_risk_cap_pct", "5.0", "5.01"),
        ("quick_risk_cap_pct", "0.1", "0.09"),
        ("quick_max_leverage", "5", "6"),
        ("quick_max_leverage", "1", "0"),
        ("quick_chase_r", "0.05", "0.04"),
        ("quick_chase_r", "1.0", "1.01"),
    ],
)
def test_parse_bound_edges(key: str, ok: str, bad: str) -> None:
    v, err = parse_value(key, ok)
    assert err is None and v is not None
    v2, err2 = parse_value(key, bad)
    assert v2 is None and err2 is not None and "ขอบเขต" in err2


def test_parse_presets_and_night() -> None:
    v, err = parse_value("quick_stop_presets", "0.3,0.5,1")
    assert err is None and v == (Decimal("0.3"), Decimal("0.5"), Decimal("1"))
    _, err = parse_value("quick_stop_presets", "0.3,0.5,1,1.5,2")
    assert err is not None
    _, err = parse_value("quick_stop_presets", "1,0.5")
    assert err is not None
    v, err = parse_value("quick_night_start", "23:30")
    assert v == "23:30" and err is None


def test_night_window_on_write(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "night.db")
    set_quick_config(conn, "quick_night_start", "20:00", source="t", now_ms=1)
    set_quick_config(conn, "quick_night_end", "08:00", source="t", now_ms=2)  # 12h
    with pytest.raises(ValueError, match="12"):
        set_quick_config(conn, "quick_night_end", "08:30", source="t", now_ms=3)
    with pytest.raises(ValueError, match="เท่า"):
        set_quick_config(conn, "quick_night_end", "20:00", source="t", now_ms=4)
    conn.close()


def test_migration_39_fresh_and_38_upgrade(tmp_path: Path) -> None:
    fresh = open_db(tmp_path / "fresh39.db")
    assert SCHEMA_VERSION == 39
    assert schema_version(fresh) == 39
    for table in ("assist_quick_config", "assist_quick_config_log"):
        assert fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    cols = {r[1] for r in fresh.execute("PRAGMA table_info(assist_settings)").fetchall()}
    assert "awaiting_quick_input" in cols and "awaiting_quick_until_ms" in cols
    reg_cols = {r[1] for r in fresh.execute("PRAGMA table_info(assist_quick_registration)").fetchall()}
    assert "exec_intent_id" in reg_cols
    idx = fresh.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_assist_quick_reg_exec_intent'"
    ).fetchone()
    assert idx
    fresh.close()

    path = tmp_path / "from38.db"
    conn = open_db(path)
    write_quick_settings(conn, risk_pct=1.0, cap_pct=1.5)
    # Drop A0 objects and rewind version to simulate pre-39
    conn.execute("DROP TABLE IF EXISTS assist_quick_config")
    conn.execute("DROP TABLE IF EXISTS assist_quick_config_log")
    # cannot easily drop columns; rewind version only — repair will re-add missing
    conn.execute("PRAGMA user_version = 38")
    conn.commit()
    # Remove config tables only; columns may already exist from 39 create — drop DB columns
    # by recreating from a true 38 snapshot:
    conn.close()

    # Build a true schema-38 DB: open, then strip migration 39 artifacts
    path2 = tmp_path / "real38.db"
    c38 = open_db(path2)
    write_quick_settings(c38, risk_pct=1.0, cap_pct=1.5)
    c38.execute("DROP TABLE IF EXISTS assist_quick_config")
    c38.execute("DROP TABLE IF EXISTS assist_quick_config_log")
    c38.execute("DROP INDEX IF EXISTS idx_assist_quick_reg_exec_intent")
    c38.execute("DROP INDEX IF EXISTS idx_assist_quick_config_log_at")
    # SQLite cannot DROP COLUMN on older; leave columns if present — repair is idempotent
    c38.execute("PRAGMA user_version = 38")
    c38.commit()
    c38.close()

    upgraded = open_db(path2)
    assert schema_version(upgraded) == 39
    qs = get_quick_settings(upgraded)
    assert qs["risk_pct"] == 1.0
    assert qs["cap_pct"] == 1.5
    assert upgraded.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assist_quick_config'"
    ).fetchone()
    # re-run migrate writes nothing material
    assert migrate(upgraded) == []
    assert upgraded.execute("SELECT COUNT(*) FROM assist_quick_config").fetchone()[0] == 0
    upgraded.close()


def test_get_quick_config_clamp_and_no_write(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "clamp.db")
    write_quick_settings(conn, risk_pct=3.0, cap_pct=1.0)
    conn.execute(
        "INSERT INTO assist_quick_config (key, value, updated_at_ms) VALUES ('quick_chase_r', 'nope', 1)"
    )
    conn.commit()
    before = conn.total_changes
    view = get_quick_config(conn)
    assert conn.total_changes == before
    assert view.values["quick_risk_pct"] == Decimal("2.0")
    assert "quick_risk_pct" in view.clamped_keys
    assert view.values["quick_chase_r"] == Decimal("0.3")
    assert "quick_chase_r" in view.defaulted_keys
    conn.close()


def test_kv_clamp_on_read_not_default(tmp_path: Path) -> None:
    """OOB-but-parseable KV values clamp to nearest bound; junk → default (#4)."""

    def _seed(name: str, rows: list[tuple[str, str]]) -> Any:
        c = open_db(tmp_path / f"{name}.db")
        for key, raw in rows:
            c.execute(
                "INSERT INTO assist_quick_config (key, value, updated_at_ms) VALUES (?, ?, 1)",
                (key, raw),
            )
        c.commit()
        before = c.total_changes
        v = get_quick_config(c)
        assert c.total_changes == before
        c.close()
        return v

    v0 = _seed(
        "lev0",
        [
            ("quick_max_leverage", "0"),
            ("quick_chase_r", "3"),
            ("quick_stop_buffer_pct", "9"),
        ],
    )
    assert v0.values["quick_max_leverage"] == 1
    assert "quick_max_leverage" in v0.clamped_keys
    assert v0.values["quick_chase_r"] == Decimal("1.0")
    assert "quick_chase_r" in v0.clamped_keys
    assert v0.values["quick_stop_buffer_pct"] == Decimal("1.0")
    assert "quick_stop_buffer_pct" in v0.clamped_keys

    v9 = _seed("lev9", [("quick_max_leverage", "9")])
    assert v9.values["quick_max_leverage"] == 5
    assert "quick_max_leverage" in v9.clamped_keys

    vj = _seed("junk", [("quick_chase_r", "nope")])
    assert vj.values["quick_chase_r"] == Decimal("0.3")
    assert "quick_chase_r" in vj.defaulted_keys


def test_lenient_read_accepts_signed_then_clamps(tmp_path: Path) -> None:
    """Read path: signed numeric KV clamps; junk/non-finite → default; write stays strict."""
    conn = open_db(tmp_path / "signed.db")
    for key, raw in (
        ("quick_max_leverage", "-1"),
        ("quick_stop_buffer_pct", "-0.5"),
        ("quick_chase_r", "+9"),
    ):
        conn.execute(
            "INSERT INTO assist_quick_config (key, value, updated_at_ms) VALUES (?, ?, 1)",
            (key, raw),
        )
    conn.commit()
    before = conn.total_changes
    view = get_quick_config(conn)
    assert conn.total_changes == before  # no DB writes on read
    assert view.values["quick_max_leverage"] == 1
    assert "quick_max_leverage" in view.clamped_keys
    assert view.values["quick_stop_buffer_pct"] == Decimal("0.02")  # hard_min
    assert "quick_stop_buffer_pct" in view.clamped_keys
    assert view.values["quick_chase_r"] == Decimal("1.0")  # hard_max (from +9)
    assert "quick_chase_r" in view.clamped_keys

    for junk in ("nan", "inf", "1e3", ""):
        v, err = parse_value("quick_max_leverage", junk, check_bounds=False)
        assert v is None and err is not None

    # Write path rejects signed input
    v, err = parse_value("quick_max_leverage", "-1", check_bounds=True)
    assert v is None and err is not None
    with pytest.raises(ValueError):
        set_quick_config(conn, "quick_max_leverage", "-1", source="t", now_ms=2)
    conn.close()


def test_get_quick_config_empty_row_no_insert(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "empty.db")
    conn.execute("DELETE FROM assist_quick_settings")
    conn.commit()
    before = conn.total_changes
    view = get_quick_config(conn)
    assert conn.total_changes == before
    assert view.values["quick_risk_pct"] == Decimal("0.5")
    assert view.values["quick_risk_cap_pct"] == Decimal("1.0")
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_settings").fetchone()[0] == 0
    conn.close()


def test_effective_clamps() -> None:
    cfg = _defaults()
    cfg["quick_risk_pct"] = Decimal("1.5")
    cfg["quick_risk_cap_pct"] = Decimal("1.0")
    cfg["quick_counter_risk_pct"] = Decimal("0.8")
    cfg["quick_max_leverage"] = 5
    eff = effective_quick_values(cfg)
    assert eff["risk_used"] == Decimal("1.0")
    assert eff["counter_used"] == Decimal("0.8")  # min(0.8, 1.0)
    cfg["quick_counter_risk_pct"] = Decimal("1.2")
    eff = effective_quick_values(cfg)
    assert eff["counter_used"] == Decimal("1.0")


def test_effective_lev_used_caps_above_5() -> None:
    """lev_used ≤ 5 even when dict input has quick_max_leverage > 5 (#7c)."""
    cfg = _defaults()
    cfg["quick_max_leverage"] = 9
    eff = effective_quick_values(cfg)
    assert eff["lev_used"] == 5


def test_needs_confirm_directions() -> None:
    assert needs_confirm("quick_risk_pct", Decimal("0.5"), Decimal("0.75"))
    assert not needs_confirm("quick_risk_pct", Decimal("0.75"), Decimal("0.5"))
    assert needs_confirm("quick_stop_buffer_pct", Decimal("0.1"), Decimal("0.05"))
    assert not needs_confirm("quick_stop_buffer_pct", Decimal("0.05"), Decimal("0.1"))
    assert needs_confirm("quick_exec_universe", "core", "any")
    assert not needs_confirm("quick_exec_universe", "any", "core")
    assert not needs_confirm("quick_tp1_r", Decimal("1"), Decimal("2"))


def test_set_logs_and_history(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "log.db")
    set_quick_config(conn, "quick_counter_risk_pct", "0.3", source="t", now_ms=10)
    rows = quick_config_history(conn, limit=10)
    assert len(rows) == 1
    assert rows[0]["key"] == "quick_counter_risk_pct"
    assert rows[0]["old_value"] == "0.25"
    assert rows[0]["new_value"] == "0.3"
    # rejected / no-op
    set_quick_config(conn, "quick_counter_risk_pct", "0.3", source="t", now_ms=11)
    assert len(quick_config_history(conn)) == 1
    with pytest.raises(ValueError):
        set_quick_config(conn, "quick_counter_risk_pct", "9", source="t", now_ms=12)
    assert len(quick_config_history(conn)) == 1
    conn.close()


def test_reset_key_and_all(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "reset.db")
    set_quick_config(conn, "quick_counter_risk_pct", "0.5", source="t", now_ms=1)
    set_quick_config(conn, "quick_risk_pct", "0.75", source="t", now_ms=2)
    changes = reset_quick_config(conn, "counter", source="reset", now_ms=3)
    assert any(c[0] == "quick_counter_risk_pct" for c in changes)
    assert get_quick_config(conn).values["quick_counter_risk_pct"] == Decimal("0.25")
    reset_quick_config(conn, "all", source="reset_all", now_ms=4)
    assert get_quick_config(conn).values["quick_risk_pct"] == Decimal("0.5")
    conn.close()


def test_reset_all_validates_final_night_pair(tmp_path: Path) -> None:
    """01:00–12:00 → reset-all must not fail on intermediate 23:30–12:00 (#5)."""
    conn = open_db(tmp_path / "night_reset.db")
    set_quick_config(conn, "quick_night_start", "01:00", source="t", now_ms=1)
    set_quick_config(conn, "quick_night_end", "12:00", source="t", now_ms=2)
    changes = reset_quick_config(conn, "all", source="reset_all", now_ms=3)
    assert any(c[0] == "quick_night_start" for c in changes)
    view = get_quick_config(conn)
    assert view.values["quick_night_start"] == "23:30"
    assert view.values["quick_night_end"] == "07:30"
    conn.close()


def _advice_case(
    *,
    change: dict[str, Any],
    main: Decimal = Decimal("1"),
    expect_substr: str | None = None,
    expect_none: bool = False,
    fix: tuple[str, Any] | None = None,
) -> None:
    before = _defaults()
    after = dict(before)
    after.update(change)
    # also put pre-change values into before for keys we change
    adv = quick_config_advice(before, after, main_risk_pct=main)
    if expect_none:
        assert adv == []
        return
    assert any(expect_substr in a.text_th for a in adv), (expect_substr, [a.text_th for a in adv])
    if fix is not None:
        assert any(a.fix == fix for a in adv)


def test_advice_rules() -> None:
    # 1 risk > cap
    _advice_case(
        change={"quick_risk_pct": Decimal("1.5")},
        expect_substr="cap จำกัด",
        fix=("quick_risk_cap_pct", Decimal("3.00")),
    )
    # 2 risk < counter
    _advice_case(
        change={"quick_risk_pct": Decimal("0.1")},
        expect_substr="สวนเทรนด์",
        fix=("quick_counter_risk_pct", Decimal("0.05")),
    )
    # 3 main+risk ≥ 0.8*warn
    _advice_case(
        change={"quick_risk_pct": Decimal("2.0")},
        main=Decimal("3"),
        expect_substr="ใกล้ถึงระดับเตือน",
    )
    # 4 cap/risk < 2
    _advice_case(
        change={"quick_risk_pct": Decimal("0.8"), "quick_risk_cap_pct": Decimal("1.0")},
        expect_substr="ไม่ถึง 2 ไม้",
    )
    # 5 cap < risk
    _advice_case(
        change={"quick_risk_cap_pct": Decimal("0.2")},
        expect_substr="ต่ำกว่า risk",
        fix=("quick_risk_cap_pct", Decimal("0.5")),
    )
    # 6 portfolio + cap > warn
    _advice_case(
        change={"quick_combined_risk_warn_pct": Decimal("2.0"), "quick_risk_cap_pct": Decimal("1.0")},
        main=Decimal("1"),  # port=2
        expect_substr="กรณีแย่สุด",
    )
    # 7 counter ≥ risk
    _advice_case(
        change={"quick_counter_risk_pct": Decimal("0.5")},
        expect_substr="ไม่เล็กกว่า risk",
        fix=("quick_counter_risk_pct", Decimal("0.25")),
    )
    # 8 counter > cap
    _advice_case(
        change={"quick_counter_risk_pct": Decimal("1.5"), "quick_risk_cap_pct": Decimal("1.0")},
        expect_substr="สูงกว่า cap",
    )
    # 9 tp1pct
    _advice_case(change={"quick_tp1_close_pct": Decimal("0")}, expect_substr="ไม่มี TP1")
    _advice_case(change={"quick_tp1_close_pct": Decimal("100")}, expect_substr="ไม่เหลือไม้")
    _advice_case(change={"quick_tp1_close_pct": Decimal("10")}, expect_substr="minNotional")
    # 10 tp1r
    _advice_case(change={"quick_tp1_r": Decimal("0.4")}, expect_substr="fee/noise")
    _advice_case(change={"quick_tp1_r": Decimal("3")}, expect_substr="2R")
    # 11 chase ≥ tp1r/2
    _advice_case(
        change={"quick_chase_r": Decimal("0.6"), "quick_tp1_r": Decimal("1.0")},
        expect_substr="chase กินพื้นที่",
    )
    # 12 warn
    _advice_case(
        change={"quick_combined_risk_warn_pct": Decimal("1.0")},
        main=Decimal("1"),
        expect_substr="ก็เตือนแล้ว",
    )
    _advice_case(
        change={"quick_combined_risk_warn_pct": Decimal("20")},
        main=Decimal("1"),
        expect_substr="ไม่มีวันเตือน",
    )
    # 13 night
    _advice_case(
        change={"quick_night_start": "01:00", "quick_night_end": "03:00"},
        expect_substr="ช่วงคืนสั้น",
    )
    _advice_case(
        change={"quick_auto_be_time": "22:00"},
        expect_substr="autobe",
        fix=("quick_auto_be_time", "night"),
    )
    # 14–18
    _advice_case(change={"quick_confirm_ttl_s": 90}, expect_substr="TTL")
    _advice_case(change={"quick_chase_r": Decimal("0.6")}, expect_substr="chase สูง")
    _advice_case(change={"quick_max_fee_r": Decimal("0.6")}, expect_substr="fee กิน")
    _advice_case(change={"quick_max_leverage": 2}, expect_substr="เลเวอเรจต่ำ")
    _advice_case(change={"quick_exec_universe": "any"}, expect_substr="universe=any")
    # not triggered when unrelated key changes
    before = _defaults()
    after = dict(before)
    after["quick_fee_bps_per_side"] = Decimal("8")
    adv = quick_config_advice(before, after, main_risk_pct=Decimal("1"))
    assert not any("universe=any" in a.text_th for a in adv)
    # active warnings include current issues
    bad = _defaults()
    bad["quick_risk_pct"] = Decimal("1.5")
    active = quick_config_active_warnings(bad, main_risk_pct=Decimal("1"))
    assert any(isinstance(a, Advice) and "cap จำกัด" in a.text_th for a in active)


def test_advice_only_change_relevant() -> None:
    before = _defaults()
    after = dict(before)
    after["quick_confirm_ttl_s"] = 90
    adv = quick_config_advice(before, after, main_risk_pct=Decimal("1"))
    assert len(adv) == 1
    assert "TTL" in adv[0].text_th
