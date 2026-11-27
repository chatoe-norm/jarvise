"""Owner dep/wd: Binance 8-dp transfer strings ('80.00000000') must confirm and book.

Regression for `p owner wd 80` -> preview OK, ✅ confirm -> bad_amount (nothing booked).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_partner_owner_tx_b1 import (
    HOUR,
    NOW,
    FakeClient,
    _init,
    _owner_cb,
    _owner_msg,
    _xfer,
)

from jarvise_assist import partner_admin as pa
from jarvise_assist import partner_store as ps
from jarvise_ingest.db import open_db

D = Decimal
ADMIN = "42"


@pytest.fixture
def env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)


def _admin_row(conn: Any) -> Any:
    admin = ps.admin_partner(conn)
    assert admin is not None
    return admin


def _owner_events(conn: Any) -> list[Any]:
    return conn.execute(
        "SELECT kind, cash_micro, units_delta_e8 FROM partner_event "
        "WHERE kind IN ('owner_deposit', 'owner_redeem')"
    ).fetchall()


def _run(conn: Any, client: FakeClient, text: str) -> tuple[dict[str, Any], str | None, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def send(t: str, **kwargs: Any) -> None:
        sent.append({"text": t, **kwargs})

    out = pa.handle_partner_text(conn, text, message=_owner_msg(), send=send, futures=client, now_ms=NOW)
    cb = None
    if out.get("ok") and out.get("token"):
        cb = sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    return out, cb, sent


def _confirm(conn: Any, client: FakeClient, cb: str, now_ms: int = NOW + 1000) -> dict[str, Any]:
    return pa.handle_partner_callback(
        conn,
        cb,
        callback=_owner_cb(cb),
        send=lambda *a, **k: None,
        answer=lambda *a, **k: None,
        edit=lambda *a, **k: True,
        futures=client,
        now_ms=now_ms,
    )


def test_normalize_owner_amount_only_drops_zero_digits() -> None:
    assert ps.normalize_owner_amount(D("80.00000000")) == D("80")
    assert ps.normalize_owner_amount(D("80.00000000")).as_tuple().exponent == -6
    assert ps.normalize_owner_amount(D("80.12345600")) == D("80.123456")
    # real sub-micro digits are kept, never rounded away
    assert ps.normalize_owner_amount(D("80.0000001")) == D("80.0000001")
    assert ps.normalize_owner_amount(D("80.00000010")).as_tuple().exponent == -8
    # already within 6 dp: untouched
    assert ps.normalize_owner_amount(D("80.5")).as_tuple().exponent == -1
    assert ps.owner_amount_refusal(D("80.00000000")) is None
    assert ps.owner_amount_refusal(D("80.0000001")) == {"ok": False, "reason": "bad_amount"}
    assert ps.owner_amount_refusal(D("0E-8")) == {"ok": False, "reason": "bad_amount"}
    assert ps.owner_amount_refusal(D("-80.00000000")) == {"ok": False, "reason": "bad_amount"}
    assert ps.owner_amount_refusal(D("NaN")) == {"ok": False, "reason": "bad_amount"}
    # too many digits for the decimal context to quantize: returned unchanged (then refused)
    huge = D("1" + "0" * 24 + ".00000000")
    assert ps.normalize_owner_amount(huge) == huge
    assert ps.normalize_owner_amount(huge).as_tuple().exponent == -8
    assert ps.owner_amount_refusal(huge) == {"ok": False, "reason": "bad_amount"}


def test_owner_amount_decimals_is_adjustable_setting() -> None:
    assert ps.OWNER_AMOUNT_DECIMALS == 6
    assert ps.normalize_owner_amount(D("80.0100"), decimals=2) == D("80.01")
    assert ps.owner_amount_refusal(D("80.011"), decimals=2) == {"ok": False, "reason": "bad_amount"}
    assert ps.owner_amount_refusal(D("80.010"), decimals=2) is None
    with pytest.raises(ValueError):
        ps.normalize_owner_amount(D("1"), decimals=7)
    with pytest.raises(ValueError):
        ps.owner_amount_refusal(D("1"), decimals=-1)


def test_owner_wd_8dp_binance_string_confirms_and_books(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "wd8.db")
    _init(conn, wallet="1000", now_ms=NOW - 10 * HOUR)
    before = _admin_row(conn)
    client = FakeClient(
        [_xfer(time_ms=NOW - HOUR, amount="-80.00000000", tran_id="435677118068")],
        wallet="920",
    )
    out, cb, _ = _run(conn, client, "p owner wd 80")
    assert out["ok"] is True and cb is not None
    res = _confirm(conn, client, cb)
    assert res.get("ok") is True, res
    assert res.get("reason") != "bad_amount"
    after = _admin_row(conn)
    # NAV 1.0 (1000 V, 1000 units): 80 USDT burns exactly 80 units.
    burned = D(int(before["units_e8"]) - int(after["units_e8"])) / ps.UNIT_SCALE
    assert burned == D("80")
    rows = _owner_events(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "owner_redeem"
    assert ps.verify_ledger(conn)["ok"]

    # retry the same confirm / re-type the command: no double booking
    again = _confirm(conn, client, cb, now_ms=NOW + 2000)
    assert again.get("ok") is not True or again.get("reason") == "already_recorded"
    out2, _, sent2 = _run(conn, client, "p owner wd 80 435677118068")
    assert out2.get("reason") in {"already_recorded", "no_match"}
    assert len(_owner_events(conn)) == 1
    assert int(_admin_row(conn)["units_e8"]) == int(after["units_e8"])


def test_owner_dep_8dp_binance_string_confirms_and_books(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep8.db")
    _init(conn, wallet="400", now_ms=NOW - 10 * HOUR)
    before = _admin_row(conn)
    client = FakeClient(
        [_xfer(time_ms=NOW - HOUR, amount="100.00000000", tran_id="dep8")],
        wallet="500",
    )
    out, cb, _ = _run(conn, client, "p owner dep 100")
    assert out["ok"] is True and cb is not None
    res = _confirm(conn, client, cb)
    assert res.get("ok") is True, res
    after = _admin_row(conn)
    minted = D(int(after["units_e8"]) - int(before["units_e8"])) / ps.UNIT_SCALE
    assert minted == D("100")
    assert len(_owner_events(conn)) == 1
    again = _confirm(conn, client, cb, now_ms=NOW + 2000)
    assert again.get("ok") is not True or again.get("reason") == "already_recorded"
    assert len(_owner_events(conn)) == 1
    assert ps.verify_ledger(conn)["ok"]


@pytest.mark.parametrize(
    "side,amount,wallet", [("wd", "-80.0000001", "919.9999999"), ("dep", "80.0000001", "480.0000001")]
)
def test_owner_sub_micro_refused_at_preview_and_never_books(
    tmp_path: Path, env_chat: None, side: str, amount: str, wallet: str
) -> None:
    conn = open_db(tmp_path / f"sub_{side}.db")
    _init(conn, wallet="1000" if side == "wd" else "400", now_ms=NOW - 10 * HOUR)
    before = _admin_row(conn)
    client = FakeClient([_xfer(time_ms=NOW - HOUR, amount=amount, tran_id=f"sub{side}")], wallet=wallet)
    out, cb, _ = _run(conn, client, f"p owner {side} 80 sub{side}")
    # parity: preview refuses exactly what confirm/booking would refuse
    assert out == {"ok": False, "reason": "bad_amount"}
    assert cb is None
    assert _owner_events(conn) == []
    assert int(_admin_row(conn)["units_e8"]) == int(before["units_e8"])


def test_store_sub_micro_still_bad_amount_and_8dp_ok(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "store.db")
    _init(conn, wallet="1000", now_ms=NOW - 10 * HOUR)
    bad = ps.apply_owner_redeem(conn, amount=D("10.0000001"), account_value=D("990"), now_ms=NOW)
    assert bad == {"ok": False, "reason": "bad_amount"}
    assert _owner_events(conn) == []
    ok = ps.apply_owner_redeem(
        conn, amount=D("10.00000000"), account_value=D("990"), now_ms=NOW, note="tranId s8"
    )
    assert ok["ok"] is True
    again = ps.apply_owner_redeem(
        conn, amount=D("10.00000000"), account_value=D("980"), now_ms=NOW + 1, note="tranId s8"
    )
    assert again.get("reason") == "already_recorded"
    assert len(_owner_events(conn)) == 1


def test_confirm_refuses_if_binance_amount_turns_sub_micro(tmp_path: Path, env_chat: None) -> None:
    """Preview on a clean amount, then the finding reads sub-micro at confirm: refuse, restore, no booking."""
    conn = open_db(tmp_path / "flip.db")
    _init(conn, wallet="1000", now_ms=NOW - 10 * HOUR)
    client = FakeClient([_xfer(time_ms=NOW - HOUR, amount="-80.00000000", tran_id="flip1")], wallet="920")
    out, cb, _ = _run(conn, client, "p owner wd 80")
    assert out["ok"] is True and cb is not None
    client.transfers = [_xfer(time_ms=NOW - HOUR, amount="-80.0000001", tran_id="flip1")]
    res = _confirm(conn, client, cb)
    assert res == {"ok": False, "reason": "bad_amount"}
    assert _owner_events(conn) == []
    # token was restored: a clean re-read confirms fine
    client.transfers = [_xfer(time_ms=NOW - HOUR, amount="-80.00000000", tran_id="flip1")]
    res2 = _confirm(conn, client, cb, now_ms=NOW + 2000)
    assert res2.get("ok") is True, res2
    assert len(_owner_events(conn)) == 1
