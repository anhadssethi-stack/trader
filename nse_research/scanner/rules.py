"""Scanner rules. Each rule looks at the indicator frame of one stock and, if it
fires, returns a Signal with a plain-English reason showing the exact numbers.
There is no hidden score: the dashboard lists the signals that fired and why.

To add a rule: write a function `(d, cfg) -> Signal | list[Signal] | None` and add it to RULES.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class Signal:
    id: str
    label: str
    side: str      # "bullish", "bearish" or "info"
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _f(x: float, nd: int = 2) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{nd}f}"


def _ok(*vals) -> bool:
    return all(v is not None and not (isinstance(v, float) and np.isnan(v)) for v in vals)


def trend_stack(d: pd.DataFrame, cfg: dict):
    r = d.iloc[-1]
    a, b, c = (f"dma{n}" for n in cfg["dma_periods"])
    if not _ok(r[a], r[b], r[c]):
        return None
    nums = f"close {_f(r.close)}, {a.upper()} {_f(r[a])}, {b.upper()} {_f(r[b])}, {c.upper()} {_f(r[c])}"
    if r.close > r[a] > r[b] > r[c]:
        return Signal("trend_up", "Uptrend (DMAs stacked)", "bullish", f"Close > 20 > 50 > 200 DMA: {nums}")
    if r.close < r[a] < r[b] < r[c]:
        return Signal("trend_down", "Downtrend (DMAs stacked)", "bearish", f"Close < 20 < 50 < 200 DMA: {nums}")
    return None


def dma_cross(d: pd.DataFrame, cfg: dict):
    _, b, c = (f"dma{n}" for n in cfg["dma_periods"])
    n = cfg["cross_lookback_days"]
    w = d.tail(n + 1)
    if len(w) < n + 1 or w[[b, c]].isna().any().any():
        return None
    diff = (w[b] - w[c]).to_numpy()
    last = d.iloc[-1]
    for i in range(len(diff) - 1, 0, -1):
        if diff[i - 1] <= 0 < diff[i] and diff[-1] > 0:
            when = pd.Timestamp(w["date"].iloc[i]).strftime("%d %b")
            return Signal("golden_cross", "Golden cross (50 over 200 DMA)", "bullish",
                          f"50 DMA crossed above 200 DMA on {when}; now {_f(last[b])} vs {_f(last[c])}")
        if diff[i - 1] >= 0 > diff[i] and diff[-1] < 0:
            when = pd.Timestamp(w["date"].iloc[i]).strftime("%d %b")
            return Signal("death_cross", "Death cross (50 under 200 DMA)", "bearish",
                          f"50 DMA crossed below 200 DMA on {when}; now {_f(last[b])} vs {_f(last[c])}")
    return None


def rsi_state(d: pd.DataFrame, cfg: dict):
    if len(d) < 2:
        return None
    r, p = d.iloc[-1], d.iloc[-2]
    if not _ok(r.rsi, p.rsi):
        return None
    lo, hi = cfg["rsi_bull_zone"]
    blo, bhi = cfg["rsi_bear_zone"]
    out = []
    if r.rsi >= cfg["rsi_overbought"]:
        out.append(Signal("rsi_overbought", "RSI overbought", "info",
                          f"RSI {_f(r.rsi, 1)} ≥ {cfg['rsi_overbought']}: strong, but stretched"))
    elif r.rsi <= cfg["rsi_oversold"]:
        out.append(Signal("rsi_oversold", "RSI oversold", "info",
                          f"RSI {_f(r.rsi, 1)} ≤ {cfg['rsi_oversold']}: weak, but stretched"))
    if lo <= r.rsi < hi and r.rsi > p.rsi:
        out.append(Signal("rsi_bull", "RSI in bullish zone, rising", "bullish",
                          f"RSI {_f(r.rsi, 1)} (was {_f(p.rsi, 1)}) inside {lo}–{hi}"))
    elif blo < r.rsi <= bhi and r.rsi < p.rsi:
        out.append(Signal("rsi_bear", "RSI in bearish zone, falling", "bearish",
                          f"RSI {_f(r.rsi, 1)} (was {_f(p.rsi, 1)}) inside {blo}–{bhi}"))
    return out


def macd_cross(d: pd.DataFrame, cfg: dict):
    n = cfg["macd_cross_lookback_days"]
    w = d.tail(n + 1)
    if len(w) < n + 1 or w[["macd", "macd_signal"]].isna().any().any():
        return None
    h = (w["macd"] - w["macd_signal"]).to_numpy()
    r = d.iloc[-1]
    for i in range(len(h) - 1, 0, -1):
        when = pd.Timestamp(w["date"].iloc[i]).strftime("%d %b")
        if h[i - 1] <= 0 < h[i] and h[-1] > 0:
            zone = "above" if r.macd > 0 else "below"
            return Signal("macd_bull", "MACD bullish cross", "bullish",
                          f"MACD crossed above signal on {when} ({zone} zero line): "
                          f"MACD {_f(r.macd)}, signal {_f(r.macd_signal)}")
        if h[i - 1] >= 0 > h[i] and h[-1] < 0:
            zone = "above" if r.macd > 0 else "below"
            return Signal("macd_bear", "MACD bearish cross", "bearish",
                          f"MACD crossed below signal on {when} ({zone} zero line): "
                          f"MACD {_f(r.macd)}, signal {_f(r.macd_signal)}")
    return None


def breakouts(d: pd.DataFrame, cfg: dict):
    r = d.iloc[-1]
    vr, need = r.get("vol_ratio"), cfg["breakout_min_volume_ratio"]
    out = []
    # report only the longest lookback that fired, to avoid listing the same move twice
    for n in sorted(cfg["breakout_lookbacks"], reverse=True):
        hi, lo = r.get(f"high{n}"), r.get(f"low{n}")
        if not _ok(hi, lo, vr):
            continue
        if r.close > hi and vr >= need:
            out.append(Signal(f"breakout_{n}", f"{n}-day breakout on volume", "bullish",
                              f"Close {_f(r.close)} > prior {n}-day high {_f(hi)}; volume {_f(vr, 1)}× 20-day avg (need {need}×)"))
            break
    for n in sorted(cfg["breakout_lookbacks"], reverse=True):
        hi, lo = r.get(f"high{n}"), r.get(f"low{n}")
        if not _ok(hi, lo, vr):
            continue
        if r.close < lo and vr >= need:
            out.append(Signal(f"breakdown_{n}", f"{n}-day breakdown on volume", "bearish",
                              f"Close {_f(r.close)} < prior {n}-day low {_f(lo)}; volume {_f(vr, 1)}× 20-day avg (need {need}×)"))
            break
    return out


def _day_side(r) -> tuple[str, str]:
    prev = r.get("prev_close_adj")
    ref = prev if _ok(prev) else r.open
    if r.close > ref:
        return "bullish", "up day"
    if r.close < ref:
        return "bearish", "down day"
    return "info", "flat day"


def volume_spike(d: pd.DataFrame, cfg: dict):
    r = d.iloc[-1]
    if not _ok(r.get("vol_ratio")) or r.vol_ratio < cfg["volume_spike_ratio"]:
        return None
    side, word = _day_side(r)
    return Signal("volume_spike", f"Volume spike ({word})", side,
                  f"Volume {_f(r.volume, 0)} = {_f(r.vol_ratio, 1)}× its 20-day average {_f(r.vol_avg, 0)} on a {word}")


def delivery(d: pd.DataFrame, cfg: dict):
    r = d.iloc[-1]
    dp, da = r.get("deliv_pct"), r.get("deliv_avg")
    if not _ok(dp, da):
        return None
    if dp >= cfg["delivery_min_pct"] and dp >= cfg["delivery_vs_avg_ratio"] * da:
        side, word = _day_side(r)
        return Signal("high_delivery", f"High delivery ({word})", side,
                      f"Delivery {_f(dp, 1)}% vs 20-day avg {_f(da, 1)}% on a {word} "
                      f"(needs ≥ {cfg['delivery_min_pct']}% and ≥ {cfg['delivery_vs_avg_ratio']}× avg)")
    return None


def relative_strength(d: pd.DataFrame, cfg: dict):
    r = d.iloc[-1]
    keys = [f"rs{n}" for n in cfg["rs_lookbacks"]]
    vals = [r.get(k) for k in keys]
    if not _ok(*vals):
        return None
    txt = ", ".join(f"{n}d {v:+.1f} pts" for n, v in zip(cfg["rs_lookbacks"], vals))
    if all(v > 0 for v in vals):
        return Signal("rs_strong", "Outperforming benchmark", "bullish", f"Return minus benchmark return: {txt}")
    if all(v < 0 for v in vals):
        return Signal("rs_weak", "Underperforming benchmark", "bearish", f"Return minus benchmark return: {txt}")
    return None


def support_resistance(d: pd.DataFrame, cfg: dict, ctx: dict | None = None):
    ctx = ctx or {}
    r = d.iloc[-1]
    sup, res, atr = ctx.get("support"), ctx.get("resistance"), r.get("atr")
    if not _ok(atr):
        return None
    near = cfg["sr_near_atr"] * atr
    out = []
    if sup and r.close - sup.price <= near:
        out.append(Signal("near_support", "Near support", "info",
                          f"Close {_f(r.close)} is {_f((r.close - sup.price) / atr, 1)} ATR above support {sup.describe()}"))
    if res and res.price - r.close <= near:
        out.append(Signal("near_resistance", "Near resistance", "info",
                          f"Close {_f(r.close)} is {_f((res.price - r.close) / atr, 1)} ATR below resistance {res.describe()}"))
    return out


RULES = [trend_stack, dma_cross, rsi_state, macd_cross, breakouts, volume_spike, delivery,
         relative_strength]


def evaluate(d: pd.DataFrame, cfg: dict, ctx: dict | None = None) -> list[Signal]:
    out: list[Signal] = []
    for rule in RULES:
        res = rule(d, cfg)
        if res is None:
            continue
        out += res if isinstance(res, list) else [res]
    out += support_resistance(d, cfg, ctx) or []
    return out
