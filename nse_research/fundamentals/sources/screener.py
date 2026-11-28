"""Screener.in company pages: 10-12 years of annual results, 12-13 quarters, balance
sheet, cash flow, ratios and the quarterly shareholding pattern, all on one page.

Free and public, no login. It is a website, not an API: the page layout can change,
and Screener asks users to be reasonable, so we fetch at most one page per stock per
`fundamentals.refresh_days` with a pause between pages. Values are ₹ crore.
"""
from __future__ import annotations

import logging
import re
from datetime import date

import pandas as pd
from bs4 import BeautifulSoup

from ...data.http import HttpClient, NotFound
from ..model import STATEMENT_COLS, Fundamentals

log = logging.getLogger(__name__)

BASE = "https://www.screener.in/company"

SECTIONS = {"quarters": "quarterly", "profit-loss": "annual", "balance-sheet": "balance",
            "cash-flow": "cashflow", "ratios": "ratios"}

# Screener row label (lower-case, without the "+" expander) -> canonical item
LABELS = {
    "sales": "sales", "revenue": "sales", "expenses": "expenses",
    "operating profit": "operating_profit", "financing profit": "operating_profit",
    "opm %": "opm_pct", "financing margin %": "opm_pct",
    "other income": "other_income", "interest": "interest", "depreciation": "depreciation",
    "profit before tax": "pbt", "tax %": "tax_pct", "net profit": "net_profit",
    "eps in rs": "eps", "dividend payout %": "dividend_payout_pct",
    "equity capital": "equity_capital", "reserves": "reserves", "borrowings": "borrowings",
    "borrowing": "borrowings", "deposits": "deposits",
    "other liabilities": "other_liabilities", "total liabilities": "total_liabilities",
    "fixed assets": "fixed_assets", "cwip": "cwip", "investments": "investments",
    "other assets": "other_assets", "total assets": "total_assets",
    "cash from operating activity": "cfo", "cash from investing activity": "cfi",
    "cash from financing activity": "cff", "net cash flow": "net_cash_flow",
    "free cash flow": "fcf",
    "debtor days": "debtor_days", "inventory days": "inventory_days", "days payable": "days_payable",
    "cash conversion cycle": "cash_conversion_cycle", "working capital days": "working_capital_days",
    "roce %": "roce_pct", "roe %": "roe_pct",
    "gross npa %": "gross_npa_pct", "net npa %": "net_npa_pct",
}
SHP_LABELS = {"promoters": "promoter", "fiis": "fii", "diis": "dii", "government": "government",
              "public": "public", "others": "others", "no. of shareholders": "shareholders"}
TOP_RATIOS = {"market cap": "mcap_cr", "current price": "price", "stock p/e": "pe",
              "book value": "book_value", "dividend yield": "div_yield_pct", "roce": "roce_pct",
              "roe": "roe_pct", "face value": "face_value"}

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"], 1)}


def clean_label(text: str) -> str:
    t = (text or "").replace("\xa0", " ").replace("+", " ")
    return re.sub(r"\s+", " ", t).strip().lower()


def to_number(text: str | None) -> float | None:
    if text is None:
        return None
    t = text.replace("\xa0", " ").replace(",", "").replace("₹", "").replace("%", "")
    t = t.replace("Cr.", "").replace("Cr", "").strip()
    m = re.search(r"-?\d+(\.\d+)?", t)
    return float(m.group()) if m else None


def period_end(header: str) -> pd.Timestamp | None:
    """'Mar 2024' -> 2024-03-31. Extra notes such as 'Mar 2021 15m' are ignored."""
    m = re.match(r"\s*([A-Za-z]{3})\w*\s+(\d{4})", header or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return pd.Timestamp(int(m.group(2)), _MONTHS[m.group(1).lower()], 1) + pd.offsets.MonthEnd(0)


def _table(section) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Header cells and (label, values) rows of the first data table in a section."""
    if section is None:
        return [], []
    tbl = section.select_one("table.data-table") or section.find("table")
    if tbl is None:
        return [], []
    head_row = tbl.find("thead").find("tr") if tbl.find("thead") else tbl.find("tr")
    headers = [th.get_text(" ", strip=True) for th in head_row.find_all(["th", "td"])][1:]
    rows = []
    body = tbl.find("tbody") or tbl
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        rows.append((cells[0].get_text(" ", strip=True), [c.get_text(" ", strip=True) for c in cells[1:]]))
    return headers, rows


def parse_page(html: str | bytes, symbol: str, basis: str) -> Fundamentals:
    soup = BeautifulSoup(html, "html.parser")
    recs: list[tuple] = []
    seen_labels: set[str] = set()
    for sec_id, statement in SECTIONS.items():
        headers, rows = _table(soup.find(id=sec_id))
        periods = [period_end(h) for h in headers]
        for label, vals in rows:
            lab = clean_label(label)
            seen_labels.add(lab)
            item = LABELS.get(lab)
            if item is None:
                continue
            for h, p, v in zip(headers, periods, vals):
                x = to_number(v)
                if x is None:
                    continue
                if h.strip().upper() == "TTM" and statement == "annual":
                    recs.append(("annual_ttm", item, pd.Timestamp(date.today()), x))
                elif p is not None:
                    recs.append((statement, item, p, x))
    statements = pd.DataFrame(recs, columns=STATEMENT_COLS)

    # shareholding: prefer the quarterly table
    shp_sec = soup.find(id="quarterly-shp") or soup.find(id="shareholding")
    headers, rows = _table(shp_sec)
    sh = []
    for label, vals in rows:
        cat = SHP_LABELS.get(clean_label(label))
        if cat is None or cat == "shareholders":
            continue
        for h, v in zip(headers, vals):
            p, x = period_end(h), to_number(v)
            if p is not None and x is not None:
                sh.append((p, cat, x))
    shareholding = pd.DataFrame(sh, columns=["quarter_end", "category", "pct"])

    ratios = {}
    for li in soup.select("#top-ratios li"):
        name = li.select_one(".name")
        key = TOP_RATIOS.get(clean_label(name.get_text(" ", strip=True)) if name else "")
        if key:
            num = li.select_one(".number")
            ratios[key] = to_number(num.get_text() if num else li.get_text(" ", strip=True).split(
                name.get_text(strip=True), 1)[-1])

    # classification: links to /market/... (broad sector ... industry), first and last
    links = [a for a in soup.select("a[href*='/market/']") if a.get_text(strip=True)]
    sector = links[0].get_text(strip=True) if links else None
    industry = links[-1].get_text(strip=True) if links else None

    is_fin = bool({"financing profit", "deposits", "financing margin %"} & seen_labels)
    f = Fundamentals(symbol=symbol, source="screener", basis=basis, statements=statements, ratios=ratios,
                     sector=sector, industry=industry, is_financial=is_fin, shareholding=shareholding)
    if statements.empty:
        f.notes.append("no result tables found on the page (layout change?)")
    return f


class ScreenerSource:
    name = "screener"

    def __init__(self, http: HttpClient, basis: str = "consolidated"):
        self.http = http
        self.basis = basis

    def url(self, symbol: str, basis: str) -> str:
        return f"{BASE}/{symbol}/consolidated/" if basis == "consolidated" else f"{BASE}/{symbol}/"

    def fetch(self, symbol: str, refresh: bool = True) -> Fundamentals:
        order = [self.basis] + [b for b in ("consolidated", "standalone") if b != self.basis]
        last: Fundamentals | None = None
        for basis in order:
            cache = f"screener/{date.today():%Y%m%d}/{symbol}_{basis}.html"
            try:
                raw = self.http.get_cached(self.url(symbol, basis), cache, html=True)
            except NotFound:
                continue
            f = parse_page(raw, symbol, basis)
            # a company without subsidiaries has an empty consolidated page: use standalone
            if len(f.series("annual", "sales")) >= 2:
                return f
            last = f
        if last is None:
            raise NotFound(f"screener: no page for {symbol}")
        return last
