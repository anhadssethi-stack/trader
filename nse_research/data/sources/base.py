"""Common schemas for every data source.

Each source turns its own file format into one of these normalised frames, so the
rest of the app never cares where a row came from (the `source` column records it).
"""
from __future__ import annotations

import pandas as pd

PRICE_COLS = [
    "date", "symbol", "series", "isin", "open", "high", "low", "close",
    "prev_close", "volume", "turnover", "trades", "source",
]
DELIVERY_COLS = ["date", "symbol", "series", "deliv_qty", "deliv_pct", "source"]
INDEX_COLS = ["date", "index_name", "open", "high", "low", "close", "source"]


def empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip whitespace from headers and string cells (NSE files pad both)."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].astype(str).str.strip()
    return df


def to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip().replace({"-": None, "": None}),
                         errors="coerce")


def finish(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.reset_index(drop=True)
