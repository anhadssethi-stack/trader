"""yfinance (Yahoo Finance) fallback for prices and index history.

Limits and failure modes:
- Unofficial scraping library; breaks when Yahoo changes its site, and rate-limits
  bursts (HTTP 429 / empty frames). We batch symbols and treat empty results as a failure.
- No delivery data, no ISIN, no series.
- Yahoo's "Close" is already split-adjusted *as of the download date*. We convert it
  back to raw prices using Yahoo's split list, so it can sit next to NSE raw prices,
  and record the splits as corporate actions. Yahoo sometimes misses or misdates
  Indian bonus issues; NSE-derived adjustments are preferred when both exist.
- Delisted symbols usually vanish from Yahoo (survivorship bias), and some volumes
  differ from NSE's because Yahoo can mix in BSE or block-window trades.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .base import INDEX_COLS, PRICE_COLS, finish

log = logging.getLogger(__name__)

YAHOO_SYMBOL_FIXES: dict[str, str] = {}  # e.g. {"NSESYMBOL": "YAHOOSYMBOL"} when they differ


def to_yahoo(symbol: str) -> str:
    return YAHOO_SYMBOL_FIXES.get(symbol, symbol) + ".NS"


def unadjust_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[date, float]]]:
    """Turn Yahoo's split-adjusted OHLCV back into raw prices.

    df: index = dates, columns Open/High/Low/Close/Volume/Stock Splits.
    Returns (raw frame, [(ex_date, factor)]) where factor multiplies prices *before*
    ex_date to make them comparable with prices on/after it (e.g. 1:2 split -> 0.5).
    """
    df = df.sort_index().copy()
    splits = df.get("Stock Splits", pd.Series(0.0, index=df.index)).fillna(0.0)
    splits = splits[splits > 0]
    # cumulative ratio of all splits strictly after each date
    mult = pd.Series(1.0, index=df.index)
    actions = []
    for d, ratio in splits.items():
        mult.loc[mult.index < d] *= float(ratio)
        actions.append((pd.Timestamp(d).date(), 1.0 / float(ratio)))
    for c in ["Open", "High", "Low", "Close"]:
        df[c] = df[c] * mult
    df["Volume"] = df["Volume"] / mult
    return df, actions


class YahooSource:
    name = "yahoo"
    kind = "price"

    def __init__(self, batch_size: int = 50, cache_dir=None):
        self.batch_size = batch_size
        self.cache_dir = cache_dir
        self.missing: list[str] = []
        self._configured = False

    def _configure(self) -> None:
        """Give yfinance its own cache folder (its default one often fails on macOS)
        and quiet its per-symbol log spam; missing symbols are summarised by us instead."""
        if self._configured:
            return
        import yfinance as yf
        if self.cache_dir is not None:
            from pathlib import Path
            p = Path(self.cache_dir) / "yfinance"
            p.mkdir(parents=True, exist_ok=True)
            try:
                yf.set_tz_cache_location(str(p))
            except Exception:  # noqa: BLE001 - cache is optional
                pass
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        self._configured = True

    def supports(self, d: date) -> bool:
        return True

    def _download(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        import yfinance as yf  # imported lazily: optional at test time

        self._configure()
        return yf.download(
            tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False, actions=True, group_by="ticker", threads=True, progress=False,
        )

    def fetch_range(self, symbols: list[str], start: date, end: date
                    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (prices in PRICE_COLS, corp_actions[symbol, ex_date, factor, source])."""
        frames, actions = [], []
        self.missing = []
        for i in range(0, len(symbols), self.batch_size):
            batch = symbols[i:i + self.batch_size]
            tick = [to_yahoo(s) for s in batch]
            try:
                raw = self._download(tick, start, end)
            except Exception as e:  # noqa: BLE001
                log.warning("yfinance batch %d failed: %s", i // self.batch_size, e)
                continue
            if raw is None or raw.empty:
                continue
            for sym, t in zip(batch, tick):
                try:
                    sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                except KeyError:
                    continue
                sub = sub.dropna(subset=["Close"])
                if sub.empty:
                    self.missing.append(sym)
                    continue
                sub, acts = unadjust_splits(sub)
                actions += [(sym, d, f, "yahoo") for d, f in acts]
                frames.append(pd.DataFrame({
                    "date": sub.index, "symbol": sym, "series": "EQ", "isin": None,
                    "open": sub["Open"].values, "high": sub["High"].values,
                    "low": sub["Low"].values, "close": sub["Close"].values,
                    "prev_close": np.nan, "volume": sub["Volume"].values,
                    "turnover": (sub["Close"] * sub["Volume"]).values, "trades": np.nan,
                    "source": "yahoo",
                }))
        if self.missing:
            log.info("Yahoo had no data for %d symbol(s) (renamed, new or delisted; NSE files cover them): %s",
                     len(self.missing), ", ".join(sorted(set(self.missing))[:20]))
        prices = finish(pd.concat(frames), PRICE_COLS) if frames else finish(pd.DataFrame(), PRICE_COLS)
        ca = pd.DataFrame(actions, columns=["symbol", "ex_date", "factor", "source"])
        return prices, ca

    def fetch_index(self, ticker: str, index_name: str, start: date, end: date) -> pd.DataFrame:
        raw = self._download([ticker], start, end)
        if raw is None or raw.empty:
            return finish(pd.DataFrame(), INDEX_COLS)
        sub = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
        sub = sub.dropna(subset=["Close"])
        out = pd.DataFrame({
            "date": sub.index, "index_name": index_name, "open": sub["Open"].values,
            "high": sub["High"].values, "low": sub["Low"].values, "close": sub["Close"].values,
            "source": "yahoo",
        })
        return finish(out, INDEX_COLS)
