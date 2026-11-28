"""Tradeable setups for the backtester.

A scanner *signal* describes a state ("in an uptrend"). A *setup* is an event that
opens a trade on a specific day ("became an uptrend today"). Each setup below is a
vectorised version of one or more scanner rules, using the same thresholds from
settings.yaml, so what the backtest measures is what the scanner shows.

Every setup is evaluated on the close of day t using only data up to t. The trade
is entered at the open of day t+1 (see engine.py), so there is no look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd


@dataclass(frozen=True)
class Setup:
    id: str
    name: str
    side: str                      # "long" or "short"
    logic: str                     # plain-English rule shown in the app
    fn: Callable[[pd.DataFrame, dict], pd.Series]

    @property
    def needs_fno(self) -> bool:
        return self.side == "short"


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def _dmas(cfg):
    return [f"dma{n}" for n in cfg["dma_periods"]]


def _stack_up(d, cfg):
    a, b, c = _dmas(cfg)
    return (d["close"] > d[a]) & (d[a] > d[b]) & (d[b] > d[c])


def _stack_down(d, cfg):
    a, b, c = _dmas(cfg)
    return (d["close"] < d[a]) & (d[a] < d[b]) & (d[b] < d[c])


def _up_day(d):
    return d["close"] > d["close"].shift(1)


def _down_day(d):
    return d["close"] < d["close"].shift(1)


def _breakout(d, cfg, n):
    return (d["close"] > d[f"high{n}"]) & (d["vol_ratio"] >= cfg["breakout_min_volume_ratio"])


def _breakdown(d, cfg, n):
    return (d["close"] < d[f"low{n}"]) & (d["vol_ratio"] >= cfg["breakout_min_volume_ratio"])


def _high_delivery(d, cfg):
    if "deliv_pct" not in d or "deliv_avg" not in d:
        return pd.Series(False, index=d.index)
    return (d["deliv_pct"] >= cfg["delivery_min_pct"]) & (d["deliv_pct"] >= cfg["delivery_vs_avg_ratio"] * d["deliv_avg"])


def _rs_strong(d, cfg):
    cols = [f"rs{n}" for n in cfg["rs_lookbacks"]]
    if not all(c in d for c in cols):
        return pd.Series(False, index=d.index)
    out = pd.Series(True, index=d.index)
    for c in cols:
        out &= d[c] > 0
    return out


def _long_n(cfg):
    return max(cfg["breakout_lookbacks"])


def _short_n(cfg):
    return min(cfg["breakout_lookbacks"])


SETUPS: list[Setup] = [
    # ---------------------------------------------------------------- long
    Setup("L_breakout_long", "Long-term breakout on volume", "long",
          "Close above the prior 55-day high, on volume ≥ 1.5× its 20-day average.",
          lambda d, c: _breakout(d, c, _long_n(c))),
    Setup("L_breakout_short", "Short-term breakout on volume", "long",
          "Close above the prior 20-day high, on volume ≥ 1.5× its 20-day average.",
          lambda d, c: _breakout(d, c, _short_n(c))),
    Setup("L_breakout_rs", "Breakout while outperforming", "long",
          "20-day breakout on volume, and the stock beat the Nifty 500 over both 63 and 126 days.",
          lambda d, c: _breakout(d, c, _short_n(c)) & _rs_strong(d, c)),
    Setup("L_trend_start", "Uptrend begins", "long",
          "Today close > 20 > 50 > 200 DMA for the first time (it was not stacked yesterday).",
          lambda d, c: _stack_up(d, c) & ~_stack_up(d, c).shift(1, fill_value=False)),
    Setup("L_golden_cross", "Golden cross", "long",
          "50 DMA crosses above the 200 DMA today.",
          lambda d, c: _cross_up(d[_dmas(c)[1]], d[_dmas(c)[2]])),
    Setup("L_macd_uptrend", "MACD bullish cross above 200 DMA", "long",
          "MACD crosses above its signal line today while the close is above the 200 DMA.",
          lambda d, c: _cross_up(d["macd"], d["macd_signal"]) & (d["close"] > d[_dmas(c)[2]])),
    Setup("L_rsi_50_uptrend", "RSI reclaims 50 in an uptrend", "long",
          "RSI crosses above 50 today while the close is above the 200 DMA.",
          lambda d, c: _cross_up(d["rsi"], pd.Series(50.0, index=d.index)) & (d["close"] > d[_dmas(c)[2]])),
    Setup("L_volume_up", "Volume spike on an up day", "long",
          "Volume ≥ 2× its 20-day average, close up on the day, close above the 50 DMA.",
          lambda d, c: (d["vol_ratio"] >= c["volume_spike_ratio"]) & _up_day(d) & (d["close"] > d[_dmas(c)[1]])),
    Setup("L_delivery_up", "High delivery on an up day", "long",
          "Delivery ≥ 50% and ≥ 1.2× its 20-day average, close up on the day, close above the 50 DMA.",
          lambda d, c: _high_delivery(d, c) & _up_day(d) & (d["close"] > d[_dmas(c)[1]])),
    # ---------------------------------------------------------------- short (F&O stocks only)
    Setup("S_breakdown_long", "Long-term breakdown on volume", "short",
          "Close below the prior 55-day low, on volume ≥ 1.5× its 20-day average. F&O stocks only.",
          lambda d, c: _breakdown(d, c, _long_n(c))),
    Setup("S_breakdown_short", "Short-term breakdown on volume", "short",
          "Close below the prior 20-day low, on volume ≥ 1.5× its 20-day average. F&O stocks only.",
          lambda d, c: _breakdown(d, c, _short_n(c))),
    Setup("S_trend_start", "Downtrend begins", "short",
          "Today close < 20 < 50 < 200 DMA for the first time. F&O stocks only.",
          lambda d, c: _stack_down(d, c) & ~_stack_down(d, c).shift(1, fill_value=False)),
    Setup("S_death_cross", "Death cross", "short",
          "50 DMA crosses below the 200 DMA today. F&O stocks only.",
          lambda d, c: _cross_down(d[_dmas(c)[1]], d[_dmas(c)[2]])),
    Setup("S_macd_downtrend", "MACD bearish cross below 200 DMA", "short",
          "MACD crosses below its signal line today while the close is below the 200 DMA. F&O stocks only.",
          lambda d, c: _cross_down(d["macd"], d["macd_signal"]) & (d["close"] < d[_dmas(c)[2]])),
    Setup("S_volume_down", "Volume spike on a down day", "short",
          "Volume ≥ 2× its 20-day average, close down on the day, close below the 50 DMA. F&O stocks only.",
          lambda d, c: (d["vol_ratio"] >= c["volume_spike_ratio"]) & _down_day(d) & (d["close"] < d[_dmas(c)[1]])),
]

SETUP_BY_ID = {s.id: s for s in SETUPS}


def setup_signals(d: pd.DataFrame, cfg: dict, setups: list[Setup] | None = None) -> pd.DataFrame:
    """Boolean frame: one column per setup, True on days the setup fires."""
    out = {}
    for s in setups or SETUPS:
        out[s.id] = s.fn(d, cfg).fillna(False).astype(bool)
    return pd.DataFrame(out, index=d.index)
