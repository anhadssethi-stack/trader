"""NSE smart-money sources.

  * Shareholding filing list (JSON API, needs NSE cookies): promoter % for every
    quarter since ~2014 plus a link to each quarter's XBRL filing.
  * The XBRL filing itself (public archive file): FII, DII, mutual fund, government
    and non-institution %, every holder named in the filing (all promoters and
    public holders above 1%) and their stake, and whether promoter shares are pledged.
  * Promoter pledge API (JSON): % of promoter shares pledged.
  * Bulk / block deals (archive CSV, latest trading day only) and the historical
    deals API (JSON; often answers 503, so it is only used for the first backfill).
  * FII/DII cash-market flows (JSON, latest day only).

The JSON APIs are unofficial and NSE changes them; each parser fails soft and the
pipeline logs what it could not read.
"""
from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date

import pandas as pd

from ..data.http import HttpClient

log = logging.getLogger(__name__)

WWW = "https://www.nseindia.com"
ARCHIVE = "https://nsearchives.nseindia.com"


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------- shareholding list
def parse_shp_master(raw: bytes | str | list) -> pd.DataFrame:
    """quarter_end, promoter_pct, public_pct, xbrl, submitted, revised."""
    data = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    if isinstance(data, dict):
        data = data.get("data") or []
    rows = []
    for it in data or []:
        q = pd.to_datetime(it.get("date"), format="%d-%b-%Y", errors="coerce")
        if pd.isna(q):
            continue
        rows.append({"quarter_end": q, "promoter_pct": _num(it.get("pr_and_prgrp")),
                     "public_pct": _num(it.get("public_val")), "xbrl": it.get("xbrl") or None,
                     "submitted": pd.to_datetime(it.get("submissionDate"), format="%d-%b-%Y", errors="coerce"),
                     "revised": it.get("revisedData") == "Y"})
    df = pd.DataFrame(rows, columns=["quarter_end", "promoter_pct", "public_pct", "xbrl", "submitted", "revised"])
    # a quarter can be filed twice (revision): keep the latest submission
    return df.sort_values(["quarter_end", "submitted"]).drop_duplicates("quarter_end", keep="last") \
        .sort_values("quarter_end").reset_index(drop=True)


# ---------------------------------------------------------------- XBRL filing
# context id prefix (before "_Context") -> category for the category totals
XBRL_TOTALS = {
    "ShareholdingOfPromoterAndPromoterGroup": "promoter",
    "InstitutionsForeign": "fii",
    "InstitutionsDomestic": "dii",
    "MutualFundsOrUTI": "mf",
    "Governments": "government",
    "NonInstitutions": "public",
}
PCT_FACT = "ShareholdingAsAPercentageOfTotalNumberOfShares"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _holder_category(ctx_prefix: str, is_promoter: bool) -> str:
    p = ctx_prefix.lower()
    if is_promoter or "promoter" in p:
        return "promoter"
    if "mutualfund" in p:
        return "mf"
    if "foreign" in p and ("portfolio" in p or "institution" in p or "venture" in p):
        return "fii"
    if any(w in p for w in ("insurance", "bank", "alternativeinvestment", "providentfund", "pensionfund",
                            "financialinstitution", "sovereignwealth", "nbfc", "institutionsdomestic",
                            "otherfinancial")):
        return "dii"
    return "public"


def parse_shp_xbrl(raw: bytes) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns (category totals, named holders, extra).

    totals:  quarter_end, category, pct          (pct 0-100)
    holders: quarter_end, name, category, pct, shares
    extra:   {"promoter_pledged": bool | None}
    In these filings the number sits in a generic fact (e.g. the % of shares) and the
    category or holder is carried by the context the fact points to.
    """
    root = ET.parse(io.BytesIO(raw)).getroot()
    quarter_end = None
    ctx_ids: set[str] = set()
    for el in root:
        if _local(el.tag) != "context":
            continue
        cid = el.get("id")
        ctx_ids.add(cid)
        if cid == "MainI":
            inst = next((x.text for x in el.iter() if _local(x.tag) == "instant"), None)
            quarter_end = pd.Timestamp(inst) if inst else None
    facts: dict[str, dict[str, str]] = {}
    pledged = None
    for el in root:
        name, ref = _local(el.tag), el.get("contextRef")
        if not ref:
            continue
        if name.startswith("WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged"):
            pledged = (pledged or False) or (str(el.text).strip().lower() == "true")
        facts.setdefault(ref, {})[name] = (el.text or "").strip()

    totals, holders = [], []
    for ref, f in facts.items():
        m = re.match(r"(?:D_)?(.+?)_Context(\w+)$", ref)
        if not m or ref.startswith("D_"):
            continue
        prefix, suffix = m.group(1), m.group(2)
        pct = _num(f.get(PCT_FACT))
        if suffix == "I" and prefix in XBRL_TOTALS and pct is not None:
            totals.append((quarter_end, XBRL_TOTALS[prefix], pct * 100))
            continue
        # per-holder facts: the name may sit on the instant or on the matching D_ context
        nm = f.get("NameOfTheShareholder") or facts.get("D_" + ref, {}).get("NameOfTheShareholder")
        if nm and pct is not None:
            promo = bool(f.get("TypeOfPromoterShareholding")
                         or facts.get("D_" + ref, {}).get("TypeOfPromoterShareholding"))
            holders.append((quarter_end, re.sub(r"\s+", " ", nm).strip(), _holder_category(prefix, promo),
                            pct * 100, _num(f.get("NumberOfShares") or f.get("NumberOfFullyPaidUpEquityShares"))))
    t = pd.DataFrame(totals, columns=["quarter_end", "category", "pct"])
    h = pd.DataFrame(holders, columns=["quarter_end", "name", "category", "pct", "shares"])
    # the same holder can appear in two sub-tables (e.g. FPI category I and a >1% list)
    h = h.sort_values("pct", ascending=False).drop_duplicates(["name", "category"]).reset_index(drop=True)
    return t, h, {"promoter_pledged": pledged}


# ---------------------------------------------------------------- pledges
def parse_pledge(raw: bytes | str | dict) -> pd.DataFrame:
    """as_of, promoter_pct, pledge_pct (% of promoter shares pledged).

    The pledge API's record layout is not documented and it answers {"data": []}
    for companies without pledges. Keys are matched by name so small renames don't
    break it; anything unreadable gives an empty frame."""
    data = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    recs = data.get("data", []) if isinstance(data, dict) else data or []
    rows = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        low = {k.lower(): v for k, v in r.items()}
        d = next((pd.to_datetime(v, dayfirst=True, errors="coerce") for k, v in low.items()
                  if k in ("shp", "date", "asondate", "broadcastdt", "quarter")), None)
        pledge = next((_num(v) for k, v in low.items()
                       if "pledg" in k and ("per" in k or "pct" in k) and "prom" in k), None)
        if pledge is None:
            pledge = next((_num(v) for k, v in low.items() if "pledg" in k and ("per" in k or "pct" in k)), None)
        prom = next((_num(v) for k, v in low.items()
                     if "prom" in k and ("per" in k or "pct" in k) and "pledg" not in k), None)
        if d is not None and not pd.isna(d) and pledge is not None:
            rows.append((d, prom, pledge))
    return pd.DataFrame(rows, columns=["as_of", "promoter_pct", "pledge_pct"])


# ---------------------------------------------------------------- bulk / block deals
def parse_deals_csv(raw: bytes, kind: str) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), dtype=str, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    if df.empty:
        return pd.DataFrame(columns=["date", "symbol", "kind", "client", "side", "qty", "price", "source"])
    price_col = next(c for c in df.columns if c.lower().startswith("trade price"))
    out = pd.DataFrame({
        "date": pd.to_datetime(df["Date"].str.strip(), format="%d-%b-%Y", errors="coerce"),
        "symbol": df["Symbol"].str.strip(),
        "kind": kind,
        "client": df["Client Name"].str.strip(),
        "side": df["Buy/Sell"].str.strip().str.upper(),
        "qty": pd.to_numeric(df["Quantity Traded"].str.replace(",", ""), errors="coerce"),
        "price": pd.to_numeric(df[price_col].str.replace(",", ""), errors="coerce"),
        "source": "nse_archive",
    })
    return out.dropna(subset=["date", "symbol", "qty", "price"])


def parse_deals_api(raw: bytes | str | dict, kind: str) -> pd.DataFrame:
    """/api/historical/bulk-deals JSON. Field names (BD_*) as published; read by suffix."""
    data = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    recs = data.get("data", []) if isinstance(data, dict) else data or []

    def pick(r, *names):
        for n in names:
            for k, v in r.items():
                if k.upper().endswith(n):
                    return v
        return None
    rows = [{"date": pd.to_datetime(pick(r, "DT_DATE", "DATE"), dayfirst=True, errors="coerce"),
             "symbol": pick(r, "SYMBOL"), "kind": kind, "client": pick(r, "CLIENT_NAME"),
             "side": str(pick(r, "BUY_SELL") or "").upper(), "qty": _num(pick(r, "QTY_TRD")),
             "price": _num(pick(r, "TP_WATP")), "source": "nse_api"} for r in recs]
    return pd.DataFrame(rows, columns=["date", "symbol", "kind", "client", "side", "qty", "price", "source"]) \
        .dropna(subset=["date", "symbol", "qty", "price"])


# ---------------------------------------------------------------- FII / DII flows
def parse_fiidii(raw: bytes | str | list) -> pd.DataFrame:
    data = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    rows = []
    for r in data or []:
        cat = str(r.get("category", "")).upper()
        cat = "FII" if "FII" in cat or "FPI" in cat else "DII" if "DII" in cat else cat
        rows.append({"date": pd.to_datetime(r.get("date"), format="%d-%b-%Y", errors="coerce"), "category": cat,
                     "buy_cr": _num(r.get("buyValue")), "sell_cr": _num(r.get("sellValue")),
                     "net_cr": _num(r.get("netValue")), "source": "nse_api"})
    return pd.DataFrame(rows, columns=["date", "category", "buy_cr", "sell_cr", "net_cr", "source"]) \
        .dropna(subset=["date"])


# ---------------------------------------------------------------- client
class NseSmartMoney:
    def __init__(self, http: HttpClient):
        self.http = http

    def _api(self, path: str, cache: str | None = None) -> bytes:
        url = WWW + path
        if cache:
            return self.http.get_cached(url, cache, refresh=True)
        return self.http.get(url)

    def shp_master(self, symbol: str) -> pd.DataFrame:
        raw = self._api(f"/api/corporate-share-holdings-master?index=equities&symbol={symbol}",
                        f"nse/shp/master/{date.today():%Y%m%d}/{symbol}.json")
        return parse_shp_master(raw)

    def shp_xbrl(self, url: str, symbol: str, quarter_end) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
        # filings never change once published (a revision gets a new file name), so cache forever
        name = url.rsplit("/", 1)[-1]
        raw = self.http.get_cached(url, f"nse/shp/xbrl/{symbol}/{pd.Timestamp(quarter_end):%Y%m%d}_{name}")
        return parse_shp_xbrl(raw)

    def pledges(self, symbol: str) -> pd.DataFrame:
        raw = self._api(f"/api/corporate-pledgedata?index=equities&symbol={symbol}",
                        f"nse/pledge/{date.today():%Y%m%d}/{symbol}.json")
        return parse_pledge(raw)

    def deals_today(self) -> pd.DataFrame:
        frames = []
        for kind in ("bulk", "block"):
            raw = self.http.get_cached(f"{ARCHIVE}/content/equities/{kind}.csv",
                                       f"nse/deals/{date.today():%Y%m%d}_{kind}.csv", refresh=True)
            frames.append(parse_deals_csv(raw, kind))
        return pd.concat(frames, ignore_index=True)

    def deals_history(self, start: date, end: date) -> pd.DataFrame:
        frames = []
        for kind in ("bulk", "block"):
            raw = self._api(f"/api/historical/{kind}-deals?from={start:%d-%m-%Y}&to={end:%d-%m-%Y}")
            frames.append(parse_deals_api(raw, kind))
        return pd.concat(frames, ignore_index=True)

    def fiidii(self) -> pd.DataFrame:
        return parse_fiidii(self._api("/api/fiidiiTradeReact"))
