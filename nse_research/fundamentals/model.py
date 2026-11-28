"""The common shape every fundamentals source is parsed into.

Sources (Screener, Yahoo) publish different labels and layouts. Each parser maps
them onto the canonical item names below, so metrics and flags never care where
the numbers came from. Money values are in ₹ crore, percentages are 0-100.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

# statement -> canonical items
ITEMS = {
    "quarterly": ["sales", "expenses", "operating_profit", "opm_pct", "other_income", "interest",
                  "depreciation", "pbt", "tax_pct", "net_profit", "eps"],
    "annual": ["sales", "expenses", "operating_profit", "opm_pct", "other_income", "interest",
               "depreciation", "pbt", "tax_pct", "net_profit", "eps", "dividend_payout_pct"],
    "balance": ["equity_capital", "reserves", "borrowings", "other_liabilities", "total_liabilities",
                "fixed_assets", "cwip", "investments", "other_assets", "total_assets"],
    "cashflow": ["cfo", "cfi", "cff", "net_cash_flow", "fcf"],
    "ratios": ["debtor_days", "inventory_days", "days_payable", "cash_conversion_cycle",
               "working_capital_days", "roce_pct", "roe_pct"],
}

# shareholding categories, % of total shares
HOLDER_CATEGORIES = ["promoter", "fii", "dii", "mf", "government", "public"]

STATEMENT_COLS = ["statement", "item", "period", "value"]
SHAREHOLDING_COLS = ["quarter_end", "category", "pct"]
HOLDER_COLS = ["quarter_end", "name", "category", "pct", "shares"]


@dataclass
class Fundamentals:
    symbol: str
    source: str                       # "screener", "yahoo", ...
    basis: str = "consolidated"       # consolidated | standalone
    fetched: date = field(default_factory=date.today)
    # long format: statement, item, period (Timestamp, period end), value.
    # A trailing-twelve-months column is stored with statement "annual_ttm".
    statements: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=STATEMENT_COLS))
    # headline numbers as published by the source: price, mcap_cr, pe, pb, book_value,
    # div_yield_pct, roce_pct, roe_pct, face_value, pledge_pct
    ratios: dict = field(default_factory=dict)
    sector: str | None = None
    industry: str | None = None
    is_financial: bool = False        # banks/NBFCs/insurers: debt and ROCE flags don't apply
    shareholding: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=SHAREHOLDING_COLS))
    holders: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=HOLDER_COLS))
    notes: list[str] = field(default_factory=list)

    def series(self, statement: str, item: str) -> pd.Series:
        """One line item over time, oldest first, NaNs dropped."""
        s = self.statements
        if s.empty:
            return pd.Series(dtype=float)
        m = s[(s["statement"] == statement) & (s["item"] == item)]
        out = m.set_index(pd.to_datetime(m["period"]))["value"].astype(float).sort_index()
        return out[~out.index.duplicated(keep="last")].dropna()

    def ttm(self, item: str) -> float | None:
        s = self.series("annual_ttm", item)
        if len(s):
            return float(s.iloc[-1])
        q = self.series("quarterly", item)
        if len(q) >= 4 and item in ("sales", "net_profit", "operating_profit", "interest", "pbt", "eps",
                                    "other_income", "depreciation", "expenses"):
            return float(q.iloc[-4:].sum())
        return None

    def holding(self, category: str) -> pd.Series:
        sh = self.shareholding
        if sh.empty:
            return pd.Series(dtype=float)
        m = sh[sh["category"] == category]
        out = m.set_index(pd.to_datetime(m["quarter_end"]))["pct"].astype(float).sort_index()
        return out[~out.index.duplicated(keep="last")].dropna()
