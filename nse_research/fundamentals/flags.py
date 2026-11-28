"""Red and green flags on fundamentals. Each rule is a plain threshold from
settings.yaml (fundamentals.flags) and its reason shows the numbers it used.
There is no combined score: the screen shows which flags fired and why.

Banks, NBFCs and insurers borrow as their business, so the debt, interest-cover,
ROCE and cash-flow rules are skipped for them (their cash flows mostly reflect
lending, not earnings quality). ROE is used for them instead of ROCE.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass
class Flag:
    id: str
    label: str
    side: str     # "red" | "green"
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _n(x, nd: int = 1) -> str:
    return "n/a" if x is None else f"{x:,.{nd}f}"


def _has(*xs) -> bool:
    return all(x is not None and x == x for x in xs)


def evaluate(m: dict, cfg: dict) -> list[Flag]:
    """Flags for one company's metrics dict (from metrics.compute + sector context)."""
    c = cfg["flags"]
    fin = bool(m.get("is_financial"))
    out: list[Flag] = []
    add = out.append

    # ---- ownership
    pl = m.get("pledge_pct")
    if _has(pl) and pl >= c["pledge_pct_red"]:
        add(Flag("pledge_high", "Promoter shares pledged", "red",
                 f"{_n(pl)}% of promoter shares are pledged (flag at {c['pledge_pct_red']}%)."))
    ch = m.get("promoter_chg_4q")
    if _has(ch) and ch <= -c["promoter_change_pp"]:
        add(Flag("promoter_selling", "Promoter stake falling", "red",
                 f"Promoter holding fell {_n(-ch, 2)} points over 4 quarters to {_n(m.get('promoter_pct'), 2)}%."))
    elif _has(ch) and ch >= c["promoter_change_pp"]:
        add(Flag("promoter_buying", "Promoter stake rising", "green",
                 f"Promoter holding rose {_n(ch, 2)} points over 4 quarters to {_n(m.get('promoter_pct'), 2)}%."))

    # ---- growth
    s3, p3 = m.get("sales_cagr_3y"), m.get("profit_cagr_3y")
    if _has(s3) and s3 < 0:
        add(Flag("sales_falling", "Sales shrinking", "red", f"3-year sales growth {_n(s3)}% a year."))
    if _has(s3, p3) and s3 >= c["growth_green_pct"] and p3 >= c["growth_green_pct"]:
        add(Flag("strong_growth", "Strong 3-year growth", "green",
                 f"Sales {_n(s3)}% and profit {_n(p3)}% a year over 3 years "
                 f"(flag at {c['growth_green_pct']}%+ for both)."))
    lq = m.get("loss_quarters_4")
    if _has(lq) and lq >= c["loss_quarters_red"]:
        add(Flag("recent_losses", "Losses in recent quarters", "red", f"Net loss in {lq} of the last 4 quarters."))
    qp = m.get("qtr_profit_yoy")
    if _has(qp) and qp <= -c["qtr_profit_drop_pct"]:
        add(Flag("profit_drop", "Latest quarter profit down sharply", "red",
                 f"Quarter ending {m.get('latest_quarter')}: net profit {_n(qp)}% vs a year earlier."))

    # ---- returns on capital
    if fin:
        roe = m.get("roe_pct")
        if _has(roe) and roe < c["roe_min_pct"]:
            add(Flag("low_roe", "Low return on equity", "red",
                     f"ROE {_n(roe)}% (financial company; flag below {c['roe_min_pct']}%)."))
        elif _has(roe) and roe >= c["roe_green_pct"]:
            add(Flag("high_roe", "High return on equity", "green", f"ROE {_n(roe)}% (flag at {c['roe_green_pct']}%+)."))
    else:
        roce = m.get("roce_pct")
        if _has(roce) and roce < c["roce_min_pct"]:
            add(Flag("low_roce", "Low return on capital", "red",
                     f"ROCE {_n(roce)}% (flag below {c['roce_min_pct']}%)."))
        elif _has(roce) and roce >= c["roce_green_pct"]:
            add(Flag("high_roce", "High return on capital", "green",
                     f"ROCE {_n(roce)}% (flag at {c['roce_green_pct']}%+)."))

    # ---- debt and cash (not for lenders)
    if not fin:
        de = m.get("debt_to_equity")
        if _has(de) and de > c["debt_to_equity_red"]:
            add(Flag("high_debt", "High debt", "red",
                     f"Debt/equity {_n(de, 2)} (borrowings ₹{_n(m.get('borrowings_cr'), 0)} Cr; "
                     f"flag above {c['debt_to_equity_red']})."))
        elif _has(de) and de <= c["debt_free_max"]:
            add(Flag("debt_free", "Almost debt-free", "green", f"Debt/equity {_n(de, 2)}."))
        ic = m.get("interest_cover")
        if _has(ic) and ic < c["interest_cover_red"]:
            add(Flag("low_interest_cover", "Interest barely covered", "red",
                     f"Profit before interest and tax is {_n(ic)}× interest (flag below {c['interest_cover_red']}×)."))
        cq = m.get("cfo_to_profit_3y")
        if _has(cq) and cq < c["cfo_to_profit_red"]:
            add(Flag("weak_cash_conversion", "Profits not turning into cash", "red",
                     f"Operating cash flow ₹{_n(m.get('cfo_3y_cr'), 0)} Cr vs net profit "
                     f"₹{_n(m.get('profit_3y_cr'), 0)} Cr over 3 years = {_n(cq, 2)}× "
                     f"(flag below {c['cfo_to_profit_red']}×)."))
        elif _has(cq) and cq >= c["cfo_to_profit_green"]:
            add(Flag("strong_cash_conversion", "Profits backed by cash", "green",
                     f"Operating cash flow is {_n(cq, 2)}× net profit over 3 years."))
        d0, d3 = m.get("debtor_days"), m.get("debtor_days_3y_ago")
        if _has(d0, d3) and d0 - d3 >= c["debtor_days_rise_red"]:
            add(Flag("receivables_rising", "Customers paying slower", "red",
                     f"Debtor days {_n(d3, 0)} → {_n(d0, 0)} over 3 years "
                     f"(flag at +{c['debtor_days_rise_red']} days)."))

    # ---- valuation vs sector (needs add_sector_context)
    pe, med, n = m.get("pe"), m.get("sector_pe_median"), m.get("sector_peers")
    if m.get("profit_ttm") is not None and m["profit_ttm"] < 0:
        add(Flag("loss_making", "Loss-making (trailing 12 months)", "red",
                 f"Net profit over the last 4 quarters ₹{_n(m['profit_ttm'], 0)} Cr, so P/E is not meaningful."))
    elif _has(pe, med) and med > 0:
        rel = pe / med
        where = f"P/E {_n(pe)} vs {_n(med)} median of {n} {m.get('sector_level', 'sector')} peers ({m.get('peer_group')})"
        if rel >= c["pe_vs_sector_expensive"]:
            add(Flag("expensive_vs_sector", "Expensive vs sector", "red",
                     f"{where} = {_n(rel, 2)}× (flag at {c['pe_vs_sector_expensive']}×)."))
        elif rel <= c["pe_vs_sector_cheap"]:
            add(Flag("cheap_vs_sector", "Cheap vs sector", "green",
                     f"{where} = {_n(rel, 2)}× (flag at {c['pe_vs_sector_cheap']}× or less)."))
    return out


def add_sector_context(df: pd.DataFrame, min_peers: int = 4) -> pd.DataFrame:
    """Median P/E of profitable peers in the same industry (or the broader sector when
    the industry has fewer than `min_peers` with a P/E). Peers = stocks in the stored
    universe, so the median is "vs the Nifty 500 companies in that industry"."""
    df = df.copy()
    for col in ("sector_pe_median", "sector_peers", "peer_group", "sector_level"):
        df[col] = None
    valid = df[(df["pe"].notna()) & (df["pe"] > 0)]
    for level in ("industry", "sector"):
        if level not in df:
            continue
        g = valid.groupby(level)["pe"].agg(["median", "count"])
        g = g[g["count"] >= min_peers]
        need = df["sector_pe_median"].isna() & df[level].isin(g.index)
        df.loc[need, "sector_pe_median"] = df.loc[need, level].map(g["median"])
        df.loc[need, "sector_peers"] = df.loc[need, level].map(g["count"])
        df.loc[need, "peer_group"] = df.loc[need, level]
        df.loc[need, "sector_level"] = level
    return df
