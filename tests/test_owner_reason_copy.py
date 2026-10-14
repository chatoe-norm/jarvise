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


def resolve_reason_label(reason: str | None) -> str:
    """Mirror of web/src/lib/copy.ts resolveReasonLabel — keep in sync."""
    if reason is None or reason == "":
        return "—"
    mapping = {
        "auto:rule:same_side_hold": "ถือไม้เดิม (ฝั่งเดียวกัน)",
        "auto:rule:opposite_side_open": "เลื่อน — มีไม้ฝั่งตรงข้าม",
        "auto:rule:no_doctrine_low_conf": "เลื่อน — กฎการเทรดไม่พร้อม / ความเชื่อมั่นต่ำ",
        "auto:rule:flat_exit": "ปิดไม้ตามสัญญาณ FLAT",
        "auto:claude:approve": "อนุมัติ — Claude",
        "auto:claude:unparseable": "เลื่อน — Claude อ่านผลไม่ได้",
        "deferred_cap": "เลื่อน — ถึงเพดานรอบนี้",
        "missing_api_key": "เลื่อน — ไม่มี API key",
        "auto_decide_disabled": "ปิด auto-decide",
        "timeout_flat": "หมดเวลา → FLAT",
    }
    if reason in mapping:
        return mapping[reason]
    if reason.startswith("auto:claude:reject:"):
        rest = owner_reason_copy(reason[len("auto:claude:reject:") :])
        return "ปฏิเสธ — Claude" if rest == "—" else f"ปฏิเสธ — Claude: {rest}"
    if reason.startswith("auto:claude:error:"):
        return f"เลื่อน — Claude ผิดพลาด: {reason[len('auto:claude:error:') :]}"
    return owner_reason_copy(reason)


def test_resolve_reason_label_same_side_hold() -> None:
    assert resolve_reason_label("auto:rule:same_side_hold") == "ถือไม้เดิม (ฝั่งเดียวกัน)"


def test_resolve_reason_label_claude_reject_remaps_doctrine_word() -> None:
    out = resolve_reason_label("auto:claude:reject:ขัดกับหลักคำสอน")
    assert "กฎการเทรด" in out
    assert "หลักคำสอน" not in out


def test_owner_reason_copy_empty() -> None:
    assert owner_reason_copy(None) == "—"
    assert owner_reason_copy("") == "—"
