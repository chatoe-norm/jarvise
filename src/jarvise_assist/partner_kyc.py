"""Partner payout KYC. Field encryption and form checks only.

No ledger math. Must not import partner_store, partner_portal, partner_admin,
partner_audit, or partner_rules. Plaintext stays in memory for the caller that
is about to show it; logs carry partner_id, kyc_id, and reason codes only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from jarvise_ingest.db import transaction

logger = logging.getLogger(__name__)

KYC_KEY_MESSAGE = "partner kyc key unavailable"
KYC_DECRYPT_MESSAGE = "partner kyc decrypt failed"
DEFAULT_KEY_PATH = "/opt/jarvise/secrets/partner_kyc.key"
KEY_ID = "k1"
DRAFT_TTL_MS = 15 * 60 * 1000
_FORM_MAX = 600
_ENC_INFO = b"jarvise-partner-kyc-enc-v1"
_FP_INFO = b"jarvise-partner-kyc-fp-v1"
_FIELDS = ("real_name", "phone", "account_no", "holder_name")
_TWELVE_DIGIT = frozenset({"GSB", "BAAC", "GHB"})

KYC_WARNING = (
    "⚠️ กรอกข้อมูลจริงเท่านั้น ห้ามใช้บัญชีของคนอื่นหรือบัญชีปลอม"
    " บัญชีรับเงินต้องเป็นชื่อของคุณเอง เมื่อกดยืนยันบันทึกแล้วจะแก้ไขเองไม่ได้ ต้องติดต่อแอดมิน"
)

_LABELS = {
    "ชื่อจริง": "real_name",
    "ชื่อ-นามสกุล": "real_name",
    "ชื่อ": "real_name",
    "เบอร์": "phone",
    "เบอร์โทร": "phone",
    "โทร": "phone",
    "มือถือ": "phone",
    "ธนาคาร": "bank",
    "แบงก์": "bank",
    "เลขบัญชี": "account_no",
    "บัญชี": "account_no",
    "ชื่อบัญชี": "holder_name",
}
_REQUIRED = ("real_name", "phone", "bank", "account_no", "holder_name")
_MISSING_TH = {
    "real_name": "ขาดชื่อจริง",
    "phone": "ขาดเบอร์มือถือ",
    "bank": "ขาดธนาคาร",
    "account_no": "ขาดเลขบัญชี",
    "holder_name": "ขาดชื่อบัญชี",
}
_THAI_TITLES = ("นางสาว", "น.ส.", "ด.ช.", "ด.ญ.", "นาย", "นาง")
_EN_TITLES = ("miss", "mrs", "mr", "ms")
# Thai letters and marks only. Digits (๐-๙) and ฿ sit outside these ranges.
_NAME_RE = re.compile(r"^[A-Za-z\u0E01-\u0E3A\u0E40-\u0E4E .'\-]{3,80}$")
_PHONE_RE = re.compile(r"^0[689][0-9]{8}$")
_ACCOUNT_RE = re.compile(r"^[0-9]{10,12}$")
_THAI_DIGIT_TRANS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

# Closed set. Codes and Thai aliases, matched case-insensitively. No PromptPay.
BANKS: dict[str, tuple[str, ...]] = {
    "KBANK": ("kbank", "กสิกร", "กสิกรไทย", "ธนาคารกสิกรไทย"),
    "SCB": ("scb", "ไทยพาณิชย์", "ธนาคารไทยพาณิชย์"),
    "BBL": ("bbl", "กรุงเทพ", "ธนาคารกรุงเทพ"),
    "KTB": ("ktb", "กรุงไทย", "ธนาคารกรุงไทย"),
    "BAY": ("bay", "กรุงศรี", "กรุงศรีอยุธยา", "ธนาคารกรุงศรีอยุธยา"),
    "TTB": ("ttb", "tmb", "ทหารไทย", "ทหารไทยธนชาต", "ธนชาต", "ธนาคารทหารไทยธนชาต"),
    "GSB": ("gsb", "ออมสิน", "ธนาคารออมสิน"),
    "BAAC": ("baac", "ธกส", "ธ.ก.ส.", "ธ.ก.ส", "ธนาคารเพื่อการเกษตรและสหกรณ์การเกษตร"),
    "GHB": ("ghb", "อาคารสงเคราะห์", "ธนาคารอาคารสงเคราะห์"),
    "KKP": ("kkp", "เกียรตินาคิน", "เกียรตินาคินภัทร", "ธนาคารเกียรตินาคินภัทร"),
    "CIMBT": ("cimbt", "cimb", "ซีไอเอ็มบี", "ซีไอเอ็มบีไทย", "ธนาคารซีไอเอ็มบีไทย"),
    "UOBT": ("uobt", "uob", "ยูโอบี", "ธนาคารยูโอบี"),
    "LHB": ("lhb", "lhbank", "แลนด์แอนด์เฮ้าส์", "ธนาคารแลนด์แอนด์เฮ้าส์"),
    "TISCO": ("tisco", "ทิสโก้", "ธนาคารทิสโก้"),
    "ICBCT": ("icbct", "icbc", "ไอซีบีซี", "ธนาคารไอซีบีซี"),
    "IBANK": ("ibank", "อิสลาม", "ธนาคารอิสลามแห่งประเทศไทย"),
}


class KycKeyError(Exception):
    """Missing or unusable key file. The message never includes the path or key bytes."""

    def __init__(self) -> None:
        super().__init__(KYC_KEY_MESSAGE)


class KycCryptoError(Exception):
    """Decrypt failed. The message never includes plaintext or ciphertext."""

    def __init__(self) -> None:
        super().__init__(KYC_DECRYPT_MESSAGE)


def _master_key() -> bytes:
    raw_path = os.environ.get("JARVISE_PARTNER_KYC_KEY_PATH") or DEFAULT_KEY_PATH
    try:
        text = Path(raw_path).read_text(encoding="utf-8").strip()
        key = base64.b64decode(text, validate=True)
    except Exception:
        raise KycKeyError() from None
    if len(key) != 32:
        raise KycKeyError()
    return key


def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=None, info=info).derive(master)


def _encrypt(master: bytes, aad: str, plaintext: str) -> bytes:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_hkdf(master, _ENC_INFO)).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return b"\x01" + nonce + ct


def _decrypt(master: bytes, aad: str, blob: bytes) -> str:
    raw = bytes(blob)
    if len(raw) < 14 or raw[0:1] != b"\x01":
        raise KycCryptoError()
    try:
        pt = AESGCM(_hkdf(master, _ENC_INFO)).decrypt(raw[1:13], raw[13:], aad.encode("utf-8"))
    except Exception:
        raise KycCryptoError() from None
    return pt.decode("utf-8")


def _fp(master: bytes, message: str) -> str:
    return hmac.new(_hkdf(master, _FP_INFO), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _aad(kyc_id: str, field: str) -> str:
    return f"partner_kyc|{kyc_id}|{field}"


def _draft_aad(partner_id: str, token: str, field: str) -> str:
    return f"partner_kyc_draft|{partner_id}|{token}|{field}"


def _strip_cf(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def clean_name(raw: str) -> str:
    text = _strip_cf(unicodedata.normalize("NFKC", str(raw)))
    return re.sub(r"\s+", " ", text).strip()


def _first_grapheme(token: str) -> str:
    if not token:
        return ""
    out = token[0]
    i = 1
    while i < len(token) and unicodedata.category(token[i]).startswith("M"):
        out += token[i]
        i += 1
    return out


def mask_name(name: str) -> str:
    tokens = clean_name(name).split(" ")
    if not tokens:
        return ""
    masked = [tokens[0]]
    for token in tokens[1:]:
        masked.append(_first_grapheme(token) + "***")
    return " ".join(masked)


def mask_phone(last4: str) -> str:
    return "xxx-xxx-" + last4


def mask_account(bank_code: str, last4: str) -> str:
    return f"{bank_code} ••••{last4}"


def _consume_title(text: str, title: str, *, boundary: bool) -> str | None:
    if not text.casefold().startswith(title.casefold()):
        return None
    rest = text[len(title) :]
    # English titles stop at a letter, so "Mrinal" stays intact. Thai titles keep
    # the older prefix strip, including "นาง" inside "นางนวล".
    if boundary and rest and rest[0] not in ". \t":
        return None
    if rest.startswith("."):
        rest = rest[1:]
    return rest.lstrip()


def _strip_titles(name: str) -> str:
    text = clean_name(name)
    while text:
        nxt: str | None = None
        for title in _THAI_TITLES:
            nxt = _consume_title(text, title, boundary=False)
            if nxt is not None:
                break
        if nxt is None:
            for title in _EN_TITLES:
                nxt = _consume_title(text, title, boundary=True)
                if nxt is not None:
                    break
        if nxt is None:
            break
        text = nxt
    return text


def names_match(left: str, right: str) -> bool:
    return _strip_titles(left).casefold() == _strip_titles(right).casefold()


def _ascii_digits(text: str) -> str:
    """Map Thai digits ๐-๙ to ASCII. Any other Unicode digit stays and then fails."""
    return text.translate(_THAI_DIGIT_TRANS)


def normalize_phone(raw: str) -> str | None:
    text = _strip_cf(unicodedata.normalize("NFKC", str(raw))).strip()
    text = _ascii_digits(text)
    text = re.sub(r"[ \-.()]", "", text)
    if text.startswith("+66"):
        text = "0" + text[3:]
    elif text.startswith("66"):
        text = "0" + text[2:]
    if _PHONE_RE.fullmatch(text):
        return text
    return None


def normalize_account(raw: str) -> str | None:
    text = _strip_cf(unicodedata.normalize("NFKC", str(raw)))
    text = _ascii_digits(text)
    text = text.replace(" ", "").replace("-", "")
    if _ACCOUNT_RE.fullmatch(text):
        return text
    return None


def resolve_bank(raw: str) -> str | None:
    text = _strip_cf(unicodedata.normalize("NFKC", str(raw)))
    text = re.sub(r"\s+", " ", text).strip()
    folded = text.casefold()
    rest = folded[len("ธนาคาร") :].strip() if folded.startswith("ธนาคาร") else folded
    for code, aliases in BANKS.items():
        names = {code.casefold(), *(alias.casefold() for alias in aliases)}
        if folded in names or rest in names:
            return code
    return None


def _split_label(line: str) -> tuple[str, str] | None:
    indexes = [line.find(sep) for sep in (":", "：") if line.find(sep) >= 0]
    if not indexes:
        return None
    idx = min(indexes)
    return line[:idx].strip(), line[idx + 1 :].strip()


def _valid_name(name: str) -> bool:
    if not _NAME_RE.fullmatch(name):
        return False
    tokens = name.split(" ")
    return len(tokens) >= 2 and all(tokens)


def parse_form(text: str) -> dict[str, Any]:
    """Parse a KYC form. Errors are fixed Thai strings and never echo the input."""
    raw = str(text or "")
    errors: list[str] = []
    if len(raw) > _FORM_MAX:
        return {"ok": False, "reason": "kyc_invalid", "errors": ["ข้อความยาวเกิน 600 ตัวอักษร"]}
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines or lines[0] != "ข้อมูลรับเงิน":
        errors.append("บรรทัดแรกต้องเป็น ข้อมูลรับเงิน")
        body: list[str] = []
    else:
        body = lines[1:]
    found: dict[str, str] = {}
    unknown = False
    duplicate = False
    for line in body:
        parts = _split_label(line)
        if parts is None:
            unknown = True
            continue
        label, value = parts
        field = _LABELS.get(label)
        if field is None:
            unknown = True
            continue
        if field in found:
            duplicate = True
            continue
        found[field] = value
    if unknown:
        errors.append("มีช่องที่ไม่รู้จัก")
    if duplicate:
        errors.append("มีช่องซ้ำ")
    for field in _REQUIRED:
        if field not in found or not found[field]:
            errors.append(_MISSING_TH[field])
    if errors:
        return {"ok": False, "reason": "kyc_invalid", "errors": errors}

    real_name = clean_name(found["real_name"])
    holder_name = clean_name(found["holder_name"])
    phone = normalize_phone(found["phone"])
    bank = resolve_bank(found["bank"])
    account = normalize_account(found["account_no"])
    if not _valid_name(real_name):
        errors.append("ชื่อจริงต้องเป็นชื่อและนามสกุล")
    if not _valid_name(holder_name):
        errors.append("ชื่อบัญชีต้องเป็นชื่อและนามสกุล")
    if phone is None:
        errors.append("เบอร์ต้องเป็นมือถือไทย")
    if bank is None:
        errors.append("ไม่รู้จักธนาคาร")
    if account is None:
        errors.append("เลขบัญชีต้องเป็นตัวเลข 10 ถึง 12 หลัก")
    if errors:
        return {"ok": False, "reason": "kyc_invalid", "errors": errors}
    assert phone is not None and bank is not None and account is not None
    expected = 12 if bank in _TWELVE_DIGIT else 10
    return {
        "ok": True,
        "real_name": real_name,
        "phone": phone,
        "bank_code": bank,
        "account_no": account,
        "holder_name": holder_name,
        "holder_matches": names_match(real_name, holder_name),
        "account_length_warn": len(account) != expected,
        "real_name_mask": mask_name(real_name),
        "holder_name_mask": mask_name(holder_name),
        "phone_last4": phone[-4:],
        "account_last4": account[-4:],
    }


def _purge(conn: sqlite3.Connection, now_ms: int) -> None:
    conn.execute("DELETE FROM partner_kyc_draft WHERE expires_at_ms <= ?", (int(now_ms),))


def _partner_row(conn: sqlite3.Connection, partner_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name, is_admin, status FROM partner WHERE id = ?",
        (str(partner_id),),
    ).fetchone()


def _max_version(conn: sqlite3.Connection, partner_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM partner_kyc WHERE partner_id = ?",
        (str(partner_id),),
    ).fetchone()
    if row is None or row["v"] is None:
        return 0
    return int(row["v"])


def kyc_state(conn: sqlite3.Connection, partner_id: str) -> str:
    if _max_version(conn, partner_id) >= 1:
        return "saved"
    return "none"


def masked_profile(conn: sqlite3.Connection, partner_id: str) -> dict[str, str] | None:
    """Mask columns of the current saved KYC. Does not read ciphertext or decrypt."""
    row = current_kyc(conn, partner_id)
    if row is None:
        return None
    return {
        "real_name_mask": str(row["real_name_mask"]),
        "bank_code": str(row["bank_code"]),
        "account_last4": str(row["account_last4"]),
        "holder_name_mask": str(row["holder_name_mask"]),
        "phone_last4": str(row["phone_last4"]),
    }


def current_kyc(conn: sqlite3.Connection, partner_id: str) -> sqlite3.Row | None:
    """Newest version, mask columns only. Does not decrypt."""
    return conn.execute(
        """
        SELECT id, partner_id, version, source, key_id, bank_code, account_last4,
               phone_last4, real_name_mask, holder_name_mask, holder_matches,
               created_at_ms, change_reason
        FROM partner_kyc
        WHERE partner_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (str(partner_id),),
    ).fetchone()


def _blobs(master: bytes, aad_for: Any, parsed: dict[str, Any]) -> dict[str, bytes]:
    return {field: _encrypt(master, aad_for(field), str(parsed[field])) for field in _FIELDS}


def _insert_version(
    conn: sqlite3.Connection,
    *,
    kyc_id: str,
    partner_id: str,
    version: int,
    source: str,
    parsed: dict[str, Any],
    blobs: dict[str, bytes],
    master: bytes,
    change_reason: str | None,
    now_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO partner_kyc (
            id, partner_id, version, source, key_id,
            real_name_enc, phone_enc, account_no_enc, holder_name_enc,
            bank_code, account_last4, phone_last4, real_name_mask, holder_name_mask,
            holder_matches, account_fp, phone_fp, change_reason, created_at_ms
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )
        """,
        (
            kyc_id,
            partner_id,
            version,
            source,
            KEY_ID,
            blobs["real_name"],
            blobs["phone"],
            blobs["account_no"],
            blobs["holder_name"],
            parsed["bank_code"],
            parsed["account_last4"],
            parsed["phone_last4"],
            parsed["real_name_mask"],
            parsed["holder_name_mask"],
            1 if parsed["holder_matches"] else 0,
            _fp(master, f"acct|{parsed['bank_code']}|{parsed['account_no']}"),
            _fp(master, f"phone|{parsed['phone']}"),
            change_reason,
            int(now_ms),
        ),
    )


def _duplicate_partner_ids(
    conn: sqlite3.Connection, partner_id: str, account_fp: str, phone_fp: str
) -> list[str]:
    rows = conn.execute(
        """
        SELECT k.partner_id
        FROM partner_kyc AS k
        WHERE k.partner_id != ?
          AND k.version = (
              SELECT MAX(version) FROM partner_kyc WHERE partner_id = k.partner_id
          )
          AND (k.account_fp = ? OR k.phone_fp = ?)
        """,
        (str(partner_id), account_fp, phone_fp),
    ).fetchall()
    seen: list[str] = []
    for row in rows:
        pid = str(row["partner_id"])
        if pid not in seen:
            seen.append(pid)
    return seen


def _public_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "real_name": parsed["real_name"],
        "phone": parsed["phone"],
        "bank_code": parsed["bank_code"],
        "account_no": parsed["account_no"],
        "holder_name": parsed["holder_name"],
        "holder_matches": bool(parsed["holder_matches"]),
        "account_length_warn": bool(parsed["account_length_warn"]),
        "real_name_mask": parsed["real_name_mask"],
        "holder_name_mask": parsed["holder_name_mask"],
        "phone_last4": parsed["phone_last4"],
        "account_last4": parsed["account_last4"],
    }


def stage_draft(conn: sqlite3.Connection, partner_id: str, form: str, now_ms: int) -> dict[str, Any]:
    """Portal only. Replaces any draft. Refuses a closed, admin, or already-saved partner."""
    pid = str(partner_id)
    try:
        with transaction(conn):
            _purge(conn, now_ms)
            row = _partner_row(conn, pid)
            if row is None:
                logger.info("partner kyc refused partner_id=%s reason=not_found", pid)
                return {"ok": False, "reason": "not_found"}
            if int(row["is_admin"] or 0) == 1:
                logger.info("partner kyc refused partner_id=%s reason=admin_exempt", pid)
                return {"ok": False, "reason": "admin_exempt"}
            if str(row["status"]) == "closed":
                logger.info("partner kyc refused partner_id=%s reason=closed", pid)
                return {"ok": False, "reason": "closed"}
            if kyc_state(conn, pid) == "saved":
                logger.info("partner kyc refused partner_id=%s reason=kyc_locked", pid)
                return {"ok": False, "reason": "kyc_locked"}
            parsed = parse_form(form)
            if not parsed.get("ok"):
                logger.info("partner kyc refused partner_id=%s reason=kyc_invalid", pid)
                return {"ok": False, "reason": "kyc_invalid", "errors": list(parsed.get("errors") or [])}
            try:
                master = _master_key()
                token = secrets.token_hex(4)
                blobs = _blobs(master, lambda field: _draft_aad(pid, token, field), parsed)
            except (KycKeyError, KycCryptoError):
                raise
            except Exception as exc:
                logger.warning("partner kyc encrypt failed partner_id=%s err=%s", pid, type(exc).__name__)
                return {"ok": False, "reason": "kyc_key"}
            conn.execute("DELETE FROM partner_kyc_draft WHERE partner_id = ?", (pid,))
            conn.execute(
                """
                INSERT INTO partner_kyc_draft (
                    partner_id, token, key_id,
                    real_name_enc, phone_enc, account_no_enc, holder_name_enc,
                    bank_code, account_last4, phone_last4, real_name_mask, holder_name_mask,
                    holder_matches, account_fp, phone_fp, created_at_ms, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    token,
                    KEY_ID,
                    blobs["real_name"],
                    blobs["phone"],
                    blobs["account_no"],
                    blobs["holder_name"],
                    parsed["bank_code"],
                    parsed["account_last4"],
                    parsed["phone_last4"],
                    parsed["real_name_mask"],
                    parsed["holder_name_mask"],
                    1 if parsed["holder_matches"] else 0,
                    _fp(master, f"acct|{parsed['bank_code']}|{parsed['account_no']}"),
                    _fp(master, f"phone|{parsed['phone']}"),
                    int(now_ms),
                    int(now_ms) + DRAFT_TTL_MS,
                ),
            )
            out = _public_parsed(parsed)
            out.update({"ok": True, "token": token})
            return out
    except (KycKeyError, KycCryptoError):
        raise


def commit_draft(conn: sqlite3.Connection, partner_id: str, token: str, now_ms: int) -> dict[str, Any]:
    """Copy one unexpired draft into version 1 and delete the draft. One transaction."""
    pid = str(partner_id)
    tok = str(token)
    with transaction(conn):
        _purge(conn, now_ms)
        row = _partner_row(conn, pid)
        if row is None:
            logger.info("partner kyc refused partner_id=%s reason=not_found", pid)
            return {"ok": False, "reason": "not_found"}
        if int(row["is_admin"] or 0) == 1:
            logger.info("partner kyc refused partner_id=%s reason=admin_exempt", pid)
            return {"ok": False, "reason": "admin_exempt"}
        if str(row["status"]) == "closed":
            logger.info("partner kyc refused partner_id=%s reason=closed", pid)
            return {"ok": False, "reason": "closed"}
        if kyc_state(conn, pid) == "saved":
            logger.info("partner kyc refused partner_id=%s reason=kyc_locked", pid)
            return {"ok": False, "reason": "kyc_locked"}
        draft = conn.execute(
            "SELECT * FROM partner_kyc_draft WHERE partner_id = ? AND token = ?",
            (pid, tok),
        ).fetchone()
        if draft is None or int(draft["expires_at_ms"]) <= int(now_ms):
            logger.info("partner kyc refused partner_id=%s reason=kyc_draft_expired", pid)
            return {"ok": False, "reason": "kyc_draft_expired"}
        master = _master_key()
        parsed: dict[str, Any] = {
            "bank_code": str(draft["bank_code"]),
            "account_last4": str(draft["account_last4"]),
            "phone_last4": str(draft["phone_last4"]),
            "real_name_mask": str(draft["real_name_mask"]),
            "holder_name_mask": str(draft["holder_name_mask"]),
            "holder_matches": int(draft["holder_matches"]) == 1,
        }
        plain: dict[str, str] = {}
        for field in _FIELDS:
            plain[field] = _decrypt(master, _draft_aad(pid, tok, field), draft[f"{field}_enc"])
        parsed["real_name"] = plain["real_name"]
        parsed["phone"] = plain["phone"]
        parsed["account_no"] = plain["account_no"]
        parsed["holder_name"] = plain["holder_name"]
        expected = 12 if parsed["bank_code"] in _TWELVE_DIGIT else 10
        parsed["account_length_warn"] = len(plain["account_no"]) != expected
        kyc_id = "pk_" + secrets.token_hex(8)
        blobs = _blobs(master, lambda field: _aad(kyc_id, field), parsed)
        _insert_version(
            conn,
            kyc_id=kyc_id,
            partner_id=pid,
            version=1,
            source="portal",
            parsed=parsed,
            blobs=blobs,
            master=master,
            change_reason=None,
            now_ms=now_ms,
        )
        conn.execute("DELETE FROM partner_kyc_draft WHERE partner_id = ?", (pid,))
        account_fp = _fp(master, f"acct|{parsed['bank_code']}|{parsed['account_no']}")
        phone_fp = _fp(master, f"phone|{parsed['phone']}")
        dupes = _duplicate_partner_ids(conn, pid, account_fp, phone_fp)
        logger.info("partner kyc saved partner_id=%s kyc_id=%s", pid, kyc_id)
        return {
            "ok": True,
            "kyc_id": kyc_id,
            "duplicate_partner_ids": dupes,
            "bank_code": parsed["bank_code"],
            "account_last4": parsed["account_last4"],
            "phone_last4": parsed["phone_last4"],
            "real_name_mask": parsed["real_name_mask"],
            "holder_name_mask": parsed["holder_name_mask"],
            "holder_matches": bool(parsed["holder_matches"]),
            "account_length_warn": bool(parsed["account_length_warn"]),
        }


def discard_draft(conn: sqlite3.Connection, partner_id: str, token: str | None = None) -> dict[str, Any]:
    """Delete the draft. A token deletes only that row, so a stale button cannot drop a newer one."""
    with transaction(conn):
        if token is None:
            cur = conn.execute(
                "DELETE FROM partner_kyc_draft WHERE partner_id = ?",
                (str(partner_id),),
            )
        else:
            cur = conn.execute(
                "DELETE FROM partner_kyc_draft WHERE partner_id = ? AND token = ?",
                (str(partner_id), str(token)),
            )
    return {"ok": True, "deleted": int(cur.rowcount)}


def admin_set_kyc(
    conn: sqlite3.Connection,
    partner_id: str,
    form: str,
    change_reason: str,
    now_ms: int,
) -> dict[str, Any]:
    """Append the next version. A reason of at least 10 characters is required after version 1."""
    pid = str(partner_id)
    with transaction(conn):
        _purge(conn, now_ms)
        row = _partner_row(conn, pid)
        if row is None:
            logger.info("partner kyc refused partner_id=%s reason=not_found", pid)
            return {"ok": False, "reason": "not_found"}
        if int(row["is_admin"] or 0) == 1:
            logger.info("partner kyc refused partner_id=%s reason=admin_exempt", pid)
            return {"ok": False, "reason": "admin_exempt"}
        version = _max_version(conn, pid)
        reason = str(change_reason or "").strip()
        if version >= 1 and len(reason) < 10:
            logger.info("partner kyc refused partner_id=%s reason=reason_short", pid)
            return {"ok": False, "reason": "reason_short"}
        parsed = parse_form(form)
        if not parsed.get("ok"):
            logger.info("partner kyc refused partner_id=%s reason=kyc_invalid", pid)
            return {"ok": False, "reason": "kyc_invalid", "errors": list(parsed.get("errors") or [])}
        master = _master_key()
        kyc_id = "pk_" + secrets.token_hex(8)
        blobs = _blobs(master, lambda field: _aad(kyc_id, field), parsed)
        _insert_version(
            conn,
            kyc_id=kyc_id,
            partner_id=pid,
            version=version + 1,
            source="admin",
            parsed=parsed,
            blobs=blobs,
            master=master,
            change_reason=reason or None,
            now_ms=now_ms,
        )
        account_fp = _fp(master, f"acct|{parsed['bank_code']}|{parsed['account_no']}")
        phone_fp = _fp(master, f"phone|{parsed['phone']}")
        logger.info("partner kyc saved partner_id=%s kyc_id=%s", pid, kyc_id)
        return {
            "ok": True,
            "kyc_id": kyc_id,
            "version": version + 1,
            "duplicate_partner_ids": _duplicate_partner_ids(conn, pid, account_fp, phone_fp),
            "bank_code": parsed["bank_code"],
            "account_last4": parsed["account_last4"],
            "phone_last4": parsed["phone_last4"],
            "real_name_mask": parsed["real_name_mask"],
            "holder_name_mask": parsed["holder_name_mask"],
            "holder_matches": bool(parsed["holder_matches"]),
            "account_length_warn": bool(parsed["account_length_warn"]),
        }


def reveal_current(conn: sqlite3.Connection, partner_id: str) -> dict[str, Any]:
    """Decrypt every version for the admin full view. Callers must not log the result."""
    pid = str(partner_id)
    rows = conn.execute(
        "SELECT * FROM partner_kyc WHERE partner_id = ? ORDER BY version ASC",
        (pid,),
    ).fetchall()
    if not rows:
        return {"ok": False, "reason": "none"}
    master = _master_key()
    versions: list[dict[str, Any]] = []
    for row in rows:
        kyc_id = str(row["id"])
        plain = {field: _decrypt(master, _aad(kyc_id, field), row[f"{field}_enc"]) for field in _FIELDS}
        expected = 12 if str(row["bank_code"]) in _TWELVE_DIGIT else 10
        versions.append(
            {
                "id": kyc_id,
                "version": int(row["version"]),
                "source": str(row["source"]),
                "created_at_ms": int(row["created_at_ms"]),
                "change_reason": row["change_reason"],
                "bank_code": str(row["bank_code"]),
                "real_name": plain["real_name"],
                "phone": plain["phone"],
                "account_no": plain["account_no"],
                "holder_name": plain["holder_name"],
                "holder_matches": int(row["holder_matches"]) == 1,
                "account_length_warn": len(plain["account_no"]) != expected,
                "real_name_mask": str(row["real_name_mask"]),
                "holder_name_mask": str(row["holder_name_mask"]),
                "phone_last4": str(row["phone_last4"]),
                "account_last4": str(row["account_last4"]),
            }
        )
    return {"ok": True, "partner_id": pid, "versions": versions}


def list_kyc_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    partners = conn.execute(
        "SELECT id, name, is_admin, status FROM partner ORDER BY name COLLATE NOCASE"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for partner in partners:
        pid = str(partner["id"])
        current = current_kyc(conn, pid)
        item: dict[str, Any] = {
            "partner_id": pid,
            "name": str(partner["name"]),
            "is_admin": int(partner["is_admin"] or 0) == 1,
            "status": str(partner["status"]),
            "state": "none" if current is None else "saved",
        }
        if current is not None:
            item["kyc_id"] = str(current["id"])
            item["bank_code"] = str(current["bank_code"])
            item["account_last4"] = str(current["account_last4"])
            item["holder_matches"] = int(current["holder_matches"]) == 1
            item["real_name_mask"] = str(current["real_name_mask"])
            item["holder_name_mask"] = str(current["holder_name_mask"])
            item["phone_last4"] = str(current["phone_last4"])
        out.append(item)
    return out


def purge_expired_drafts(conn: sqlite3.Connection, now_ms: int) -> None:
    with transaction(conn):
        _purge(conn, now_ms)


def deposit_kyc_line(conn: sqlite3.Connection, partner_id: str) -> str:
    row = current_kyc(conn, partner_id)
    if row is None:
        return "⚠️ ไม่มีข้อมูลรับเงิน"
    line = "ข้อมูลรับเงิน: " + mask_account(str(row["bank_code"]), str(row["account_last4"]))
    line += f" ({row['holder_name_mask']})"
    if int(row["holder_matches"]) != 1:
        line += "\n⚠️ ชื่อบัญชีไม่ตรงกับชื่อจริง"
    return line


def payout_account_line(
    conn: sqlite3.Connection,
    *,
    partner_id: str,
    payout_kyc_id: str | None,
    partner_name: str,
) -> str:
    """Masked account snapshotted on the payout, plus a warning when it is no longer current."""
    if not payout_kyc_id:
        current = current_kyc(conn, partner_id)
        if current is not None:
            masked = mask_account(str(current["bank_code"]), str(current["account_last4"]))
            return f"⚠️ ไม่มีข้อมูลรับเงินตอนยืนยัน — ปัจจุบัน: {masked} ตรวจ p kyc {partner_name} ก่อนโอน"
        return "⚠️ ไม่มีข้อมูลรับเงิน"
    row = conn.execute(
        """
        SELECT id, bank_code, account_last4, holder_name_mask
        FROM partner_kyc WHERE id = ?
        """,
        (str(payout_kyc_id),),
    ).fetchone()
    if row is None:
        return "⚠️ ไม่มีข้อมูลรับเงิน"
    line = "โอนเข้า: " + mask_account(str(row["bank_code"]), str(row["account_last4"]))
    line += f" ({row['holder_name_mask']})"
    current = current_kyc(conn, partner_id)
    if current is None or str(current["id"]) != str(row["id"]):
        line += f"\n⚠️ บัญชีรับเงินเปลี่ยนหลังยืนยัน — ตรวจ p kyc {partner_name} ก่อนโอน"
    return line


def partner_payout_line(
    conn: sqlite3.Connection,
    *,
    payout_kyc_id: str | None,
    cash: Decimal,
) -> str:
    """Partner-facing mask. Empty when there is no snapshot or the cash is not positive."""
    if cash <= 0 or not payout_kyc_id:
        return ""
    row = conn.execute(
        """
        SELECT bank_code, account_last4, holder_name_mask
        FROM partner_kyc WHERE id = ?
        """,
        (str(payout_kyc_id),),
    ).fetchone()
    if row is None:
        return ""
    account = mask_account(str(row["bank_code"]), str(row["account_last4"]))
    return f"โอนเข้า: {account} ({row['holder_name_mask']})"


_KYC_LEGACY = "⚠️ ยังไม่ได้กรอกข้อมูลรับเงิน — ส่งฟอร์มเมื่อสะดวก"


def set_draft_confirm_message_id(
    conn: sqlite3.Connection, partner_id: str, token: str, message_id: int
) -> bool:
    """Store the confirm message id on the matching draft only."""
    with transaction(conn):
        cur = conn.execute(
            """
            UPDATE partner_kyc_draft
            SET confirm_message_id = ?
            WHERE partner_id = ? AND token = ?
            """,
            (int(message_id), str(partner_id), str(token)),
        )
        updated = int(cur.rowcount) == 1
    if not updated:
        logger.info("partner kyc refused partner_id=%s reason=draft_missing", partner_id)
    return updated


def draft_confirm_message_id(conn: sqlite3.Connection, partner_id: str) -> int | None:
    """The stored confirm message id, or None when there is no draft or no id."""
    row = conn.execute(
        """
        SELECT confirm_message_id
        FROM partner_kyc_draft
        WHERE partner_id = ?
        """,
        (str(partner_id),),
    ).fetchone()
    if row is None or row["confirm_message_id"] is None:
        return None
    return int(row["confirm_message_id"])


def card_kyc_line(conn: sqlite3.Connection, partner_id: str) -> str:
    row = current_kyc(conn, partner_id)
    if row is None:
        return "ข้อมูลรับเงิน: ยังไม่กรอก"
    account = mask_account(str(row["bank_code"]), str(row["account_last4"]))
    return f"ข้อมูลรับเงิน: {row['real_name_mask']} · {account}"
