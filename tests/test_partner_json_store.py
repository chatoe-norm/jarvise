"""Unit tests for partner_json_store preview tokens."""

from __future__ import annotations

import json
from pathlib import Path

from jarvise_assist.partner_json_store import (
    consume_owner_preview,
    issue_owner_preview,
    load_json_object,
    restore_owner_preview,
    revoke_owner_preview,
)

NOW = 1_700_000_000_000
TTL_MS = 900_000


def test_restore_does_not_resurrect_revoked_token(tmp_path: Path) -> None:
    path = tmp_path / "partner_owner_preview.json"
    token = issue_owner_preview(
        path,
        side="dep",
        tran_id="t1",
        amount_micro=50_000_000,
        issued_at_ms=NOW,
    )
    preview = consume_owner_preview(path, token, now_ms=NOW + 1)
    assert preview is not None
    assert preview["replay"] is False
    assert revoke_owner_preview(path, token, now_ms=NOW + 1) is True
    restore_owner_preview(path, preview, now_ms=NOW + 1)
    assert consume_owner_preview(path, token, now_ms=NOW + 2) is None
    data = load_json_object(path)
    assert token not in (data.get("previews") or {})


def test_issue_prunes_expired_and_consumed_old_previews(tmp_path: Path) -> None:
    path = tmp_path / "partner_owner_preview.json"
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "previews": {
                    "expired_hex_aaaaaaaaaaaaaaaa": {
                        "side": "dep",
                        "tran_id": "old",
                        "amount_micro": 1_000_000,
                        "issued_at_ms": NOW - TTL_MS - 1,
                    },
                    "consumed_old_bbbbbbbbbbbbbbbb": {
                        "side": "wd",
                        "tran_id": "gone",
                        "amount_micro": 2_000_000,
                        "issued_at_ms": NOW - TTL_MS - 5_000,
                        "consumed": True,
                    },
                    "future_hex_cccccccccccccccc": {
                        "side": "dep",
                        "tran_id": "future",
                        "amount_micro": 3_000_000,
                        "issued_at_ms": NOW + 120_000,
                    },
                    "valid_hex_dddddddddddddddd": {
                        "side": "dep",
                        "tran_id": "keep",
                        "amount_micro": 4_000_000,
                        "issued_at_ms": NOW - 60_000,
                    },
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    fresh = issue_owner_preview(
        path,
        side="wd",
        tran_id="new",
        amount_micro=5_000_000,
        issued_at_ms=NOW,
    )
    data = load_json_object(path)
    previews = data.get("previews") or {}
    assert set(previews) == {"valid_hex_dddddddddddddddd", fresh}
    assert "expired_hex_aaaaaaaaaaaaaaaa" not in previews
    assert "consumed_old_bbbbbbbbbbbbbbbb" not in previews
    assert "future_hex_cccccccccccccccc" not in previews
    kept = consume_owner_preview(path, "valid_hex_dddddddddddddddd", now_ms=NOW)
    assert kept is not None
    assert kept["tran_id"] == "keep"
