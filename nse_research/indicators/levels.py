"""Support and resistance from swing pivots.

A swing high is a bar whose high is the highest of the `window` bars on each side
(a swing low likewise). Pivots within `cluster_atr` x ATR of each other merge into
one level; a level's strength is how many pivots touched it. Pivots need `window`
bars after them to be confirmed, so the latest few bars can never create a level.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Level:
    price: float
    touches: int
    last_date: pd.Timestamp
    kind: str  # "support" or "resistance" relative to the current close

    def describe(self) -> str:
        return f"{self.price:,.2f} ({self.touches} touch{'es' if self.touches != 1 else ''}, last {self.last_date:%d %b %Y})"


def swing_pivots(high: pd.Series, low: pd.Series, window: int) -> tuple[np.ndarray, np.ndarray]:
    h, lo = high.to_numpy(float), low.to_numpy(float)
    n = len(h)
    is_hi = np.zeros(n, bool)
    is_lo = np.zeros(n, bool)
    for i in range(window, n - window):
        seg_h = h[i - window:i + window + 1]
        seg_l = lo[i - window:i + window + 1]
        if h[i] == np.nanmax(seg_h) and np.sum(seg_h == h[i]) == 1:
            is_hi[i] = True
        if lo[i] == np.nanmin(seg_l) and np.sum(seg_l == lo[i]) == 1:
            is_lo[i] = True
    return is_hi, is_lo


def find_levels(df: pd.DataFrame, window: int = 5, lookback: int = 250, cluster_atr: float = 0.5,
                atr_value: float | None = None) -> list[Level]:
    d = df.tail(lookback).reset_index(drop=True)
    if len(d) < 2 * window + 1:
        return []
    is_hi, is_lo = swing_pivots(d["high"], d["low"], window)
    pts = [(d.at[i, "high"], d.at[i, "date"]) for i in np.where(is_hi)[0]]
    pts += [(d.at[i, "low"], d.at[i, "date"]) for i in np.where(is_lo)[0]]
    if not pts:
        return []
    atr_value = atr_value or float((d["high"] - d["low"]).tail(14).mean())
    tol = max(cluster_atr * atr_value, 1e-9)
    pts.sort(key=lambda x: x[0])
    clusters: list[list[tuple[float, pd.Timestamp]]] = [[pts[0]]]
    for p in pts[1:]:
        if p[0] - np.mean([x[0] for x in clusters[-1]]) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    close = float(d["close"].iloc[-1])
    levels = []
    for c in clusters:
        price = float(np.mean([x[0] for x in c]))
        levels.append(Level(price, len(c), pd.Timestamp(max(x[1] for x in c)),
                            "support" if price < close else "resistance"))
    return levels


def nearest_levels(levels: list[Level], close: float) -> tuple[Level | None, Level | None]:
    sup = [lv for lv in levels if lv.price < close]
    res = [lv for lv in levels if lv.price >= close]
    s = max(sup, key=lambda lv: lv.price) if sup else None
    r = min(res, key=lambda lv: lv.price) if res else None
    return s, r
