from jarvise_ingest.indicators import atr, ema, indicator_series, rsi, warm_from


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


def test_warm_from_counts_bars_until_the_seed_is_forgotten():
    # EMA-200 seeds at index 199 and needs 461 more bars before the seed
    # contributes under 1% of the value.
    assert warm_from(199, 2 / 201) == 660


def test_indicator_series_withholds_values_that_still_carry_their_seed():
    closes = [100.0 + i * 0.5 for i in range(700)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]

    series = indicator_series(highs, lows, closes)

    assert series["atr_14"][75] is None
    assert series["atr_14"][76] is not None
    assert series["ema_200"][659] is None
    assert series["ema_200"][660] is not None


def test_indicator_series_keys_match_the_stored_columns():
    closes = [100.0 + i for i in range(30)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]

    series = indicator_series(highs, lows, closes)

    assert set(series) == {"atr_14", "rsi_14", "ema_20", "ema_200"}
    assert all(len(values) == 30 for values in series.values())
