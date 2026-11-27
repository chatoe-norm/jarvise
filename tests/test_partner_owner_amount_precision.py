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


def _confirm(
    conn: Any,
    client: FakeClient,
    cb: str,
    now_ms: int = NOW + 1000,
    answers: list[str | None] | None = None,
) -> dict[str, Any]:
    def answer(_id: str, text: str | None = None) -> None:
        if answers is not None:
            answers.append(text)

    return pa.handle_partner_callback(
        conn,
        cb,
        callback=_owner_cb(cb),
        send=lambda *a, **k: None,
        answer=answer,
        edit=lambda *a, **k: True,
        futures=client,
        now_ms=now_ms,
    )


BAD = {"ok": False, "reason": "bad_amount"}


def test_normalize_owner_amount_only_drops_zero_digits() -> None:
    n = ps.normalize_owner_amount
    assert n(D("80.00000000")) == D("80")
    assert n(D("80.00000000")).as_tuple().exponent == -6
    assert n(D("80.12345600")).as_tuple() == D("80.123456").as_tuple()
    # real sub-micro digits are kept, never rounded away
    assert n(D("80.0000001")).as_tuple() == D("80.0000001").as_tuple()
    assert n(D("80.00000010")).as_tuple().exponent == -8
    # already within 6 dp / exponent forms: untouched
    assert n(D("80.5")).as_tuple().exponent == -1
    assert n(D("8E+1")).as_tuple() == D("8E+1").as_tuple()
    assert n(D("8.0E+1")).as_tuple() == D("8.0E+1").as_tuple()
    # too many digits for the decimal context to quantize: returned unchanged
    huge = D("1" + "0" * 24 + ".00000000")
    assert n(huge).as_tuple() == huge.as_tuple()


def test_owner_amount_refusal_edges() -> None:
    r = ps.owner_amount_refusal
    assert r(D("80.00000000")) is None
    assert r(D("80.12345600")) is None
    assert r(D("8E+1")) is None
    assert r(D("8.0E+1")) is None
    for bad in (
        "80.0000001",
        "80.0000009",
        "0.0000001",
        "0E-8",
        "-0.00000000",
        "-80.00000000",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "1" + "0" * 24 + ".00000000",
        "1" + "0" * 24 + ".000000",
        "1E+24",
        "1E+999999",
    ):
        assert r(D(bad)) == BAD, bad


def test_owner_amount_decimals_is_adjustable_setting(monkeypatch: Any) -> None:
    assert ps.OWNER_AMOUNT_DECIMALS == 6
    assert ps.normalize_owner_amount(D("80.0100"), decimals=2).as_tuple().exponent == -2
    assert ps.owner_amount_refusal(D("80.011"), decimals=2) == BAD
    assert ps.owner_amount_refusal(D("80.010"), decimals=2) is None
    assert ps.owner_amount_refusal(D("80.000"), decimals=0) is None
    assert ps.owner_amount_refusal(D("80.5"), decimals=0) == BAD
    for bad_d in (7, -1, True, 2.5):
        with pytest.raises(ValueError):
            ps.normalize_owner_amount(D("1"), decimals=bad_d)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ps.owner_amount_refusal(D("1"), decimals=bad_d)  # type: ignore[arg-type]
    # module setting is the default
    monkeypatch.setattr(ps, "OWNER_AMOUNT_DECIMALS", 2)
    assert ps.owner_amount_refusal(D("80.011")) == BAD
    assert ps.normalize_owner_amount(D("80.0100")).as_tuple().exponent == -2


@pytest.mark.parametrize(
    "amount,typed,cash_micro",
    [("-80.00000000", "80", -80_000_000), ("-80.12345600", "80.123456", -80_123_456)],
)
def test_owner_wd_8dp_binance_string_confirms_and_books(
    tmp_path: Path, env_chat: None, amount: str, typed: str, cash_micro: int
) -> None:
    conn = open_db(tmp_path / "wd8.db")
    try:
        _init(conn, wallet="1000", now_ms=NOW - 10 * HOUR)
        before = _admin_row(conn)
        wallet = str(D("1000") + D(amount))
        client = FakeClient([_xfer(time_ms=NOW - HOUR, amount=amount, tran_id="435677118068")], wallet=wallet)
        out, cb, _ = _run(conn, client, f"p owner wd {typed}")
        assert out["ok"] is True and cb is not None
        res = _confirm(conn, client, cb)
        assert res["ok"] is True, res
        assert res.get("reason") is None
        after = _admin_row(conn)
        # NAV 1.0 (1000 V, 1000 units): X USDT burns exactly X units.
        burned = D(int(before["units_e8"]) - int(after["units_e8"])) / ps.UNIT_SCALE
        assert burned == -D(cash_micro) / ps.MICRO_SCALE
        rows = _owner_events(conn)
        assert len(rows) == 1
        assert rows[0]["kind"] == "owner_redeem"
        assert abs(int(rows[0]["cash_micro"])) == abs(cash_micro)
        assert int(rows[0]["units_delta_e8"]) == int(after["units_e8"]) - int(before["units_e8"])
        assert ps.verify_ledger(conn)["ok"]

        # retry the same confirm / re-type the command: no double booking
        again = _confirm(conn, client, cb, now_ms=NOW + 2000)
        assert again["ok"] is True and again["reason"] == "already_recorded", again
        out2, _, _ = _run(conn, client, f"p owner wd {typed} 435677118068")
        assert out2["reason"] == "already_recorded", out2
        assert len(_owner_events(conn)) == 1
        assert int(_admin_row(conn)["units_e8"]) == int(after["units_e8"])
    finally:
        conn.close()


def test_owner_dep_8dp_binance_string_confirms_and_books(tmp_path: Path, env_chat: None) -> None:
    conn = open_db(tmp_path / "dep8.db")
    try:
        _init(conn, wallet="400", now_ms=NOW - 10 * HOUR)
        before = _admin_row(conn)
        client = FakeClient([_xfer(time_ms=NOW - HOUR, amount="100.00000000", tran_id="dep8")], wallet="500")
        out, cb, _ = _run(conn, client, "p owner dep 100")
        assert out["ok"] is True and cb is not None
        res = _confirm(conn, client, cb)
        assert res["ok"] is True, res
        after = _admin_row(conn)
        minted = D(int(after["units_e8"]) - int(before["units_e8"])) / ps.UNIT_SCALE
        assert minted == D("100")
        rows = _owner_events(conn)
        assert len(rows) == 1
        assert rows[0]["kind"] == "owner_deposit"
        assert abs(int(rows[0]["cash_micro"])) == 100_000_000
        again = _confirm(conn, client, cb, now_ms=NOW + 2000)
        assert again["ok"] is True and again["reason"] == "already_recorded", again
        out2, _, _ = _run(conn, client, "p owner dep 100 dep8")
        assert out2["reason"] == "already_recorded", out2
        assert len(_owner_events(conn)) == 1
        assert ps.verify_ledger(conn)["ok"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "side,amount,wallet",
    [("wd", "-80.0000001", "919.9999999"), ("dep", "80.0000001", "480.0000001")],
)
def test_owner_sub_micro_refused_at_preview_and_never_books(
    tmp_path: Path, env_chat: None, side: str, amount: str, wallet: str
) -> None:
    conn = open_db(tmp_path / f"sub_{side}.db")
    try:
        _init(conn, wallet="1000" if side == "wd" else "400", now_ms=NOW - 10 * HOUR)
        before = _admin_row(conn)
        client = FakeClient([_xfer(time_ms=NOW - HOUR, amount=amount, tran_id=f"sub{side}")], wallet=wallet)
        out, cb, sent = _run(conn, client, f"p owner {side} 80 sub{side}")
        # parity: preview refuses exactly what confirm/booking would refuse
        assert out == BAD
        assert cb is None
        assert sent[-1]["text"] == pa.thai_reason("bad_amount")
        assert "bad_amount" not in sent[-1]["text"]
        assert _owner_events(conn) == []
        assert int(_admin_row(conn)["units_e8"]) == int(before["units_e8"])
    finally:
        conn.close()


def test_store_sub_micro_and_huge_refused_and_8dp_ok(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "store.db")
    try:
        _init(conn, wallet="1000", now_ms=NOW - 10 * HOUR)
        for bad in ("10.0000001", "1E+24", "1E+999999"):
            assert ps.apply_owner_redeem(conn, amount=D(bad), account_value=D("990"), now_ms=NOW) == BAD
            assert ps.apply_owner_deposit(conn, amount=D(bad), account_value=D("990"), now_ms=NOW) == BAD
        assert _owner_events(conn) == []
        ok = ps.apply_owner_redeem(
            conn, amount=D("10.00000000"), account_value=D("990"), now_ms=NOW, note="tranId s8"
        )
        assert ok["ok"] is True
        again = ps.apply_owner_redeem(
            conn, amount=D("10.00000000"), account_value=D("980"), now_ms=NOW + 1, note="tranId s8"
        )
        assert again["reason"] == "already_recorded"
        assert len(_owner_events(conn)) == 1
    finally:
        conn.close()


@pytest.mark.parametrize("flip", ["-80.0000001", "-80.0000006"])
def test_confirm_refuses_if_binance_amount_turns_sub_micro(tmp_path: Path, env_chat: None, flip: str) -> None:
    """Preview on a clean amount, then the finding reads sub-micro at confirm: bad_amount (not
    amount_mismatch, even when it rounds to a different micro), restore, no booking."""
    conn = open_db(tmp_path / "flip.db")
    try:
        _init(conn, wallet="1000", now_ms=NOW - 10 * HOUR)
        client = FakeClient([_xfer(time_ms=NOW - HOUR, amount="-80.00000000", tran_id="flip1")], wallet="920")
        out, cb, _ = _run(conn, client, "p owner wd 80")
        assert out["ok"] is True and cb is not None
        client.transfers = [_xfer(time_ms=NOW - HOUR, amount=flip, tran_id="flip1")]
        answers: list[str | None] = []
        res = _confirm(conn, client, cb, answers=answers)
        assert res == BAD
        assert answers[-1] == pa.thai_reason("bad_amount")
        assert _owner_events(conn) == []
        # token was restored: a clean re-read confirms fine
        client.transfers = [_xfer(time_ms=NOW - HOUR, amount="-80.00000000", tran_id="flip1")]
        res2 = _confirm(conn, client, cb, now_ms=NOW + 2000)
        assert res2["ok"] is True, res2
        assert len(_owner_events(conn)) == 1
    finally:
        conn.close()
