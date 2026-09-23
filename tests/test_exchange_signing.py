"""Ed25519 / RSA / HMAC signing and auth resolution tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from jarvise_exchange.binance_spot import (
    BinanceAuth,
    BinanceSpotClient,
    resolve_binance_auth,
    sign_payload,
    sign_query,
    signature_for_query,
)


def test_sign_query_known_fixture():
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1"
        "&price=0.1&recvWindow=5000&timestamp=1499827319559"
    )
    expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert sign_query(secret, query) == expected


def _write_ed25519_pem(path: Path) -> ed25519.Ed25519PrivateKey:
    key = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def _write_rsa_pem(path: Path) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def test_ed25519_sign_payload_verifiable(tmp_path: Path):
    pem = tmp_path / "ed.pem"
    private = _write_ed25519_pem(pem)
    auth = BinanceAuth(api_key="k", private_key=private)
    payload = "timestamp=1499827319559"
    sig_b64 = sign_payload(auth, payload)
    raw = base64.b64decode(sig_b64)
    private.public_key().verify(raw, payload.encode("ASCII"))
    from urllib.parse import quote

    assert signature_for_query(auth, payload) == quote(sig_b64, safe="")


def test_rsa_sign_payload_verifiable(tmp_path: Path):
    pem = tmp_path / "rsa.pem"
    private = _write_rsa_pem(pem)
    auth = BinanceAuth(api_key="k", private_key=private)
    payload = "timestamp=1499827319559"
    sig_b64 = sign_payload(auth, payload)
    private.public_key().verify(
        base64.b64decode(sig_b64),
        payload.encode("ASCII"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_resolve_prefers_private_key_path(tmp_path: Path, monkeypatch):
    pem = tmp_path / "ed.pem"
    _write_ed25519_pem(pem)
    monkeypatch.setenv("BINANCE_API_KEY", "api-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "hmac-secret")
    monkeypatch.setenv("BINANCE_API_PRIVATE_KEY_PATH", str(pem))
    auth = resolve_binance_auth()
    assert auth is not None
    assert auth.hmac_secret is None
    assert auth.private_key is not None


def test_resolve_hmac_when_no_private_path(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "api-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "hmac-secret")
    monkeypatch.delenv("BINANCE_API_PRIVATE_KEY_PATH", raising=False)
    auth = resolve_binance_auth()
    assert auth is not None
    assert auth.hmac_secret == "hmac-secret"
    assert auth.private_key is None


def test_resolve_none_without_key_material(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "api-key")
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_API_PRIVATE_KEY_PATH", raising=False)
    assert resolve_binance_auth() is None


def test_resolve_missing_file_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "api-key")
    monkeypatch.setenv("BINANCE_API_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    with pytest.raises(FileNotFoundError):
        resolve_binance_auth()


def test_client_url_encodes_asymmetric_signature(tmp_path: Path):
    pem = tmp_path / "ed.pem"
    private = _write_ed25519_pem(pem)
    auth = BinanceAuth(api_key="api-key", private_key=private)
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            content=json.dumps(
                {"balances": [{"asset": "BTC", "free": "1", "locked": "0"}]}
            ),
        )

    client = BinanceSpotClient(
        auth, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    rows = client.list_spot_balances()
    assert len(rows) == 1
    parsed = urlparse(captured["url"])
    qs = parse_qs(parsed.query)
    sig = qs["signature"][0]
    # Base64 often includes + or = which must appear URL-encoded in the raw URL
    assert "signature=" in captured["url"]
    raw_sig = unquote(sig)
    private.public_key().verify(
        base64.b64decode(raw_sig),
        f"timestamp={qs['timestamp'][0]}".encode("ASCII"),
    )
