"""Fundamental metrics computed from the canonical statements.

Every metric is a plain formula on published numbers (written next to it below),
so it can be checked by hand against the source page. When a source publishes the
ratio itself (Screener's ROCE, P/E), the computed value is used if the inputs are
there, else the published one, and `metric_sources` records which.
"""
from __future__ import annotations

import math

import pandas as pd

from .model import Fundamentals


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def cagr(s: pd.Series, years: int) -> float | None:
    """Compound annual growth over the last `years` annual periods, in %.
    None when either end is zero/negative (growth from a loss has no CAGR)."""
    s = s.dropna()
    if len(s) < years + 1:
        return None
    start, end = float(s.iloc[-years - 1]), float(s.iloc[-1])
    if start <= 0 or end <= 0:
        return None
    return ((end / start) ** (1 / years) - 1) * 100


def yoy(s: pd.Series) -> float | None:
    """Latest quarter vs the same quarter a year earlier (4 quarters back), in %."""
    s = s.dropna()
    if len(s) < 5:
        return None
    now, then = float(s.iloc[-1]), float(s.iloc[-5])
    if then <= 0:
        return None
    return (now / then - 1) * 100


def _last(s: pd.Series) -> float | None:
    return _f(s.iloc[-1]) if len(s) else None


def _ago(s: pd.Series, n: int) -> float | None:
    return _f(s.iloc[-1 - n]) if len(s) > n else None


def _change(h: pd.Series, months: int) -> float | None:
    """Latest value minus the value for the quarter `months` earlier (by date, so a
    missing quarter gives None instead of comparing with the wrong one)."""
    if len(h) < 2:
        return None
    then = (h.index[-1] - pd.DateOffset(months=months)) + pd.offsets.MonthEnd(0)
    near = h[(h.index >= then - pd.Timedelta(days=5)) & (h.index <= then + pd.Timedelta(days=5))]
    return float(h.iloc[-1] - near.iloc[-1]) if len(near) else None


def compute(f: Fundamentals, price: float | None = None) -> dict:
    """All metrics for one company as a flat dict (None = not available)."""
    m: dict = {"symbol": f.symbol, "source": f.source, "basis": f.basis,
               "fetched": f.fetched.isoformat() if f.fetched else None,
               "sector": f.sector, "industry": f.industry, "is_financial": f.is_financial}
    src: dict = {}
    r = f.ratios or {}

    sales, np_ = f.series("annual", "sales"), f.series("annual", "net_profit")
    for y in (3, 5):
        m[f"sales_cagr_{y}y"] = cagr(sales, y)
        m[f"profit_cagr_{y}y"] = cagr(np_, y)
    qs, qp = f.series("quarterly", "sales"), f.series("quarterly", "net_profit")
    m["qtr_sales_yoy"] = yoy(qs)
    m["qtr_profit_yoy"] = yoy(qp)
    m["latest_quarter"] = qs.index[-1].date().isoformat() if len(qs) else None
    m["loss_quarters_4"] = int((qp.iloc[-4:] < 0).sum()) if len(qp) >= 4 else None
    m["opm_pct"] = _last(f.series("annual", "opm_pct"))
    m["sales_ttm"] = f.ttm("sales")
    m["profit_ttm"] = f.ttm("net_profit")

    # balance sheet: equity = share capital + reserves; D/E = borrowings / equity
    eq = (f.series("balance", "equity_capital") + f.series("balance", "reserves")).dropna()
    debt = f.series("balance", "borrowings")
    m["equity_cr"] = _last(eq)
    m["borrowings_cr"] = _last(debt)
    m["debt_to_equity"] = (_last(debt) / _last(eq)) if _last(debt) is not None and (_last(eq) or 0) > 0 else None

    # ROE: the source's own figure first (Screener uses profit attributable to shareholders,
    # which excludes minority interests); else net profit / average equity, latest year
    if _f(r.get("roe_pct")) is not None:
        m["roe_pct"], src["roe_pct"] = _f(r.get("roe_pct")), "published"
    elif len(f.series("ratios", "roe_pct")):
        m["roe_pct"], src["roe_pct"] = _last(f.series("ratios", "roe_pct")), "source ratios table"
    elif len(np_) and len(eq) >= 2 and np_.index[-1] in eq.index:
        i = eq.index.get_loc(np_.index[-1])
        avg_eq = (eq.iloc[i] + eq.iloc[i - 1]) / 2 if i >= 1 else eq.iloc[i]
        m["roe_pct"] = float(np_.iloc[-1] / avg_eq * 100) if avg_eq > 0 else None
        src["roe_pct"] = "computed (incl. minority interests)"
    else:
        m["roe_pct"] = None

    # ROCE = EBIT / capital employed (equity + borrowings); the source's own ROCE row if present
    roce_hist = f.series("ratios", "roce_pct")
    if f.is_financial:
        m["roce_pct"] = None        # capital employed has no meaning for a lender; ROE is used
    elif len(roce_hist):
        m["roce_pct"] = _last(roce_hist)
        src["roce_pct"] = "source ratios table"
    else:
        pbt, intr = f.series("annual", "pbt"), f.series("annual", "interest")
        ce = (eq + debt.reindex(eq.index).fillna(0)).dropna()
        if len(pbt) and len(ce) and pbt.index[-1] in ce.index:
            ebit = pbt.iloc[-1] + (intr.iloc[-1] if len(intr) else 0)
            m["roce_pct"] = float(ebit / ce.loc[pbt.index[-1]] * 100) if ce.loc[pbt.index[-1]] > 0 else None
            src["roce_pct"] = "computed"
        else:
            m["roce_pct"] = _f(r.get("roce_pct"))
            src["roce_pct"] = "published" if m["roce_pct"] is not None else None
    m["roce_3y_ago"] = _ago(roce_hist, 3)

    # interest cover = (PBT + interest) / interest, trailing twelve months if available
    pbt_t, int_t = f.ttm("pbt"), f.ttm("interest")
    if pbt_t is None:
        pbt_t, int_t = _last(f.series("annual", "pbt")), _last(f.series("annual", "interest"))
    m["interest_cover"] = ((pbt_t + int_t) / int_t) if pbt_t is not None and int_t and int_t > 0 else None

    # cash flow quality: operating cash flow vs reported profit, last 3 years summed
    cfo = f.series("cashflow", "cfo")
    cfi = f.series("cashflow", "cfi")
    if len(cfo) >= 3 and len(np_) >= 3:
        p3 = np_.reindex(cfo.index[-3:]).sum(min_count=3)
        c3 = cfo.iloc[-3:].sum()
        m["cfo_3y_cr"] = float(c3)
        m["profit_3y_cr"] = _f(p3)
        # a lender's operating cash flow is mostly loans made/repaid, not earnings quality
        m["cfo_to_profit_3y"] = float(c3 / p3) if p3 and p3 > 0 and not f.is_financial else None
    else:
        m["cfo_3y_cr"] = m["profit_3y_cr"] = m["cfo_to_profit_3y"] = None
    # free cash flow, last 3 years: the source's FCF row (CFO - capex) when published,
    # else CFO + CFI (cash left after all investing; CFI is negative when the company invests)
    fcf = f.series("cashflow", "fcf")
    if len(fcf) >= 3:
        m["fcf_3y_cr"], src["fcf_3y_cr"] = float(fcf.iloc[-3:].sum()), "published FCF"
    elif len(cfo) >= 3 and len(cfi) >= 3:
        m["fcf_3y_cr"], src["fcf_3y_cr"] = float(cfo.iloc[-3:].sum() + cfi.iloc[-3:].sum()), "CFO + CFI"
    else:
        m["fcf_3y_cr"] = None

    dd = f.series("ratios", "debtor_days")
    m["debtor_days"] = _last(dd)
    m["debtor_days_3y_ago"] = _ago(dd, 3)

    # valuation: P/E = price / trailing EPS; P/B = price / book value per share
    eps_t = f.ttm("eps")
    price = _f(price) or _f(r.get("price"))
    m["price"] = price
    if price and eps_t:
        m["pe"] = price / eps_t if eps_t > 0 else None
        src["pe"] = "computed (price / TTM EPS)"
    else:
        m["pe"] = _f(r.get("pe"))
        src["pe"] = "published" if m["pe"] is not None else None
    bv = _f(r.get("book_value"))
    m["pb"] = (price / bv) if price and bv and bv > 0 else _f(r.get("pb"))
    m["mcap_cr"] = _f(r.get("mcap_cr"))
    m["div_yield_pct"] = _f(r.get("div_yield_pct"))

    # ownership
    for cat in ("promoter", "fii", "dii", "mf", "public"):
        h = f.holding(cat)
        m[f"{cat}_pct"] = _last(h)
        m[f"{cat}_chg_1q"] = _change(h, 3)
        m[f"{cat}_chg_4q"] = _change(h, 12)
    prom = f.holding("promoter")
    m["shareholding_quarter"] = prom.index[-1].date().isoformat() if len(prom) else None
    m["pledge_pct"] = _f(r.get("pledge_pct"))

    m["metric_sources"] = {k: v for k, v in src.items() if v}
    return m
