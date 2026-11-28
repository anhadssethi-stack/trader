"""Fundamental screen: metrics + red/green flags for every stored company."""
from __future__ import annotations

import logging

import pandas as pd

from ..data.store import Store
from ..settings import Settings
from .flags import add_sector_context, evaluate
from .metrics import compute

log = logging.getLogger(__name__)


def latest_closes(store: Store, symbols: list[str]) -> dict[str, float]:
    """Last stored close per symbol (raw, which is what today's P/E uses)."""
    if not symbols:
        return {}
    ph = ",".join("?" * len(symbols))
    df = store.q(f"""SELECT symbol, arg_max(close, date) AS close FROM prices
                     WHERE symbol IN ({ph}) GROUP BY symbol""", symbols)
    return dict(zip(df["symbol"], df["close"]))


def build_screen(settings: Settings, store: Store, symbols: list[str] | None = None) -> pd.DataFrame:
    """One row per company: metrics, sector P/E context, flags, counts of red/green flags."""
    cfg = settings["fundamentals"]
    have = store.fundamentals_fetched()
    syms = [s for s in (symbols or sorted(have)) if s in have]
    closes = latest_closes(store, syms)
    uni = store.universe().drop_duplicates("symbol").set_index("symbol")
    rows = []
    for sym in syms:
        f = store.load_fundamentals(sym)
        if f is None:
            continue
        try:
            m = compute(f, closes.get(sym))
        except Exception as e:  # noqa: BLE001 - one odd filing must not stop the screen
            log.warning("fundamentals failed for %s: %s", sym, e)
            continue
        m["company"] = uni["company"].get(sym) if sym in uni.index else None
        if not m.get("industry") and sym in uni.index:
            m["industry"] = uni["industry"].get(sym)   # NSE's own industry as a fallback
        rows.append(m)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = add_sector_context(df, cfg["min_sector_peers"])
    flags = [evaluate(r, cfg) for r in df.to_dict("records")]
    df["flags"] = [[f.to_dict() for f in fl] for fl in flags]
    df["red_flags"] = [[f.label for f in fl if f.side == "red"] for fl in flags]
    df["green_flags"] = [[f.label for f in fl if f.side == "green"] for fl in flags]
    df["n_red"] = df["red_flags"].map(len)
    df["n_green"] = df["green_flags"].map(len)
    return df.sort_values(["n_red", "n_green", "symbol"], ascending=[True, False, True]).reset_index(drop=True)
