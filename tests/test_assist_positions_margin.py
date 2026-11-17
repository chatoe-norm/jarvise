"""Owner open-positions margin line."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import httpx
from tests.test_assist import FakeFutures

from jarvise_assist.futures import BinanceFuturesClient, FuturesPosition, SymbolFilters
from jarvise_assist.menu import render_open_positions
from jarvise_assist.partner_positions import render_partner_positions, reset_positions_cache
from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_ingest.db import open_db

D = Decimal


def test_position_risk_parses_margin_fields() -> None:
    rows = [
        {
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "0.05",
            "entryPrice": "100",
            "markPrice": "101",
            "updateTime": 1,
            "isolatedWallet": "2280.8625",
            "positionInitialMargin": "2287.86",
            "marginAsset": "USDT",
        },
        {
            "symbol": "ETHUSDT",
            "positionSide": "BOTH",
            "positionAmt": "1",
            "entryPrice": "2000",
            "markPrice": "2010",
            "updateTime": 2,
            "isolatedWallet": "0",
            "positionInitialMargin": "1045.2",
            "marginAsset": "USDT",
        },
        {
            "symbol": "SOLUSDT",
            "positionSide": "BOTH",
            "positionAmt": "2",
            "entryPrice": "100",
            "markPrice": "101",
            "updateTime": 3,
        },
        {
            "symbol": "BNBUSDT",
            "positionSide": "BOTH",
            "positionAmt": "3",
            "entryPrice": "500",
            "markPrice": "501",
            "updateTime": 4,
            "isolatedWallet": "abc",
            "positionInitialMargin": "",
            "marginAsset": "USDT",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert urlparse(str(request.url)).path == "/fapi/v3/positionRisk"
        return httpx.Response(200, content=json.dumps(rows))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    client = BinanceFuturesClient(auth, client=http, base_url="https://fapi.binance.com")
    positions = client.position_risk()
    http.close()

    assert len(positions) == 4
    isolated, cross, missing, garbage = positions

    assert isolated.isolated_wallet == D("2280.8625")
    assert isolated.position_initial_margin == D("2287.86")
    assert isolated.margin_asset == "USDT"

    assert cross.isolated_wallet == D("0")
    assert cross.position_initial_margin == D("1045.2")
    assert cross.margin_asset == "USDT"

    assert missing.isolated_wallet is None
    assert missing.position_initial_margin is None
    assert missing.margin_asset is None

    assert garbage.isolated_wallet is None
    assert garbage.position_initial_margin is None
    assert garbage.margin_asset == "USDT"


def _pos(
    *,
    symbol: str = "BTCUSDT",
    position_side: str = "BOTH",
    amt: str = "0.05",
    entry: str = "100",
    mark: str = "101",
    isolated_wallet: str | None = None,
    position_initial_margin: str | None = None,
    margin_asset: str | None = None,
) -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        position_side=position_side,
        position_amt=D(amt),
        entry_price=D(entry),
        mark_price=D(mark),
        update_time_ms=1,
        isolated_wallet=D(isolated_wallet) if isolated_wallet is not None else None,
        position_initial_margin=(D(position_initial_margin) if position_initial_margin is not None else None),
        margin_asset=margin_asset,
    )


def test_render_isolated_margin(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "iso.db")
    fake = FakeFutures(
        positions=[_pos(isolated_wallet="2280.8625", margin_asset="USDT")],
        ticks={"BTCUSDT": D("1")},
        steps={"BTCUSDT": D("0.001")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=1)
    assert "• LONG · qty 0.05 · margin 2,280.86 USDT" in text
    conn.close()


def test_render_isolated_rounds_half_up(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "half.db")
    fake = FakeFutures(
        positions=[_pos(isolated_wallet="1234.565", margin_asset="USDT")],
        ticks={"BTCUSDT": D("1")},
        steps={"BTCUSDT": D("0.001")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=1)
    assert "margin 1,234.57 USDT" in text
    conn.close()


def test_render_cross_margin(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "cross.db")
    fake = FakeFutures(
        positions=[
            _pos(
                isolated_wallet="0",
                position_initial_margin="1045.2",
                margin_asset="USDT",
            )
        ],
        ticks={"BTCUSDT": D("1")},
        steps={"BTCUSDT": D("0.001")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=1)
    assert "margin ≈ 1,045.20 USDT (cross)" in text
    conn.close()


def test_render_missing_margin(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "miss.db")
    fake = FakeFutures(
        positions=[_pos()],
        ticks={"BTCUSDT": D("1")},
        steps={"BTCUSDT": D("0.001")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=1)
    assert "margin —" in text
    conn.close()


def test_render_non_universe_margin(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "out.db")
    fake = FakeFutures(
        positions=[_pos(symbol="PEPEUSDT", amt="1000")],
        ticks={"PEPEUSDT": D("0.00000001")},
        steps={"PEPEUSDT": D("1")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=1)
    assert "• LONG · qty 1000 · margin — · no data, no advice" in text
    conn.close()


def test_render_hedge_long_short_own_wallets(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "hedge.db")
    long_pos = _pos(
        position_side="LONG",
        amt="0.05",
        isolated_wallet="2280.8625",
        margin_asset="USDT",
    )
    short_pos = _pos(
        position_side="SHORT",
        amt="-0.02",
        isolated_wallet="900.5",
        margin_asset="USDT",
    )
    fake = FakeFutures(
        positions=[long_pos, short_pos],
        ticks={"BTCUSDT": D("1")},
        steps={"BTCUSDT": D("0.001")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=1)
    assert "• LONG · qty 0.05 · margin 2,280.86 USDT" in text
    assert "• SHORT · qty 0.02 · margin 900.50 USDT" in text
    conn.close()


def test_render_margin_asset_usdc(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "usdc.db")
    fake = FakeFutures(
        positions=[_pos(isolated_wallet="100.25", margin_asset="USDC")],
        ticks={"BTCUSDT": D("1")},
        steps={"BTCUSDT": D("0.001")},
    )
    text = render_open_positions(conn, futures=fake, now_ms=1)
    assert "margin 100.25 USDC" in text
    conn.close()


def test_render_futures_read_error(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "err.db")
    text = render_open_positions(
        conn,
        futures=FakeFutures(read_error=RuntimeError("boom")),
        now_ms=1,
    )
    assert text == "อ่าน futures ไม่ได้"
    conn.close()


def test_render_no_positions(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "empty.db")
    text = render_open_positions(conn, futures=FakeFutures(positions=[]), now_ms=1)
    assert text == "ไม่มีไม้ที่ถืออยู่"
    conn.close()


def test_partner_view_hides_margin(tmp_path: Path) -> None:
    reset_positions_cache()
    conn = open_db(tmp_path / "partner.db")

    class FakePosClient:
        def position_risk(self, *, symbol: str | None = None) -> list[FuturesPosition]:
            return [
                FuturesPosition(
                    symbol="BTCUSDT",
                    position_side="LONG",
                    position_amt=D("0.01"),
                    entry_price=D("100"),
                    mark_price=D("110"),
                    update_time_ms=1,
                    isolated_wallet=D("2280.8625"),
                    margin_asset="USDT",
                )
            ]

        def symbol_filters(self, symbol: str) -> SymbolFilters:
            return SymbolFilters(
                step_size=D("0.001"),
                min_qty=D("0"),
                min_notional=D("5"),
                tick_size=D("0.1"),
            )

    text = render_partner_positions(conn, FakePosClient(), now_ms=1)
    # Symbol may end in USDT; forbid the margin label and amount/unit copy.
    assert "margin" not in text.lower()
    assert " USDT" not in text
    assert "2,280" not in text
    conn.close()
    reset_positions_cache()
