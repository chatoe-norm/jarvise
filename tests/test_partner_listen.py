"""Listen routing hooks for partner admin commands (P2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvise_assist.listen import process_update
from jarvise_ingest.db import open_db


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        mid = len(sent) + 1
        sent.append({"text": text, "reply_markup": reply_markup, "message_id": mid})
        return mid

    return sent, send


def _callback(
    data: str, *, chat: int = 42, mid: int = 7, chat_type: str = "private", from_id: int = 42
) -> dict[str, Any]:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cq1",
            "data": data,
            "from": {"id": from_id},
            "message": {
                "message_id": mid,
                "chat": {"id": chat, "type": chat_type},
                "text": "body",
            },
        },
    }


def _message(text: str, *, chat: int = 42, chat_type: str = "private", from_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat, "type": chat_type},
            "from": {"id": from_id},
            "text": text,
        },
    }


def test_p_reaches_overview(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "p.db")
    seen: list[str] = []

    def fake(conn: Any, text: str, **kw: Any) -> dict[str, Any]:
        seen.append(text)
        kw["send"]("overview-ok")
        return {"ok": True, "cmd": "overview"}

    monkeypatch.setattr("jarvise_assist.listen.handle_partner_text", fake)
    sent, send = _send_capture()
    out = process_update(conn, _message("p"), send=send, futures=object())
    assert seen == ["p"]
    assert out["cmd"] == "overview"
    assert sent[0]["text"] == "overview-ok"
    conn.close()


def test_p_foreign_chat_silent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pf.db")
    called = []

    def fake(*a: Any, **k: Any) -> dict[str, Any]:
        called.append(1)
        return {"ok": True}

    monkeypatch.setattr("jarvise_assist.listen.handle_partner_text", fake)
    sent, send = _send_capture()
    out = process_update(conn, _message("p", chat=99), send=send)
    assert out == {"ignored": "foreign_chat"}
    assert sent == []
    assert called == []
    conn.close()


def test_pa_dy_foreign_silent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pady.db")
    called = []

    def fake(*a: Any, **k: Any) -> dict[str, Any]:
        called.append(1)
        return {"ok": True}

    monkeypatch.setattr("jarvise_assist.listen.handle_partner_callback", fake)
    sent, send = _send_capture()
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    out = process_update(
        conn,
        _callback("pa:dy:pr_0123456789abcdef", chat=99),
        send=send,
        answer=answer,
    )
    assert out == {"ignored": "foreign_chat"}
    assert called == []
    assert answers == []
    assert sent == []
    conn.close()


def test_partner_beats_awaiting_explore(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pex.db")
    from jarvise_assist.store import write_settings

    write_settings(conn, awaiting_explore=True)
    partner_hits: list[str] = []
    explore_hits: list[str] = []

    def fake_partner(conn: Any, text: str, **kw: Any) -> dict[str, Any]:
        partner_hits.append(text)
        return {"ok": True, "partner": True}

    def fake_explore(sym: str) -> dict[str, Any] | None:
        explore_hits.append(sym)
        return None

    monkeypatch.setattr("jarvise_assist.listen.handle_partner_text", fake_partner)
    sent, send = _send_capture()
    out = process_update(
        conn,
        _message("p help"),
        send=send,
        explore_lookup=fake_explore,
    )
    assert partner_hits == ["p help"]
    assert explore_hits == []
    assert out.get("partner") is True
    conn.close()


def test_quick_still_works(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pq.db")
    quick_hits: list[str] = []

    def fake_quick(conn: Any, text: str, **kw: Any) -> dict[str, Any]:
        quick_hits.append(text)
        return {"ok": True, "quick": True}

    monkeypatch.setattr("jarvise_assist.listen.handle_quick_text", fake_quick)
    monkeypatch.setattr(
        "jarvise_assist.listen.parse_quick_command",
        lambda t: {"symbol": "BTCUSDT"} if t.startswith("q ") else None,
    )
    sent, send = _send_capture()
    out = process_update(conn, _message("q BTC long 1%"), send=send, futures=object())
    assert quick_hits == ["q BTC long 1%"]
    assert out.get("quick") is True
    conn.close()


def test_unknown_callback_fallthrough(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "puc.db")
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    out = process_update(
        conn,
        _callback("not_a_real_prefix"),
        send=_send_capture()[1],
        answer=answer,
    )
    assert out.get("unhandled_callback") == "not_a_real_prefix"
    assert answers == [("cq1", None)]
    conn.close()


def test_pa_callback_routed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pacb.db")
    seen: list[str] = []

    def fake(conn: Any, data: str, **kw: Any) -> dict[str, Any]:
        seen.append(data)
        kw["answer"]("ok")
        return {"ok": True, "pa": data}

    monkeypatch.setattr("jarvise_assist.listen.handle_partner_callback", fake)
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    out = process_update(
        conn,
        _callback("pa:iy"),
        send=_send_capture()[1],
        answer=answer,
        futures=object(),
    )
    assert seen == ["pa:iy"]
    assert out.get("pa") == "pa:iy"
    conn.close()


# ---- P3a routing -------------------------------------------------------------


def test_bound_partner_p_reaches_portal(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pp_route.db")
    from decimal import Decimal

    from jarvise_assist import partner_store as ps

    a = ps.create_partner(conn, name="admin", is_admin=True, now_ms=1)
    b = ps.create_partner(conn, name="B", is_admin=False, now_ms=2)
    ps.apply_deposit(conn, a["partner_id"], Decimal("1000"), Decimal("1000"), 3)
    inv = ps.create_invite(conn, b["partner_id"], now_ms=4)
    assert ps.bind_invite(conn, inv["code"], "501", admin_chat_id="42", now_ms=5)["ok"]

    portal_hits: list[str] = []
    admin_hits: list[str] = []
    risk_hits: list[str] = []
    quick_hits: list[str] = []

    def fake_portal(conn: Any, message: dict[str, Any], **kw: Any) -> dict[str, Any]:
        portal_hits.append(str(message.get("text")))
        return {"ok": True, "portal": True}

    def fake_admin(*a: Any, **k: Any) -> dict[str, Any]:
        admin_hits.append("x")
        return {"ok": True}

    def fake_risk(*a: Any, **k: Any) -> dict[str, Any]:
        risk_hits.append("x")
        return {"handled": False}

    def fake_quick(*a: Any, **k: Any) -> dict[str, Any]:
        quick_hits.append("x")
        return {"ok": True}

    monkeypatch.setattr("jarvise_assist.listen.handle_portal_text", fake_portal, raising=False)
    monkeypatch.setattr("jarvise_assist.listen.handle_partner_text", fake_admin)
    monkeypatch.setattr("jarvise_assist.listen.handle_risk_number", fake_risk)
    monkeypatch.setattr("jarvise_assist.listen.handle_quick_text", fake_quick)
    out = process_update(conn, _message("p", chat=501, from_id=501), send=_send_capture()[1])
    assert portal_hits == ["p"]
    assert admin_hits == []
    assert risk_hits == []
    assert quick_hits == []
    assert out.get("portal") is True
    conn.close()


def test_partner_foreign_callbacks_silent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pp_fcb.db")
    called: list[str] = []

    def track(name: str) -> Any:
        def fake(*a: Any, **k: Any) -> dict[str, Any]:
            called.append(name)
            return {"ok": True}

        return fake

    monkeypatch.setattr("jarvise_assist.listen.handle_partner_callback", track("pa"))
    monkeypatch.setattr("jarvise_assist.listen.handle_portal_callback", track("pp"), raising=False)
    monkeypatch.setattr("jarvise_assist.listen.handle_risk_callback", track("risk"))
    monkeypatch.setattr("jarvise_assist.listen.handle_exec_callback", track("exec"))
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    for data in (
        "pa:dy:pr_0123456789abcdef",
        "pa:pd:pr_0123456789abcdef",
        "risk:1",
        "ey:x",
    ):
        out = process_update(
            conn,
            _callback(data, chat=501, from_id=501),
            send=_send_capture()[1],
            answer=answer,
        )
        assert out == {"ignored": "foreign_chat"}
    assert called == []
    assert answers == []
    conn.close()


def test_owner_pp_answered_ignored(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pp_own.db")
    portal_hits: list[str] = []

    def fake_portal(*a: Any, **k: Any) -> dict[str, Any]:
        portal_hits.append("x")
        return {"ok": True}

    monkeypatch.setattr("jarvise_assist.listen.handle_portal_callback", fake_portal, raising=False)
    answers: list[Any] = []

    def answer(cq_id: str, text: str | None = None) -> bool:
        answers.append((cq_id, text))
        return True

    out = process_update(
        conn,
        _callback("pp:v"),
        send=_send_capture()[1],
        answer=answer,
    )
    assert out == {"ignored": "partner_prefix"}
    assert answers == [("cq1", None)]
    assert portal_hits == []
    conn.close()


def test_no_owner_chat_bound_partner_silent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    conn = open_db(tmp_path / "pp_no.db")
    portal_hits: list[str] = []

    def fake_portal(*a: Any, **k: Any) -> dict[str, Any]:
        portal_hits.append("x")
        return {"ok": True}

    monkeypatch.setattr("jarvise_assist.listen.handle_portal_text", fake_portal, raising=False)
    out = process_update(conn, _message("p", chat=501, from_id=501), send=_send_capture()[1])
    assert out == {"ignored": "no_owner_chat"}
    assert portal_hits == []
    conn.close()
