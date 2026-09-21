from jarvise_ingest.indicators import atr, ema, enrich_candles, rsi


def test_ema_known_seed():
    values = [float(i) for i in range(1, 11)]
    out = ema(values, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == (1 + 2 + 3) / 3
    # next = (4 - prev) * (2/4) + prev
    prev = out[2]
    expected = (4 - prev) * 0.5 + prev
    assert abs(out[3] - expected) < 1e-9


def test_rsi_constant_uptrend_high():
    closes = [100.0 + i for i in range(20)]
    out = rsi(closes, 14)
    assert out[14] is not None
    assert out[14] > 70


def test_atr_positive():
    highs = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
    lows = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    closes = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5, 20.5, 21.5, 22.5, 23.5]
    out = atr(highs, lows, closes, 14)
    assert out[13] is not None
    assert out[13] > 0
    assert out[14] is not None


def test_enrich_candles_adds_fields():
    candles = [
        {
            "symbol": "BTCUSDT",
            "timestamp": 1_000 + i,
            "timeframe": "1h",
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100.5 + i,
            "volume": 10.0,
        }
        for i in range(30)
    ]
    enrich_candles(candles)
    assert candles[-1]["atr_14"] is not None
    assert candles[-1]["rsi_14"] is not None
    assert candles[-1]["ema_20"] is not None
    assert candles[0]["ema_200"] is None
