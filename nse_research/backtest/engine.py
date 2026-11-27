"""Event-driven backtest of every setup over the stored history.

Trade rules (all from settings.yaml → backtest):
- Signal on the close of day t, entry at the open of day t+1 plus slippage.
- Stop = entry ∓ stop_atr × ATR(14) measured on day t. Target = entry ± target_r × risk.
- Each later day: if the open gaps through the stop or target, exit at the open.
  Otherwise, if the day's range touches both, assume the stop was hit first
  (conservative). Otherwise exit at whichever level was touched.
- If neither is hit within max_hold_days, exit at that day's close ("time").
- If the stock stops trading (delisted, suspended), exit at the last close ("data_end").
- Quantity follows your risk settings: risk amount ÷ risk per share, capped at the
  max position size. Costs (brokerage, STT, exchange, SEBI, stamp, GST, DP) are
  charged in rupees on that quantity, so results are net of all costs.
- Long = cash delivery. Short = stock futures, only on F&O stocks.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from ..data.store import Store
from ..indicators.core import add_indicators
from ..settings import Settings
from .costs import trade_costs
from .setups import SETUPS, Setup, setup_signals

log = logging.getLogger(__name__)


@dataclass
class SimResult:
    exit_idx: int
    exit_price: float
    reason: str           # target | stop | time | data_end
    entry: float
    stop: float
    target: float
    hit_levels: dict      # r level -> reached before the stop?


def simulate(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int, side: str,
             atr: float, stop_atr: float, target_r: float, max_hold: int, slip: float,
             r_levels: tuple = ()) -> SimResult:
    """Simulate one trade entered at the open of bar i. Prices are slipped against you."""
    n = len(o)
    sgn = 1.0 if side == "long" else -1.0
    entry = o[i] * (1 + sgn * slip)
    risk = stop_atr * atr
    stop = entry - sgn * risk
    target = entry + sgn * target_r * risk
    end = min(i + max_hold, n)            # bars i .. end-1 are in the trade
    oo, hh, ll, cc = o[i:end], h[i:end], l[i:end], c[i:end]
    if side == "long":
        gap_stop = oo <= stop
        gap_tgt = oo >= target
        hit_stop = ll <= stop
        hit_tgt = hh >= target
        fav = hh
    else:
        gap_stop = oo >= stop
        gap_tgt = oo <= target
        hit_stop = hh >= stop
        hit_tgt = ll <= target
        fav = ll
    gap_stop[0] = gap_tgt[0] = False      # the entry fill *is* the first open
    any_evt = gap_stop | gap_tgt | hit_stop | hit_tgt
    k = int(np.argmax(any_evt)) if any_evt.any() else -1

    if k >= 0:
        if gap_stop[k]:
            px, reason = oo[k], "stop"
        elif gap_tgt[k]:
            px, reason = oo[k], "target"
        elif hit_stop[k]:
            px, reason = stop, "stop"
        else:
            px, reason = target, "target"
        exit_rel = k
    else:
        exit_rel = len(oo) - 1
        px = cc[exit_rel]
        reason = "time" if i + max_hold <= n else "data_end"
    exit_price = px * (1 - sgn * slip)

    # "Reached nR before the stop", independent of where the target was set.
    stop_evt = gap_stop | hit_stop
    ks = int(np.argmax(stop_evt)) if stop_evt.any() else len(oo)
    hits = {}
    for r in r_levels:
        lvl = entry + sgn * r * risk
        reach = (fav >= lvl) if side == "long" else (fav <= lvl)
        kr = int(np.argmax(reach)) if reach.any() else len(oo)
        hits[r] = bool(kr < ks)           # same bar as the stop counts as stop first
    return SimResult(i + exit_rel, float(exit_price), reason, float(entry), float(stop), float(target), hits)


def position_size(entry: float, stop: float, settings: Settings) -> int:
    per_share = abs(entry - stop)
    if per_share <= 0 or entry <= 0:
        return 0
    qty = min(settings.risk_amount / per_share, settings.max_position_value / entry)
    return max(int(math.floor(qty)), 1)


def trade_pnl(side: str, entry: float, exit_price: float, qty: int, costs_cfg: dict) -> tuple[float, float]:
    """(gross pnl, total costs) in rupees."""
    if side == "long":
        cb = trade_costs(entry * qty, exit_price * qty, "delivery", costs_cfg)
        gross = (exit_price - entry) * qty
    else:
        cb = trade_costs(exit_price * qty, entry * qty, "futures", costs_cfg)  # buy back = exit
        gross = (entry - exit_price) * qty
    return gross, cb.total


def _eligible(d: pd.DataFrame, bt: dict, cfg: dict) -> pd.Series:
    ok = d["atr"].notna() & d[f"dma{max(cfg['dma_periods'])}"].notna() & (d["close"] >= bt["min_price"])
    if "turnover_avg_cr" in d:
        ok &= d["turnover_avg_cr"] >= bt["min_avg_turnover_cr"]
    if bt.get("start_date"):
        ok &= pd.to_datetime(d["date"]) >= pd.Timestamp(bt["start_date"])
    return ok


def backtest_symbol(sym: str, px: pd.DataFrame, settings: Settings, bench: pd.Series | None,
                    setups: list[Setup], fno: set[str], current: set[str],
                    market_last: np.datetime64 | None = None) -> tuple[list[dict], list[dict]]:
    cfg, bt = settings["scanner"], settings["backtest"]
    d = add_indicators(px.sort_values("date").reset_index(drop=True), cfg, bench)
    if len(d) < 2:
        return [], []
    sig = setup_signals(d, cfg, setups)
    elig = _eligible(d, bt, cfg).to_numpy()
    o, h, l, c = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr = d["atr"].to_numpy(float)
    dates = pd.to_datetime(d["date"]).to_numpy()
    src = d["source"].to_numpy() if "source" in d else np.array([None] * len(d))
    slip = bt["slippage_bps"] / 1e4
    wf = bt["walk_forward"]
    grid = [(sa, tr) for sa in wf["stop_atr_grid"] for tr in wf["target_r_grid"]]
    r_levels = tuple(bt["r_levels"])
    trades, grid_rows = [], []
    for s in setups:
        if s.needs_fno and sym not in fno:
            continue
        fire = np.where(sig[s.id].to_numpy() & elig)[0]
        next_free = 0
        for t in fire:
            i = t + 1
            if i >= len(d) or (bt["one_trade_per_symbol"] and i < next_free):
                continue
            res = simulate(o, h, l, c, i, s.side, atr[t], bt["stop_atr"], bt["target_r"],
                           bt["max_hold_days"], slip, r_levels)
            next_free = res.exit_idx + 1
            if res.reason == "data_end" and market_last is not None and dates[-1] >= market_last:
                res.reason = "open"        # still running today: excluded from statistics
            qty = position_size(res.entry, res.stop, settings)
            gross, cost = trade_pnl(s.side, res.entry, res.exit_price, qty, settings["costs"])
            risk_rs = abs(res.entry - res.stop) * qty
            net = gross - cost
            tid = f"{s.id}|{sym}|{pd.Timestamp(dates[t]).date()}"
            window_src = src[i:res.exit_idx + 1]
            trades.append({
                "trade_id": tid, "setup": s.id, "side": s.side, "symbol": sym,
                "signal_date": pd.Timestamp(dates[t]).date(), "entry_date": pd.Timestamp(dates[i]).date(),
                "exit_date": pd.Timestamp(dates[res.exit_idx]).date(), "bars": res.exit_idx - i + 1,
                "entry": res.entry, "stop": res.stop, "target": res.target, "exit": res.exit_price,
                "exit_reason": res.reason, "atr": atr[t], "qty": qty,
                "gross_pnl": gross, "costs": cost, "net_pnl": net,
                "r": net / risk_rs if risk_rs else 0.0,
                "ret_pct": net / (res.entry * qty) * 100 if qty else 0.0,
                **{f"hit_{r}r": res.hit_levels[r] for r in r_levels},
                "in_current_universe": sym in current,
                "yahoo_share": float(np.mean(window_src == "yahoo")) if len(window_src) else 0.0,
            })
            for sa, tr in grid:
                g = simulate(o, h, l, c, i, s.side, atr[t], sa, tr, bt["max_hold_days"], slip)
                gq = position_size(g.entry, g.stop, settings)
                gg, gc = trade_pnl(s.side, g.entry, g.exit_price, gq, settings["costs"])
                grisk = abs(g.entry - g.stop) * gq
                grid_rows.append({"trade_id": tid, "setup": s.id, "stop_atr": sa, "target_r": tr,
                                  "entry_date": pd.Timestamp(dates[i]).date(),
                                  "exit_date": pd.Timestamp(dates[g.exit_idx]).date(),
                                  "exit_reason": ("open" if g.reason == "data_end" and market_last is not None
                                                  and dates[-1] >= market_last else g.reason),
                                  "r": (gg - gc) / grisk if grisk else 0.0})
    return trades, grid_rows


def backtest_symbols(settings: Settings, store: Store) -> list[str]:
    if settings["backtest"]["universe"] == "current":
        from ..data.pipeline import Pipeline
        return Pipeline(settings, store, http=object(), sources={}).scan_symbols()
    series = settings["universe"]["series"]
    ph = ",".join("?" * len(series))
    return store.q(f"SELECT DISTINCT symbol FROM prices WHERE series IN ({ph}) ORDER BY symbol",
                   series)["symbol"].tolist()


def run_backtest(settings: Settings, store: Store, setup_ids: list[str] | None = None,
                 symbols: list[str] | None = None, batch: int = 100,
                 progress: Callable[[int, int], None] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    setups = [s for s in SETUPS if not setup_ids or s.id in setup_ids]
    symbols = symbols or backtest_symbols(settings, store)
    b = settings["benchmark"]
    bidx = store.index_prices(b["index_name"])
    bench = bidx.set_index("date")["close"] if not bidx.empty else None
    fno = store.fno_symbols()
    current = set(store.universe()["symbol"])
    last = store.last_price_date()
    market_last = np.datetime64(pd.Timestamp(last)) if last is not None else None
    all_trades, all_grid = [], []
    for k in range(0, len(symbols), batch):
        chunk = symbols[k:k + batch]
        px = store.adjusted_prices(chunk, series=settings["universe"]["series"])
        for sym, g in px.groupby("symbol"):
            if len(g) < settings["scanner"]["min_history_days"]:
                continue
            try:
                t, gr = backtest_symbol(sym, g, settings, bench, setups, fno, current, market_last)
            except Exception as e:  # noqa: BLE001 - one bad series must not stop the run
                log.warning("backtest failed for %s: %s", sym, e)
                continue
            all_trades += t
            all_grid += gr
        if progress:
            progress(min(k + batch, len(symbols)), len(symbols))
        log.info("backtest: %d/%d symbols, %d trades so far", min(k + batch, len(symbols)),
                 len(symbols), len(all_trades))
    trades = pd.DataFrame(all_trades)
    grid = pd.DataFrame(all_grid)
    span = store.q("SELECT min(date) AS first, max(date) AS last FROM prices").iloc[0]
    src = store.q("SELECT source, count(*) AS n FROM prices GROUP BY source")
    meta = {
        "created": pd.Timestamp.now().isoformat(timespec="seconds"),
        "symbols_tested": len(symbols),
        "data_first": str(span["first"])[:10], "data_last": str(span["last"])[:10],
        "price_rows_by_source": dict(zip(src["source"], src["n"].astype(int))),
        "fno_symbols": len(fno),
        "setups": [s.id for s in setups],
        "params": {k: v for k, v in settings["backtest"].items()},
        "costs": settings["costs"],
        "profile": settings["profile"],
    }
    return trades, grid, meta
