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
    for t in ["prices", "delivery", "index_prices", "corp_actions", "universe", "fno", "scan_results",
              "fund_info", "fund_statements", "shareholding", "holders", "pledges", "deals", "flows"]:
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
    _demo_smart_money(store, rng, [u["symbol"] for u in uni], px, end)
    store.set_meta("demo", "1")
    store.set_meta("last_update", "demo data")
    return store


def _demo_smart_money(store: Store, rng, symbols: list[str], px: pd.DataFrame, end: date) -> None:
    """Random fundamentals, shareholding, deals and flows so the Phase 3 pages have data."""
    from .fundamentals.model import Fundamentals
    years = [pd.Timestamp(f"{y}-03-31") for y in range(end.year - 6, end.year)]
    quarters = list(pd.date_range(end=pd.Timestamp(end) - pd.offsets.QuarterEnd(1), periods=12, freq="QE"))
    last = px.sort_values("date").groupby("symbol")["close"].last()
    investors = ["ASHISH KACHOLIA", "MUKUL MAHAVIR AGRAWAL", "SOME CAPITAL LLP", "ABC MUTUAL FUND", "XYZ FPI"]
    deals, holders = [], []
    for i, sym in enumerate(symbols):
        g, base = rng.normal(0.12, 0.12), rng.uniform(500, 20000)
        sales = base * np.cumprod(np.r_[1, 1 + rng.normal(g, 0.08, len(years) - 1)])
        margin = rng.uniform(0.04, 0.2)
        profit = sales * margin * rng.normal(1, 0.15, len(years))
        # crore shares, chosen so P/E lands somewhere between 8 and 60
        shares = profit[-1] / (float(last.get(sym, 100)) / rng.uniform(8, 60))
        eq = np.cumsum(np.r_[base * 0.5, profit[1:] * 0.7])
        rows = []
        for k, p in enumerate(years):
            rows += [("annual", "sales", p, sales[k]), ("annual", "net_profit", p, profit[k]),
                     ("annual", "pbt", p, profit[k] * 1.33), ("annual", "interest", p, sales[k] * rng.uniform(0, .04)),
                     ("annual", "eps", p, profit[k] / shares),
                     ("balance", "equity_capital", p, shares * 10), ("balance", "reserves", p, eq[k]),
                     ("balance", "borrowings", p, eq[k] * rng.uniform(0, 1.5)),
                     ("cashflow", "cfo", p, profit[k] * rng.uniform(0.3, 1.4)),
                     ("cashflow", "cfi", p, -profit[k] * rng.uniform(0.2, 0.9)),
                     ("ratios", "debtor_days", p, rng.uniform(20, 120)),
                     ("ratios", "roce_pct", p, rng.uniform(4, 35))]
        qsales = sales[-1] / 4 * np.cumprod(np.r_[1, 1 + rng.normal(g / 4, 0.05, len(quarters) - 1)])
        for k, p in enumerate(quarters):
            qp = qsales[k] * margin * rng.normal(1, 0.4)
            rows += [("quarterly", "sales", p, qsales[k]), ("quarterly", "net_profit", p, qp),
                     ("quarterly", "eps", p, qp / shares)]
        prom = np.clip(rng.uniform(30, 75) + np.cumsum(rng.normal(0, 0.6, len(quarters))), 0, 75)
        fii = np.clip(rng.uniform(5, 25) + np.cumsum(rng.normal(0, 0.8, len(quarters))), 0, 60)
        dii = np.clip(rng.uniform(5, 25) + np.cumsum(rng.normal(0, 0.8, len(quarters))), 0, 60)
        mf = dii * rng.uniform(0.4, 0.8)
        sh = [(q, c, v) for q, a, b, d, m in zip(quarters, prom, fii, dii, mf)
              for c, v in (("promoter", a), ("fii", b), ("dii", d), ("mf", m), ("public", 100 - a - b - d))]
        f = Fundamentals(symbol=sym, source="demo", fetched=end,
                         statements=pd.DataFrame(rows, columns=["statement", "item", "period", "value"]),
                         ratios={"book_value": (shares * 10 + eq[-1]) / shares,
                                 "mcap_cr": float(last.get(sym, 100)) * shares,
                                 "pledge_pct": float(max(0, rng.normal(-5, 12)))},
                         sector=SECTORS[i % len(SECTORS)], industry=SECTORS[i % len(SECTORS)],
                         is_financial=(i % len(SECTORS) == 0),
                         shareholding=pd.DataFrame(sh, columns=["quarter_end", "category", "pct"]))
        store.save_fundamentals(f)
        for q in quarters[-2:]:
            for name in rng.choice(investors, 2, replace=False):
                holders.append((sym, q, name, "public", rng.uniform(1, 3), None, "demo"))
        for _ in range(rng.integers(0, 3)):
            deals.append((end - timedelta(days=int(rng.integers(0, 80))), sym, str(rng.choice(["bulk", "block"])),
                          str(rng.choice(investors)), str(rng.choice(["BUY", "SELL"])),
                          float(rng.integers(1e5, 2e6)), float(last.get(sym, 100)), "demo"))
    store.upsert_holders(pd.DataFrame(holders, columns=["symbol", "quarter_end", "name", "category", "pct",
                                                        "shares", "source"]))
    store.upsert_deals(pd.DataFrame(deals, columns=["date", "symbol", "kind", "client", "side", "qty", "price",
                                                    "source"]))
    days = pd.bdate_range(end - timedelta(days=60), end)
    store.upsert_flows(pd.DataFrame([(d, c, 0.0, 0.0, float(rng.normal(0, 1500)), "demo")
                                     for d in days for c in ("FII", "DII")],
                                    columns=["date", "category", "buy_cr", "sell_cr", "net_cr", "source"]))
