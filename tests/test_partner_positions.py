"""Partner read-only open positions view (pp:pos)."""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jarvise_assist import partner_portal as portal
from jarvise_assist import partner_positions as ppos
from jarvise_assist import partner_store as ps
from jarvise_assist.futures import FuturesPosition, SymbolFilters
from jarvise_assist.listen import process_update
from jarvise_assist.partner_value import AccountValueError
from jarvise_ingest.db import open_db

D = Decimal
NOW = 1_700_000_000_000
ADMIN = "42"
PARTNER_TG = "501"
OTHER_TG = "502"


@pytest.fixture(autouse=True)
def _env_chat(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ADMIN)
    ppos.reset_positions_cache()
    portal.reset_value_cache()


class FakePosClient:
    """Minimal futures client: only position_risk and symbol_filters."""

    def __init__(
        self,
        positions: list[FuturesPosition] | None = None,
        *,
        tick: Decimal = D("0.1"),
        risk_error: BaseException | None = None,
        filters_error: bool = False,
        ticks: dict[str, Decimal] | None = None,
    ) -> None:
        self.positions = list(positions or [])
        self.tick = tick
        self.ticks = ticks or {}
        self.risk_error = risk_error
        self.filters_error = filters_error
        self.risk_calls = 0

    def position_risk(self, *, symbol: str | None = None) -> list[FuturesPosition]:
        self.risk_calls += 1
        if self.risk_error is not None:
            raise self.risk_error
        if symbol is None:
            return list(self.positions)
        return [p for p in self.positions if p.symbol == symbol.upper()]

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        if self.filters_error:
            raise RuntimeError("filters unavailable")
        tick = self.ticks.get(symbol.upper(), self.tick)
        return SymbolFilters(
            step_size=D("0.001"),
            min_qty=D("0"),
            min_notional=D("5"),
            tick_size=tick,
        )

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected client attribute: {name}")


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> int:
        mid = len(sent) + 1
        entry: dict[str, Any] = {"text": text, "reply_markup": reply_markup, "message_id": mid}
        if chat_id is not None:
            entry["chat_id"] = chat_id
        sent.append(entry)
        return mid

    return sent, send


def _edit_capture() -> tuple[list[dict[str, Any]], Any]:
    edited: list[dict[str, Any]] = []

    def edit(
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        **kw: Any,
    ) -> bool:
        entry: dict[str, Any] = {
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        if chat_id is not None:
            entry["chat_id"] = chat_id
        edited.append(entry)
        return True

    return edited, edit


def _answer_capture() -> tuple[list[Any], Any]:
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    return answers, answer


def _cb(
    data: str,
    *,
    chat: int = 501,
    from_id: int | None = None,
    chat_type: str = "private",
    mid: int = 7,
    text: str = "card",
) -> dict[str, Any]:
    fid = from_id if from_id is not None else chat
    return {
        "id": "cq1",
        "data": data,
        "from": {"id": fid},
        "message": {
            "message_id": mid,
            "chat": {"id": chat, "type": chat_type},
            "text": text,
        },
    }


def _bind_partner(conn: Any, name: str, tg: str, *, now_ms: int) -> str:
    a = ps.create_partner(conn, name="admin", is_admin=True, now_ms=now_ms)
    b = ps.create_partner(conn, name=name, is_admin=False, now_ms=now_ms + 1)
    ps.apply_deposit(conn, a["partner_id"], D("1000"), D("1000"), now_ms + 2)
    inv = ps.create_invite(conn, b["partner_id"], now_ms=now_ms + 3)
    assert inv["ok"]
    out = ps.bind_invite(conn, inv["code"], tg, admin_chat_id=ADMIN, now_ms=now_ms + 4)
    assert out["ok"]
    return str(b["partner_id"])


def _bind_second(conn: Any, name: str, tg: str, *, now_ms: int) -> str:
    b = ps.create_partner(conn, name=name, is_admin=False, now_ms=now_ms)
    inv = ps.create_invite(conn, b["partner_id"], now_ms=now_ms + 1)
    assert inv["ok"]
    out = ps.bind_invite(conn, inv["code"], tg, admin_chat_id=ADMIN, now_ms=now_ms + 2)
    assert out["ok"]
    return str(b["partner_id"])


def _pos(
    symbol: str = "BTCUSDT",
    *,
    side: str = "LONG",
    amt: str = "0.01",
    entry: str = "91234.5",
    mark: str = "92010.0",
    liq: str | None = None,
) -> FuturesPosition:
    amt_d = D(amt)
    if side.upper() == "SHORT" and amt_d > 0:
        amt_d = -amt_d
    liq_d = D(liq) if liq is not None else None
    return FuturesPosition(
        symbol=symbol.upper(),
        position_side=side.upper() if side.upper() in ("LONG", "SHORT") else "BOTH",
        position_amt=amt_d,
        entry_price=D(entry),
        mark_price=D(mark),
        update_time_ms=NOW,
        liquidation_price=liq_d,
    )


def _insert_watch(
    conn: Any,
    *,
    symbol: str = "BTCUSDT",
    position_side: str = "LONG",
    side: str = "long",
    entry: float = 91234.5,
    entry_time_ms: int | None = NOW - 3_600_000,
    initial_stop: float | None = 90500.0,
    closed_at_ms: int | None = None,
    qty: float = 0.01,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_watch_position (
            symbol, position_side, side, entry, qty, in_universe, signal_id,
            initial_stop, initial_stop_source, entry_time_ms, no_stop_latched,
            last_seen_ms, closed_at_ms
        ) VALUES (?, ?, ?, ?, ?, 1, NULL, ?, 'fixed', ?, 0, ?, ?)
        """,
        (
            symbol,
            position_side,
            side,
            entry,
            qty,
            initial_stop,
            entry_time_ms,
            NOW,
            closed_at_ms,
        ),
    )
    conn.commit()


def _run_pos(
    conn: Any,
    client: Any,
    *,
    chat: int = 501,
    now_ms: int = NOW + 100,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
    edited, edit = _edit_capture()
    sent, send = _send_capture()
    answers, answer = _answer_capture()
    out = portal.handle_portal_callback(
        conn,
        "pp:pos",
        callback=_cb("pp:pos", chat=chat),
        send=send,
        answer=answer,
        edit=edit,
        futures=client,
        now_ms=now_ms,
    )
    return out, edited, sent, answers


# --- a. card buttons ---------------------------------------------------------


def test_a_active_card_last_row_pp_pos(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "a.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    markup = portal._card_buttons()
    rows = markup["inline_keyboard"]
    assert len(rows) == 4
    assert rows[0] == [
        {"text": "🔄 ดูยอด", "callback_data": "pp:v"},
        {"text": "💰 แจ้งฝาก", "callback_data": "pp:dep"},
    ]
    assert rows[1] == [
        {"text": "🔁 ทบกำไร", "callback_data": "pp:rq:cv"},
        {"text": "💸 ถอนกำไร", "callback_data": "pp:rq:wd"},
    ]
    assert rows[2] == [{"text": "🚪 ปิดพอร์ต", "callback_data": "pp:rq:cl"}]
    assert rows[3] == [{"text": "📊 ไม้ที่เปิดอยู่", "callback_data": "pp:pos"}]
    for row in rows:
        for btn in row:
            assert str(btn["callback_data"]).startswith("pp:")
    # closed card still has no buttons
    row = ps.partner_by_telegram_id(conn, PARTNER_TG)
    assert row is not None
    conn.execute(
        "UPDATE partner SET status='closed', updated_at_ms=? WHERE id=?",
        (NOW + 50, row["id"]),
    )
    conn.commit()
    closed = ps.partner_by_telegram_id(conn, PARTNER_TG)
    assert closed is not None
    text, buttons = portal._build_card(
        conn,
        closed,
        AccountValueError(reason="no_client"),
        now_ms=NOW + 60,
    )
    assert "ปิดพอร์ตแล้ว" in text
    assert buttons is None
    conn.close()


# --- b. LONG happy path ------------------------------------------------------


def test_b_long_btc_render_and_edit_chat(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "b.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    _insert_watch(conn)
    client = FakePosClient([_pos()])
    out, edited, sent, answers = _run_pos(conn, client)
    assert out == {"ok": True, "cmd": "positions"}
    assert len(answers) == 1
    assert edited
    assert edited[0]["chat_id"] == PARTNER_TG
    text = edited[0]["text"]
    assert "BTCUSDT · LONG" in text
    assert "เข้า 91234.5" in text
    assert "ตอนนี้ 92010.0" in text
    assert "+0.85%" in text
    assert "เปิด ~" in text
    kb = edited[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == ["pp:pos", "pp:v"]
    assert sent == []
    conn.close()


# --- c. SHORT sign -----------------------------------------------------------


def test_c_short_sign(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "c.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    # mark below entry → profit for short → +
    client = FakePosClient([_pos(side="SHORT", amt="-0.01", entry="100", mark="90")])
    text = ppos.render_partner_positions(conn, client, now_ms=NOW + 1)
    assert "SHORT" in text
    assert "+10.00%" in text
    ppos.reset_positions_cache()
    # mark above entry → loss → -
    client2 = FakePosClient([_pos(side="SHORT", amt="-0.01", entry="100", mark="110")])
    text2 = ppos.render_partner_positions(conn, client2, now_ms=NOW + 2)
    assert "-10.00%" in text2
    conn.close()


# --- d/e. privacy ------------------------------------------------------------


def test_d_e_stop_and_other_privacy(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "de.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    _bind_second(conn, "C", OTHER_TG, now_ms=NOW + 20)
    _insert_watch(conn, initial_stop=90500.0, qty=1.234567)
    pos = _pos(amt="1.234567", liq="88888.8")
    client = FakePosClient([pos])
    text = ppos.render_partner_positions(conn, client, now_ms=NOW + 100)
    low = text.lower()
    assert "stop" not in low
    assert "SL" not in text
    assert "trailing" not in low
    assert "90500" not in text
    assert "R —" not in text
    assert re.search(r"\d(\.\d+)?R\b", text) is None
    assert "1.234567" not in text
    assert "88888" not in text
    assert "qty" not in low
    # symbols may end in USDT; forbid amount/unit copy like "12.5 USDT"
    assert " USDT" not in text
    assert not re.search(r"\d\s*USDT", text)
    assert "liq" not in low
    assert "TP" not in text
    assert "NAV" not in text
    assert "wallet" not in low
    assert "leverage" not in low
    assert "แนะนำ" not in text
    assert "พอร์ตของ C" not in text
    assert "partner C" not in low
    conn.close()


# --- f. open-time omit rules -------------------------------------------------


def test_f_open_time_omit(tmp_path: Path) -> None:
    cases = [
        ("none", None),
        ("side", {"side": "short"}),
        ("shift", {"entry": 91234.5 * 1.001}),  # > 0.05%
        ("closed", {"closed_at_ms": NOW}),
    ]
    for label, override in cases:
        conn = open_db(tmp_path / f"f_{label}.db")
        _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
        ppos.reset_positions_cache()
        if override is not None:
            kw: dict[str, Any] = {"initial_stop": 90500.0}
            kw.update(override)
            _insert_watch(conn, **kw)
        client = FakePosClient([_pos()])
        text = ppos.render_partner_positions(conn, client, now_ms=NOW + 10)
        assert "BTCUSDT · LONG" in text
        assert "เข้า 91234.5" in text
        assert "+0.85%" in text
        assert "เปิด ~" not in text
        conn.close()


# --- g. entry 0 and filters raise --------------------------------------------


def test_g_entry_zero_and_filters_fallback(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "g.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    client = FakePosClient([_pos(entry="0", mark="92010.0")])
    text = ppos.render_partner_positions(conn, client, now_ms=NOW + 1)
    assert "ยังไม่ปิด —" in text
    ppos.reset_positions_cache()
    # huge/tiny value with filters error → no exponent notation
    client2 = FakePosClient(
        [
            FuturesPosition(
                symbol="BTCUSDT",
                position_side="LONG",
                position_amt=D("0.01"),
                entry_price=D("91234.5"),
                mark_price=D("92010.0"),
            )
        ],
        filters_error=True,
    )
    text2 = ppos.render_partner_positions(conn, client2, now_ms=NOW + 2)
    assert "e+" not in text2.lower()
    assert "e-" not in text2.lower()
    assert "เข้า" in text2
    conn.close()


# --- h. empty / error / cache ------------------------------------------------


def test_h_empty_error_and_cache(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    _bind_second(conn, "C", OTHER_TG, now_ms=NOW + 20)

    empty = FakePosClient([])
    assert ppos.render_partner_positions(conn, empty, now_ms=NOW + 1) == "ตอนนี้ไม่มีไม้ที่เปิดอยู่"

    ppos.reset_positions_cache()
    err = FakePosClient(risk_error=RuntimeError("BoomErrorXYZ"))
    text = ppos.render_partner_positions(conn, err, now_ms=NOW + 2)
    assert text == "อ่านไม้ไม่ได้ตอนนี้ ลองใหม่อีกครั้ง"
    assert "BoomErrorXYZ" not in text
    assert "RuntimeError" not in text

    # failure cache: 1 call at t+9s, 2 at t+11s
    assert err.risk_calls == 1
    ppos.render_partner_positions(conn, err, now_ms=NOW + 2 + 9_000)
    assert err.risk_calls == 1
    ppos.render_partner_positions(conn, err, now_ms=NOW + 2 + 11_000)
    assert err.risk_calls == 2

    # success cache shared by two partners
    ppos.reset_positions_cache()
    ok_client = FakePosClient([_pos()])
    t0 = NOW + 100_000
    a_text = ppos.render_partner_positions(conn, ok_client, now_ms=t0)
    b_text = ppos.render_partner_positions(conn, ok_client, now_ms=t0 + 10_000)
    assert a_text == b_text
    assert ok_client.risk_calls == 1
    ppos.render_partner_positions(conn, ok_client, now_ms=t0 + 31_000)
    assert ok_client.risk_calls == 2

    # None client
    ppos.reset_positions_cache()
    assert ppos.render_partner_positions(conn, None, now_ms=t0) == "อ่านไม้ไม่ได้ตอนนี้ ลองใหม่อีกครั้ง"
    conn.close()


# --- i. closed / unbound / group / mismatch ----------------------------------


def test_i_closed_and_ignored(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "i.db")
    pid = _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    conn.execute(
        "UPDATE partner SET status='closed', updated_at_ms=? WHERE id=?",
        (NOW + 50, pid),
    )
    conn.commit()
    client = FakePosClient([_pos()])
    out, edited, sent, answers = _run_pos(conn, client, now_ms=NOW + 60)
    assert out == {"ok": True, "cmd": "positions"}
    assert answers  # answered once
    body = edited[0]["text"] if edited else sent[0]["text"]
    assert body == "ปิดพอร์ตแล้ว"
    markup = edited[0]["reply_markup"] if edited else sent[0]["reply_markup"]
    assert markup is None
    assert client.risk_calls == 0

    # unbound
    edited2, edit2 = _edit_capture()
    sent2, send2 = _send_capture()
    answers2, answer2 = _answer_capture()
    out2 = portal.handle_portal_callback(
        conn,
        "pp:pos",
        callback=_cb("pp:pos", chat=999),
        send=send2,
        answer=answer2,
        edit=edit2,
        futures=client,
        now_ms=NOW + 70,
    )
    assert out2 == {"ignored": "foreign_chat"}
    assert sent2 == []
    assert edited2 == []

    # group
    out3 = portal.handle_portal_callback(
        conn,
        "pp:pos",
        callback=_cb("pp:pos", chat=501, chat_type="group"),
        send=send2,
        answer=answer2,
        edit=edit2,
        futures=client,
        now_ms=NOW + 80,
    )
    assert out3 == {"ignored": "foreign_chat"}

    # mismatched from
    out4 = portal.handle_portal_callback(
        conn,
        "pp:pos",
        callback=_cb("pp:pos", chat=501, from_id=777),
        send=send2,
        answer=answer2,
        edit=edit2,
        futures=client,
        now_ms=NOW + 90,
    )
    assert out4 == {"ignored": "foreign_chat"}
    assert client.risk_calls == 0
    conn.close()


# --- j. owner chat via listen ------------------------------------------------


def test_j_owner_pp_pos_ignored(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "j.db")
    client = FakePosClient([_pos()])
    answers, answer = _answer_capture()
    out = process_update(
        conn,
        {
            "callback_query": {
                "id": "cq-own",
                "data": "pp:pos",
                "from": {"id": int(ADMIN)},
                "message": {
                    "message_id": 1,
                    "chat": {"id": int(ADMIN), "type": "private"},
                    "text": "x",
                },
            }
        },
        send=_send_capture()[1],
        answer=answer,
        futures=client,
        now_ms_value=NOW,
    )
    assert out == {"ignored": "partner_prefix"}
    assert client.risk_calls == 0
    assert len(answers) == 1
    conn.close()


# --- k. no writes ------------------------------------------------------------


def test_k_no_writes(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "k.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    _insert_watch(conn)
    client = FakePosClient([_pos()])
    before = conn.total_changes
    _run_pos(conn, client)
    assert conn.total_changes == before
    conn.close()


# --- l. truncation -----------------------------------------------------------


def test_l_twelve_positions_truncates(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "l.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    positions = [
        _pos(
            symbol=f"S{i:02d}USDT",
            side="LONG",
            entry="100",
            mark="101",
            amt="0.1",
        )
        for i in range(12)
    ]
    client = FakePosClient(positions)
    text = ppos.render_partner_positions(conn, client, now_ms=NOW + 1)
    # count numbered lines
    numbered = [ln for ln in text.splitlines() if re.match(r"^\d+\. ", ln)]
    assert len(numbered) == 10
    assert "+2 ไม้" in text
    conn.close()


# --- m. static guard ---------------------------------------------------------


def test_m_static_guard_partner_positions() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
    src = (root / "partner_positions.py").read_text(encoding="utf-8")
    assert "float(" not in src
    low = src.lower()
    for bad in (
        "stop",
        "all_open_orders",
        "algo",
        "initial_",
        "liquidation",
        "jarvise_assist.execute",
        "import execute",
        "place_",
        "cancel",
        "new_order",
        "post(",
        "delete(",
    ):
        assert bad not in low, bad

    prefix_re = re.compile(r"""["'](ms|xx|ey|ec|en|mn):""", re.IGNORECASE)
    assert prefix_re.search(src) is None
    assert prefix_re.search('"ms:x"') is not None
    assert prefix_re.search("'xx:'") is not None
    assert prefix_re.search('f"ey:{a}"') is not None
    assert prefix_re.search("now_ms: int") is None
    assert prefix_re.search("when: int") is None

    tree = ast.parse(src)
    allowed = {"__future__", "sqlite3", "datetime", "decimal", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, node.module
            mod = node.module or ""
            assert mod.split(".")[0] in allowed, mod


def test_n_refresh_unchanged_no_duplicate(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "n.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)
    _insert_watch(conn)
    client = FakePosClient([_pos()])
    t0 = NOW + 100
    out1, edited1, sent1, answers1 = _run_pos(conn, client, now_ms=t0)
    assert out1 == {"ok": True, "cmd": "positions"}
    assert len(edited1) == 1
    assert sent1 == []
    assert len(answers1) == 1
    first_text = edited1[0]["text"]

    edited2, edit2 = _edit_capture()
    sent2, send2 = _send_capture()
    answers2, answer2 = _answer_capture()
    out2 = portal.handle_portal_callback(
        conn,
        "pp:pos",
        callback=_cb("pp:pos", chat=501, text=first_text),
        send=send2,
        answer=answer2,
        edit=edit2,
        futures=client,
        now_ms=t0 + 5_000,
    )
    assert out2.get("unchanged") is True
    assert out2 == {"ok": True, "cmd": "positions", "unchanged": True}
    assert edited2 == []
    assert sent2 == []
    assert len(answers2) == 1
    assert client.risk_calls == 1
    conn.close()


def test_o_client_build_failure_shows_error(tmp_path: Path, monkeypatch: Any) -> None:
    conn = open_db(tmp_path / "o.db")
    _bind_partner(conn, "B", PARTNER_TG, now_ms=NOW)

    def boom() -> Any:
        raise FileNotFoundError("/opt/jarvise/secrets/x.pem")

    monkeypatch.setattr("jarvise_assist.partner_portal.build_futures_client", boom)

    edited_pos, edit_pos = _edit_capture()
    sent_pos, send_pos = _send_capture()
    answers_pos, answer_pos = _answer_capture()
    out_pos = portal.handle_portal_callback(
        conn,
        "pp:pos",
        callback=_cb("pp:pos", chat=501),
        send=send_pos,
        answer=answer_pos,
        edit=edit_pos,
        futures=None,
        now_ms=NOW + 100,
    )
    assert out_pos == {"ok": True, "cmd": "positions"}
    assert edited_pos
    assert edited_pos[0]["text"] == "อ่านไม้ไม่ได้ตอนนี้ ลองใหม่อีกครั้ง"
    assert sent_pos == []

    portal.reset_value_cache()
    edited_v, edit_v = _edit_capture()
    sent_v, send_v = _send_capture()
    answers_v, answer_v = _answer_capture()
    out_v = portal.handle_portal_callback(
        conn,
        "pp:v",
        callback=_cb("pp:v", chat=501),
        send=send_v,
        answer=answer_v,
        edit=edit_v,
        futures=None,
        now_ms=NOW + 200,
    )
    assert out_v["ok"] is True
    assert edited_v
    assert "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง" in edited_v[0]["text"]
    assert sent_v == []

    leak_haystack = " ".join(str(e.get("text", "")) for e in edited_pos + edited_v + sent_pos + sent_v)
    assert ".pem" not in leak_haystack
    assert "secrets" not in leak_haystack
    conn.close()
