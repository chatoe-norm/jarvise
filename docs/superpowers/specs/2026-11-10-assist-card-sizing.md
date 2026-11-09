# Assist card sizing (PR-D)

The card and the held-position list change what they show and what `assist_signal` stores. Qty, notional, leverage, and `loss_at_stop` stay the results of the current sizer.

## Fields on `SizedSignal`

Optional, default empty:

- `balance_usdt` — USD-M futures USDT wallet balance used for this size
- `budget_usdt` — `balance × risk_pct / 100`
- `reduce_reasons` — ordered tuple of reason codes
- `requote_anchor_id`, `requote_anchor_sent_at_ms`, `requote_first_loss` — set only when a re-quote cap lowers qty

Reason codes, in this order, only when that step lowers qty:

| Code | Where |
| --- | --- |
| `step` | `floor_to_step` of the raw qty in `size_signal` |
| `leverage_cap` | the `MAX_LEVERAGE` branch in `size_signal` |
| `margin_cap` | `_shrink_for_max_leverage` when shared leverage shrinks qty |
| `requote_cap` | `cap_requote_qty` when the first-loss (or first-qty) cap lowers qty |

A shared-leverage update that only changes the leverage number does not add `margin_cap`.

## Card

`format_signal_message` keeps every current line. After the size line:

`ฐาน: Futures {balance} USDT × {risk}% = งบ {budget} USDT`

`risk_pct` is the budget input. The loss line stays the size used.

Reduction lines print only when `loss_at_stop < 0.95 × budget`, after the loss line:

- `step`: `ลดขนาดเพราะ: ปัดจำนวนตาม lot step` only when that floor removes more than 5% of the budget
- `leverage_cap`: no new line. The existing `ขนาดเต็มต้องใช้ Nx — ลดเป็น Mx แล้ว` is the leverage reason
- `margin_cap`: `ลดขนาดเพราะ: margin ไม่พอสำหรับหลายไอเดียพร้อมกัน`
- `requote_cap`: `ลดขนาดเพราะ: เพดาน re-quote จากไอเดีย {SYMBOL} {HH:MM} (เสี่ยงครั้งแรก {x} USDT)` in Asia/Bangkok

Push, the morning batch, on-demand, and the one-tap preview all use this formatter.

## Storage

Migration 26 `assist_card_sizing` adds nullable columns on `assist_signal` and does not rewrite existing rows:

- `balance_usdt REAL`
- `risk_pct REAL`
- `budget_usdt REAL`
- `reduce_reasons TEXT` — JSON list of reason codes, null when empty
- `requote_anchor_id TEXT`

`insert_signal`, `record_blocked_signal`, and `mark_signal_sent` write them. The morning re-quote updates the held row through `mark_signal_sent`.

The entry preview JSON gets those five keys plus `requote_anchor_sent_at_ms` and `requote_first_loss`. No extra migration.

## Held list

`render_open_positions` keeps the same facts. Layout:

```
📋 ไม้ที่ถืออยู่

1. BTCUSDT
• LONG · qty …
• เข้า … · mark …
• stop …
• ยังไม่ปิด: …

2. ETHUSDT
• …
```

A blank line separates coins. Non-universe rows are numbered the same way. The list does not place, cancel, or edit an order.
