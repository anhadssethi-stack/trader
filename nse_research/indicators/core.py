"""Technical indicators as plain functions on pandas Series. No look-ahead: every
value at row t uses only data up to and including t."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (RMA), seeded with the simple mean of the first n values."""
    s = s.astype(float)
    out = pd.Series(np.nan, index=s.index)
    vals = s.to_numpy(dtype=float, copy=True)
    valid = np.where(~np.isnan(vals))[0]
    if len(valid) < n:
        return out
    first = valid[0]
    seed_end = first + n
    if seed_end > len(vals):
        return out
    prev = np.nanmean(vals[first:seed_end])
    res = np.full(len(vals), np.nan)
    res[seed_end - 1] = prev
    for i in range(seed_end, len(vals)):
        v = vals[i]
        if not np.isnan(v):
            prev = (prev * (n - 1) + v) / n
        res[i] = prev
    return pd.Series(res, index=s.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0), n)
    loss = wilder((-delta).clip(lower=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    out[(loss == 0) & gain.notna()] = 100.0
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    return pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1, skipna=False) \
        .fillna(high - low)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return wilder(true_range(high, low, close), n)


def relative_strength(close: pd.Series, bench: pd.Series, n: int) -> pd.Series:
    """Excess return over the benchmark across n bars, in % points.
    E.g. +8 means the stock beat the index by 8 percentage points over n days."""
    return (close / close.shift(n) - 1) * 100 - (bench / bench.shift(n) - 1) * 100


def add_indicators(df: pd.DataFrame, cfg: dict, bench: pd.Series | None = None) -> pd.DataFrame:
    """df: one symbol, sorted by date, columns open/high/low/close/volume[/deliv_pct/turnover].
    bench: benchmark close indexed by date (aligned inside)."""
    out = df.copy().reset_index(drop=True)
    c = out["close"]
    for n in cfg["dma_periods"]:
        out[f"dma{n}"] = sma(c, n)
    out["rsi"] = rsi(c, cfg["rsi_period"])
    f, s, g = cfg["macd"]
    out = pd.concat([out, macd(c, f, s, g)], axis=1)
    out["atr"] = atr(out["high"], out["low"], c, cfg["atr_period"])
    out["atr_pct"] = out["atr"] / c * 100
    nv = cfg["volume_avg_days"]
    # average of the *previous* n days, so today's spike doesn't dilute its own baseline
    out["vol_avg"] = out["volume"].shift(1).rolling(nv, min_periods=nv).mean()
    out["vol_ratio"] = out["volume"] / out["vol_avg"]
    if "turnover" in out:
        out["turnover_avg_cr"] = out["turnover"].rolling(nv, min_periods=nv).mean() / 1e7
    for n in cfg["breakout_lookbacks"]:
        out[f"high{n}"] = out["high"].shift(1).rolling(n, min_periods=n).max()
        out[f"low{n}"] = out["low"].shift(1).rolling(n, min_periods=n).min()
    if "deliv_pct" in out:
        nd = cfg["delivery_avg_days"]
        out["deliv_avg"] = out["deliv_pct"].shift(1).rolling(nd, min_periods=max(5, nd // 2)).mean()
    if bench is not None and len(bench):
        b = bench.reindex(pd.to_datetime(out["date"])).ffill().to_numpy()
        out["bench_close"] = b
        for n in cfg["rs_lookbacks"]:
            out[f"rs{n}"] = relative_strength(c, out["bench_close"], n)
        out["rs_line"] = c / out["bench_close"]
    return out
