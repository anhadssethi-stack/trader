"""Yahoo Finance fundamentals via yfinance: the fallback when Screener fails.

Thinner than Screener for Indian stocks: about 4 years of annual statements and
5 quarters, no shareholding pattern (promoter/FII/DII) and no debtor days.
Yahoo reports rupees; everything is converted to ₹ crore here.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..model import STATEMENT_COLS, Fundamentals

log = logging.getLogger(__name__)
CR = 1e7

# yfinance row label -> (statement, canonical item); first match wins per item
ROWS = {
    "financials": [("Total Revenue", "sales"), ("Operating Revenue", "sales"),
                   ("Net Income Common Stockholders", "net_profit"), ("Net Income", "net_profit"),
                   ("Pretax Income", "pbt"), ("Interest Expense", "interest"),
                   ("Diluted EPS", "eps"), ("Basic EPS", "eps"), ("Operating Income", "operating_profit"),
                   ("Reconciled Depreciation", "depreciation"), ("Other Income Expense", "other_income")],
    "balance": [("Common Stock", "equity_capital"), ("Stockholders Equity", "_equity"),
                ("Total Debt", "borrowings"), ("Total Assets", "total_assets"),
                ("Accounts Receivable", "_receivables")],
    "cashflow": [("Operating Cash Flow", "cfo"), ("Investing Cash Flow", "cfi"),
                 ("Financing Cash Flow", "cff"), ("Changes In Cash", "net_cash_flow")],
}
FIN_WORDS = ("bank", "financial", "insurance", "credit services", "capital markets", "asset management")


def _rows(df: pd.DataFrame, statement: str, mapping: list[tuple[str, str]], per_share: set[str]) -> list[tuple]:
    out, done = [], set()
    if df is None or df.empty:
        return out
    for label, item in mapping:
        if item in done or label not in df.index:
            continue
        done.add(item)
        for col, v in df.loc[label].items():
            if pd.notna(v):
                val = float(v) if item in per_share else float(v) / CR
                out.append((statement, item, pd.Timestamp(col).normalize() + pd.offsets.MonthEnd(0), val))
    return out


def from_yfinance(symbol: str, t) -> Fundamentals:
    """Build Fundamentals from a yfinance.Ticker-like object (testable with a stub)."""
    eps = {"eps"}
    recs = _rows(t.financials, "annual", ROWS["financials"], eps)
    recs += _rows(t.quarterly_financials, "quarterly", ROWS["financials"], eps)
    bal = _rows(t.balance_sheet, "balance", ROWS["balance"], set())
    recs += _rows(t.cashflow, "cashflow", ROWS["cashflow"], set())
    # Yahoo gives total equity, not share capital + reserves: store reserves = equity - capital
    b = pd.DataFrame(bal, columns=STATEMENT_COLS)
    eq = b[b["item"] == "_equity"].set_index("period")["value"]
    cap = b[b["item"] == "equity_capital"].set_index("period")["value"].reindex(eq.index).fillna(0)
    for p in eq.index:
        recs.append(("balance", "equity_capital", p, cap[p]))
        recs.append(("balance", "reserves", p, eq[p] - cap[p]))
    recs += [r for r in bal if r[1] in ("borrowings", "total_assets")]
    st = pd.DataFrame(recs, columns=STATEMENT_COLS)
    # debtor days = receivables / sales x 365
    rec = b[b["item"] == "_receivables"].set_index("period")["value"]
    sales = st[(st["statement"] == "annual") & (st["item"] == "sales")].set_index("period")["value"]
    dd = (rec / sales.reindex(rec.index) * 365).dropna()
    st = pd.concat([st, pd.DataFrame([("ratios", "debtor_days", p, v) for p, v in dd.items()],
                                     columns=STATEMENT_COLS)], ignore_index=True)

    info = {}
    try:
        info = t.info or {}
    except Exception as e:  # noqa: BLE001 - yfinance's info endpoint is flaky
        log.debug("yahoo info failed for %s: %s", symbol, e)
    ratios = {
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "mcap_cr": (info["marketCap"] / CR) if info.get("marketCap") else None,
        "pe": info.get("trailingPE"),
        "book_value": info.get("bookValue"),
        "div_yield_pct": info.get("dividendYield"),   # yfinance already gives this in %
        "roe_pct": info["returnOnEquity"] * 100 if info.get("returnOnEquity") is not None else None,
    }
    sector, industry = info.get("sector"), info.get("industry")
    fin = any(w in f"{sector} {industry}".lower() for w in FIN_WORDS)
    return Fundamentals(symbol=symbol, source="yahoo", basis="consolidated", statements=st,
                        ratios={k: v for k, v in ratios.items() if v is not None},
                        sector=sector, industry=industry, is_financial=fin,
                        notes=["Yahoo: ~4 years of history, no shareholding pattern"])


class YahooFundamentals:
    name = "yahoo"

    def __init__(self, suffix: str = ".NS"):
        self.suffix = suffix

    def fetch(self, symbol: str, refresh: bool = True) -> Fundamentals:
        import yfinance as yf
        return from_yfinance(symbol, yf.Ticker(symbol + self.suffix))
