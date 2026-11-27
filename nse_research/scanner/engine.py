"""Run the technical scan over the universe and produce one transparent row per stock."""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from ..data.store import Store
from ..indicators.core import add_indicators
from ..indicators.levels import find_levels, nearest_levels
from ..settings import Settings
from .rules import evaluate

log = logging.getLogger(__name__)


def _num(x):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(x) else round(x, 4)


def analyse_symbol(px: pd.DataFrame, cfg: dict, bench: pd.Series | None) -> tuple[pd.DataFrame, dict]:
    """Indicators + signals for one stock. Returns (indicator frame, summary dict)."""
    d = add_indicators(px.sort_values("date"), cfg, bench)
    d["prev_close_adj"] = d["close"].shift(1)
    last = d.iloc[-1]
    levels = find_levels(d, cfg["pivot_window"], cfg["sr_lookback_days"], cfg["sr_cluster_atr"],
                         _num(last.get("atr")))
    sup, res = nearest_levels(levels, float(last["close"]))
    signals = evaluate(d, cfg, {"support": sup, "resistance": res})
    prev = d["close"].iloc[-2] if len(d) > 1 else np.nan
    summary = {
        "date": pd.Timestamp(last["date"]).date().isoformat(),
        "close": _num(last["close"]),
        "change_pct": _num((last["close"] / prev - 1) * 100) if prev == prev else None,
        **{f"dma{n}": _num(last.get(f"dma{n}")) for n in cfg["dma_periods"]},
        "rsi": _num(last.get("rsi")),
        "macd": _num(last.get("macd")), "macd_signal": _num(last.get("macd_signal")),
        "atr": _num(last.get("atr")), "atr_pct": _num(last.get("atr_pct")),
        "vol_ratio": _num(last.get("vol_ratio")),
        "deliv_pct": _num(last.get("deliv_pct")), "deliv_avg": _num(last.get("deliv_avg")),
        **{f"rs{n}": _num(last.get(f"rs{n}")) for n in cfg["rs_lookbacks"]},
        "turnover_avg_cr": _num(last.get("turnover_avg_cr")),
        "support": _num(sup.price) if sup else None, "support_touches": sup.touches if sup else None,
        "resistance": _num(res.price) if res else None,
        "resistance_touches": res.touches if res else None,
        "signals": [s.to_dict() for s in signals],
        "bullish": [s.label for s in signals if s.side == "bullish"],
        "bearish": [s.label for s in signals if s.side == "bearish"],
        "info": [s.label for s in signals if s.side == "info"],
        "n_bullish": sum(s.side == "bullish" for s in signals),
        "n_bearish": sum(s.side == "bearish" for s in signals),
        "source": last.get("source"),
    }
    return d, summary


def run_scan(settings: Settings, store: Store, symbols: list[str], save: bool = True) -> pd.DataFrame:
    cfg = settings["scanner"]
    series = settings["universe"]["series"]
    b = settings["benchmark"]
    bidx = store.index_prices(b["index_name"])
    bench = bidx.set_index("date")["close"] if not bidx.empty else None
    if bench is None:
        log.warning("No benchmark (%s) prices stored: relative strength will be blank.", b["index_name"])
    uni = store.universe().drop_duplicates("symbol").set_index("symbol")
    fno = store.fno_symbols()
    latest = store.last_price_date()
    px_all = store.adjusted_prices(symbols, series=series)
    rows, skipped = [], {"no data": 0, "short history": 0, "illiquid": 0, "stale": 0, "error": 0}
    for sym, px in px_all.groupby("symbol"):
        if len(px) < cfg["min_history_days"]:
            skipped["short history"] += 1
            continue
        try:
            _, summ = analyse_symbol(px, cfg, bench)
        except Exception as e:  # noqa: BLE001 - one bad stock must not stop the scan
            log.warning("scan failed for %s: %s", sym, e)
            skipped["error"] += 1
            continue
        liq = summ.get("turnover_avg_cr")
        if liq is not None and liq < cfg["min_avg_turnover_cr"]:
            skipped["illiquid"] += 1
            continue
        summ["stale"] = latest is not None and summ["date"] != pd.Timestamp(latest).date().isoformat()
        summ.update({
            "symbol": sym,
            "company": uni["company"].get(sym) if sym in uni.index else None,
            "industry": uni["industry"].get(sym) if sym in uni.index else None,
            "fno": sym in fno,
        })
        # SELL means a short. Multi-day shorts need F&O, so flag what is actually shortable.
        summ["short_possible"] = bool(settings["profile"]["allow_short"]) and (
            summ["fno"] or not settings["profile"]["short_requires_fno"])
        rows.append(summ)
    skipped["no data"] = len(set(symbols) - set(px_all["symbol"])) if not px_all.empty else len(symbols)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["n_bullish", "n_bearish", "symbol"], ascending=[False, True, True])
        if save:
            store.save_scan(latest or date.today(), df)
    store.set_meta("last_scan_skipped", str(skipped))
    log.info("Scanned %d stocks; skipped %s", len(df), skipped)
    return df
