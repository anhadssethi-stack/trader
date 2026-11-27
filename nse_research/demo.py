"""Synthetic demo database so the dashboard can be tried without any downloads.
Everything here is random. The app shows a red DEMO DATA banner when it is used."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .data.adjust import detect_corp_actions
from .data.store import Store

SECTORS = ["Financial Services", "Information Technology", "Healthcare", "Automobile and Auto Components",
           "Fast Moving Consumer Goods", "Capital Goods", "Metals & Mining", "Oil Gas & Consumable Fuels"]


def build_demo(path, n_symbols: int = 40, years: int = 3, seed: int = 7) -> Store:
    rng = np.random.default_rng(seed)
    end = date.today() - timedelta(days=1)
    days = pd.bdate_range(end - timedelta(days=int(365.25 * years)), end)
    n = len(days)
    store = Store(path)
    for t in ["prices", "delivery", "index_prices", "corp_actions", "universe", "fno", "scan_results"]:
        store.con.execute(f"DELETE FROM {t}")
    mkt = np.cumsum(rng.normal(0.0004, 0.01, n))
    idx = 20000 * np.exp(mkt)
    store.upsert_index(pd.DataFrame({"date": days.date, "index_name": "Nifty 500", "open": idx,
                                     "high": idx * 1.005, "low": idx * 0.995, "close": idx, "source": "demo"}))
    uni, fno, prices, deliv = [], [], [], []
    for i in range(n_symbols):
        sym = f"DEMO{i:02d}"
        beta, drift = rng.uniform(0.6, 1.4), rng.normal(0.0002, 0.0006)
        r = beta * np.diff(np.r_[0, mkt]) + rng.normal(drift, 0.015, n)
        close = rng.uniform(100, 3000) * np.exp(np.cumsum(r))
        openp = close * np.exp(rng.normal(0, 0.006, n))
        high = np.maximum(openp, close) * np.exp(np.abs(rng.normal(0, 0.008, n)))
        low = np.minimum(openp, close) * np.exp(-np.abs(rng.normal(0, 0.008, n)))
        vol = rng.lognormal(12, 0.5, n) * (1 + 3 * (rng.random(n) < 0.03))
        prev = np.r_[np.nan, close[:-1]]
        if i == 0:  # a 1:2 split two-thirds through, to exercise the adjustment logic
            k = int(n * 2 / 3)
            close[k:], openp[k:], high[k:], low[k:] = close[k:] / 2, openp[k:] / 2, high[k:] / 2, low[k:] / 2
            vol[k:] *= 2
            prev = np.r_[np.nan, close[:-1]]
            prev[k] = close[k - 1] / 2  # NSE publishes the adjusted previous close on the ex-date
        prices.append(pd.DataFrame({"date": days.date, "symbol": sym, "series": "EQ", "isin": None,
                                    "open": openp, "high": high, "low": low, "close": close,
                                    "prev_close": prev, "volume": vol, "turnover": vol * close,
                                    "trades": vol / 50, "source": "demo"}))
        deliv.append(pd.DataFrame({"date": days.date, "symbol": sym, "series": "EQ", "deliv_qty": vol * 0.4,
                                   "deliv_pct": np.clip(rng.normal(45, 12, n), 5, 95), "source": "demo"}))
        uni.append({"symbol": sym, "company": f"Demo Company {i:02d} Ltd", "industry": SECTORS[i % len(SECTORS)],
                    "series": "EQ", "isin": None})
        if i % 2 == 0:
            fno.append(sym)
    px = pd.concat(prices)
    store.upsert_prices(px)
    store.upsert_delivery(pd.concat(deliv))
    store.replace_universe(pd.DataFrame(uni), "nifty500")
    store.replace_fno(fno)
    store.upsert_corp_actions(detect_corp_actions(px, 0.02))
    store.set_meta("demo", "1")
    store.set_meta("last_update", "demo data")
    return store
