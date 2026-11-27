import numpy as np
import pandas as pd
import pytest

from nse_research.indicators.core import add_indicators, atr, macd, relative_strength, rsi, sma, wilder
from nse_research.indicators.levels import find_levels, nearest_levels

from .conftest import make_ohlc


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = sma(s, 3)
    assert out.isna().sum() == 2 and out.iloc[-1] == 4


def test_wilder_matches_recursive_definition():
    s = pd.Series(np.arange(1, 21, dtype=float))
    w = wilder(s, 5)
    assert w.iloc[3] != w.iloc[3]  # NaN before the seed
    assert w.iloc[4] == pytest.approx(3.0)  # mean of 1..5
    assert w.iloc[5] == pytest.approx((3.0 * 4 + 6) / 5)


def test_rsi_extremes_and_range():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert rsi(up, 14).iloc[-1] == 100
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert rsi(down, 14).iloc[-1] == pytest.approx(0)
    rnd = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 300)))
    r = rsi(rnd, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_rsi_known_value():
    # Equal up and down moves of the same size -> RSI 50
    s = pd.Series([10, 11] * 30, dtype=float)
    assert rsi(s, 14).iloc[-1] == pytest.approx(50, abs=4)


def test_macd_zero_for_constant_price():
    m = macd(pd.Series(np.full(100, 50.0)))
    assert m["macd"].dropna().abs().max() == pytest.approx(0)


def test_atr_constant_range():
    n = 50
    close = pd.Series(np.full(n, 100.0))
    a = atr(close + 2, close - 2, close, 14)
    assert a.iloc[-1] == pytest.approx(4.0)


def test_relative_strength():
    stock = pd.Series([100.0, 110.0])
    bench = pd.Series([100.0, 105.0])
    assert relative_strength(stock, bench, 1).iloc[-1] == pytest.approx(5.0)


def test_add_indicators_no_lookahead(cfg):
    df = make_ohlc(100 + np.cumsum(np.random.default_rng(2).normal(0, 1, 300)))
    full = add_indicators(df, cfg)
    cut = add_indicators(df.iloc[:250], cfg)
    cols = ["dma20", "dma50", "dma200", "rsi", "macd", "atr", "vol_ratio", "high20"]
    pd.testing.assert_frame_equal(full.loc[:249, cols], cut[cols])


def test_volume_ratio_uses_prior_days(cfg):
    df = make_ohlc(np.full(40, 100.0))
    df.loc[39, "volume"] = 3e5
    out = add_indicators(df, cfg)
    assert out["vol_ratio"].iloc[-1] == pytest.approx(3.0)


def test_levels_find_repeated_pivots():
    # oscillate between ~90 and ~110 so both levels get several touches
    x = np.arange(200)
    close = 100 + 10 * np.sin(x / 5)
    df = make_ohlc(close)
    df["close"] = df["close"].values
    df.loc[df.index[-1], "close"] = 100.0
    levels = find_levels(df, window=3, lookback=200, cluster_atr=0.5, atr_value=2.0)
    sup, res = nearest_levels(levels, 100.0)
    assert sup is not None and res is not None
    assert sup.price == pytest.approx(89.1, abs=1.5) and sup.touches >= 3
    assert res.price == pytest.approx(111.1, abs=1.5) and res.touches >= 3
