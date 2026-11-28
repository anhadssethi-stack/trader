"""Split / bonus / rights adjustment.

NSE bhavcopies are raw: on a split's ex-date the price simply drops. NSE does,
however, publish the *adjusted* previous close on the ex-date (PREVCLOSE), so
  factor = prev_close(ex-date) / close(previous trading day)
recovers the adjustment without needing a separate corporate-actions feed.
Ordinary dividends are normally not adjusted by NSE and are ignored here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def detect_corp_actions(raw: pd.DataFrame, min_change: float = 0.02,
                        market_days: list | None = None) -> pd.DataFrame:
    """Find ex-dates where NSE's prev_close differs from our stored last close.

    Only compares consecutive *market* days, so a gap in our own data can never be
    mistaken for a split. Returns [symbol, ex_date, factor, source].
    """
    cols = ["symbol", "ex_date", "factor", "source"]
    if raw.empty:
        return pd.DataFrame(columns=cols)
    df = raw.sort_values(["symbol", "date"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    days = pd.DatetimeIndex(sorted(pd.to_datetime(market_days if market_days is not None
                                                  else df["date"].unique())))
    if len(days) < 2:
        return pd.DataFrame(columns=cols)
    prev_market = pd.Series(days[:-1], index=days[1:])
    g = df.groupby("symbol", sort=False)
    df["last_close"] = g["close"].shift(1)
    df["last_date"] = g["date"].shift(1)
    df["expected_prev"] = df["date"].map(prev_market)
    ok = (df["prev_close"].notna() & df["last_close"].notna()
          & (df["last_date"] == df["expected_prev"]) & (df["last_close"] > 0))
    ratio = df["prev_close"] / df["last_close"]
    hit = ok & ((ratio - 1).abs() > min_change)
    out = pd.DataFrame({
        "symbol": df.loc[hit, "symbol"],
        "ex_date": df.loc[hit, "date"].dt.date,
        "factor": ratio[hit].round(6),
        "source": "nse_prevclose",
    })
    return out[cols].reset_index(drop=True)


def apply_adjustments(raw: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of raw with OHLC (and prev_close) multiplied, and volume divided,
    by the product of every action factor whose ex-date is *after* the row's date."""
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["adj_factor"] = 1.0
    if actions is not None and not actions.empty:
        acts = actions.copy()
        acts["ex_date"] = pd.to_datetime(acts["ex_date"])
        df = df.reset_index(drop=True)
        groups = df.groupby("symbol").indices
        for sym, a in acts.groupby("symbol"):
            rows = groups.get(sym)
            if rows is None:
                continue
            mask = df.index[rows]
            a = a.sort_values("ex_date")
            ex = a["ex_date"].values
            # suffix product: rev_cum[i] = product of factors[i:]
            rev_cum = np.append(np.cumprod(a["factor"].values[::-1])[::-1], 1.0)
            idx = np.searchsorted(ex, df.loc[mask, "date"].values, side="right")
            df.loc[mask, "adj_factor"] = rev_cum[idx]
    f = df["adj_factor"]
    for c in ["open", "high", "low", "close", "prev_close"]:
        if c in df:
            df[c] = df[c] * f
    if "volume" in df:
        df["volume"] = df["volume"] / f
    return df


def infer_gap_actions(rows: pd.DataFrame, known: pd.DataFrame | None = None,
                      min_gap: float = 0.4) -> pd.DataFrame:
    """Splits/bonuses that NSE did *not* flag through prev_close.

    Some files (mostly older legacy bhavcopies, and ETF unit splits) carry an
    unadjusted previous close on the ex-date, so `detect_corp_actions` can't see the
    event and the stock appears to crash 50-99% overnight. With NSE's price bands
    (20% at most for cash-only stocks), an overnight gap of more than `min_gap` (40%)
    at the open is almost always a corporate action (split, bonus, consolidation,
    demerger, large special dividend), so it is adjusted with factor = open / last close.

    rows: [symbol, date, open, prev_close, last_close, last_date] where last_* is the
    previous stored day. known: existing actions; events within 3 days of one are skipped.
    Returns [symbol, ex_date, factor, source].
    """
    cols = ["symbol", "ex_date", "factor", "source"]
    if rows.empty:
        return pd.DataFrame(columns=cols)
    r = rows.copy()
    r["date"] = pd.to_datetime(r["date"])
    ratio = r["open"] / r["last_close"]
    nse_same = r["prev_close"].isna() | ((r["prev_close"] / r["last_close"] - 1).abs() <= 0.02)
    hit = (r["last_close"] > 0) & (r["open"] > 0) & nse_same & ((ratio < 1 - min_gap) | (ratio > 1 / (1 - min_gap)))
    out = pd.DataFrame({"symbol": r.loc[hit, "symbol"], "ex_date": r.loc[hit, "date"],
                        "factor": ratio[hit].round(6), "source": "gap_inferred"})
    if known is not None and not known.empty and not out.empty:
        k = known.assign(ex_date=pd.to_datetime(known["ex_date"]))[["symbol", "ex_date"]]
        m = out.reset_index().merge(k, on="symbol", how="left", suffixes=("", "_k"))
        near = (m["ex_date"] - m["ex_date_k"]).abs() <= pd.Timedelta(days=3)
        drop = set(m.loc[near, "index"])
        out = out[~out.index.isin(drop)]
    out["ex_date"] = out["ex_date"].dt.date
    return out[cols].reset_index(drop=True)
