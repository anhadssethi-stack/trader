"""Data checks that catch problems which would silently distort a backtest."""
from __future__ import annotations

import pandas as pd

from .store import Store


def suspicious_moves(store: Store, series: list[str], threshold: float = 0.25,
                     batch: int = 300) -> pd.DataFrame:
    """Day-to-day moves larger than `threshold` in the split-adjusted closes.

    Most NSE stocks have 20% price bands, so a bigger jump in *adjusted* data usually
    means a split or bonus that was not detected (or a bad print). Each row says whether
    NSE's own previous close on that day agrees with our last close: if it doesn't, the
    move is an un-applied corporate action."""
    syms = store.q("SELECT DISTINCT symbol FROM prices ORDER BY symbol")["symbol"].tolist()
    out = []
    for k in range(0, len(syms), batch):
        px = store.adjusted_prices(syms[k:k + batch], series=series)
        if px.empty:
            continue
        px = px.sort_values(["symbol", "date"])
        g = px.groupby("symbol")
        px["last_close"] = g["close"].shift(1)
        px["move_pct"] = (px["close"] / px["last_close"] - 1) * 100
        bad = px[px["move_pct"].abs() > threshold * 100]
        if not bad.empty:
            out.append(bad[["symbol", "date", "last_close", "close", "move_pct", "prev_close", "source"]])
    if not out:
        return pd.DataFrame(columns=["symbol", "date", "last_close", "close", "move_pct", "prev_close", "source"])
    return pd.concat(out).sort_values("move_pct").reset_index(drop=True)
