"""Mirror of web/src/lib/copy.ts ownerReasonCopy — keep in sync."""


def owner_reason_copy(reason: str | None) -> str:
    if reason is None or reason == "":
        return "—"
    return reason.replace("หลักคำสอน", "กฎการเทรด")


def test_owner_reason_copy_maps_stored_claude_thai() -> None:
    raw = (
        "เทรนด์ขาขึ้นชัดเจน EMA20>EMA200 มาก ราคาอยู่เหนือ EMA20 เล็กน้อย "
        "RSI กลางๆ 51 ไม่ร้อนเกิน ตรงกับหลักคำสอน pullback entry บน trend_up กระดาษเท่านั้น"
    )
    out = owner_reason_copy(raw)
    assert "กฎการเทรด" in out
    assert "หลักคำสอน" not in out
    assert out.startswith("เทรนด์ขาขึ้น")


def test_owner_reason_copy_leaves_rule_prefix() -> None:
    assert owner_reason_copy("auto:rule:same_side_hold") == "auto:rule:same_side_hold"


def test_owner_reason_copy_empty() -> None:
    assert owner_reason_copy(None) == "—"
    assert owner_reason_copy("") == "—"
