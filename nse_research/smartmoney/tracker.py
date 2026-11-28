"""Smart-money tracker: who is adding to or cutting a stock.

Three inputs, each with its own delay (shown in the dashboard):
  * Quarterly shareholding patterns (promoter / FII / DII / mutual fund / public %).
    Companies file within 21 days of quarter end, so this is 3-15 weeks old.
  * Named holders above 1% from the same filing. "Well-known investors" are matched
    by name against the list in settings.yaml (smart_money.investors).
  * Bulk and block deals: published by NSE after market close the same day.

Nothing here predicts anything. It lists what changed, by whom, and when.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd

CATEGORY_LABELS = {"promoter": "Promoters", "fii": "FIIs", "dii": "DIIs", "mf": "Mutual funds",
                   "government": "Government", "public": "Public"}


def stake_changes(shareholding: pd.DataFrame, min_change_pp: float,
                  categories: tuple[str, ...] = ("fii", "dii", "mf", "promoter")) -> pd.DataFrame:
    """Quarter-on-quarter change per stock and holder category.

    shareholding: symbol, quarter_end, category, pct (one row per filing line).
    Returns rows where |change| >= min_change_pp for the latest quarter of each stock.
    """
    cols = ["symbol", "category", "holder", "quarter_end", "prev_quarter", "pct", "prev_pct", "change_pp"]
    if shareholding is None or shareholding.empty:
        return pd.DataFrame(columns=cols)
    sh = shareholding[shareholding["category"].isin(categories)].copy()
    sh["quarter_end"] = pd.to_datetime(sh["quarter_end"])
    sh = sh.sort_values(["symbol", "category", "quarter_end"]).drop_duplicates(
        ["symbol", "category", "quarter_end"], keep="last")
    sh["prev_pct"] = sh.groupby(["symbol", "category"])["pct"].shift(1)
    sh["prev_quarter"] = sh.groupby(["symbol", "category"])["quarter_end"].shift(1)
    latest = sh.groupby("symbol")["quarter_end"].transform("max")
    out = sh[(sh["quarter_end"] == latest) & sh["prev_pct"].notna()].copy()
    out["change_pp"] = out["pct"] - out["prev_pct"]
    out = out[out["change_pp"].abs() >= min_change_pp]
    out["holder"] = out["category"].map(CATEGORY_LABELS)
    return out[cols].sort_values("change_pp", ascending=False).reset_index(drop=True)


def _matcher(investors: list[dict]):
    pats = [(i["name"], re.compile("|".join(i["match"]), re.I)) for i in investors or []]

    def match(name: str | None) -> str | None:
        if not name:
            return None
        for label, p in pats:
            if p.search(name):
                return label
        return None
    return match


def holder_changes(holders: pd.DataFrame, investors: list[dict], min_change_pp: float,
                   only_known: bool = False) -> pd.DataFrame:
    """Named holders (>1% filings): new entries, increases, cuts and exits between the
    two latest quarters each stock filed. `known_investor` is set when the name matches
    the watch list. An exit means the holder dropped below the 1% disclosure line or sold out."""
    cols = ["symbol", "holder", "known_investor", "category", "quarter_end", "prev_quarter",
            "pct", "prev_pct", "change_pp", "event"]
    if holders is None or holders.empty:
        return pd.DataFrame(columns=cols)
    h = holders.copy()
    h["quarter_end"] = pd.to_datetime(h["quarter_end"])
    h["key"] = h["name"].str.upper().str.replace(r"[^A-Z0-9 ]", "", regex=True).str.split().str.join(" ")
    rows = []
    for sym, g in h.groupby("symbol"):
        qs = sorted(g["quarter_end"].unique())
        if len(qs) < 1:
            continue
        now = g[g["quarter_end"] == qs[-1]].groupby("key").agg(
            name=("name", "first"), category=("category", "first"), pct=("pct", "sum"))
        prev_q = qs[-2] if len(qs) >= 2 else None
        prev = g[g["quarter_end"] == prev_q].groupby("key").agg(
            name=("name", "first"), category=("category", "first"), pct=("pct", "sum")) \
            if prev_q is not None else now.iloc[0:0]
        for key in now.index.union(prev.index):
            a = now["pct"].get(key)
            b = prev["pct"].get(key)
            if prev_q is None:
                continue
            if b is None or b != b:
                ev, chg = "new above 1%", a
            elif a is None or a != a:
                ev, chg = "exited / below 1%", -b
            else:
                chg = a - b
                ev = "increased" if chg > 0 else "reduced"
            if abs(chg) < min_change_pp:
                continue
            src = now if key in now.index else prev
            rows.append({"symbol": sym, "holder": src.loc[key, "name"], "category": src.loc[key, "category"],
                         "quarter_end": qs[-1], "prev_quarter": prev_q,
                         "pct": a, "prev_pct": b, "change_pp": chg, "event": ev})
    out = pd.DataFrame(rows, columns=[c for c in cols if c != "known_investor"])
    match = _matcher(investors)
    out["known_investor"] = out["holder"].map(match)
    if only_known:
        out = out[out["known_investor"].notna()]
    return out[cols].sort_values(["known_investor", "change_pp"], ascending=[True, False],
                                 na_position="last").reset_index(drop=True)


def recent_deals(deals: pd.DataFrame, investors: list[dict], lookback_days: int,
                 today: date | None = None) -> pd.DataFrame:
    """Bulk/block deals in the last `lookback_days`, newest first, with the watch-list match."""
    cols = ["date", "symbol", "kind", "client", "known_investor", "side", "qty", "price", "value_cr"]
    if deals is None or deals.empty:
        return pd.DataFrame(columns=cols)
    d = deals.copy()
    d["date"] = pd.to_datetime(d["date"])
    since = pd.Timestamp((today or date.today()) - timedelta(days=lookback_days))
    d = d[d["date"] >= since]
    d["value_cr"] = d["qty"] * d["price"] / 1e7
    d["known_investor"] = d["client"].map(_matcher(investors))
    return d.reindex(columns=cols).sort_values(["date", "value_cr"], ascending=[False, False]).reset_index(drop=True)


def deal_summary(deals: pd.DataFrame) -> pd.DataFrame:
    """Per stock: net bought value by deal clients (buys minus sells), number of deals,
    and the clients involved. Input is recent_deals() output."""
    if deals is None or deals.empty:
        return pd.DataFrame(columns=["symbol", "deals", "net_value_cr", "buy_value_cr", "sell_value_cr",
                                     "last_deal", "clients", "known_investors"])
    d = deals.copy()
    d["signed"] = d["value_cr"].where(d["side"] == "BUY", -d["value_cr"])
    g = d.groupby("symbol")
    out = pd.DataFrame({
        "deals": g.size(),
        "net_value_cr": g["signed"].sum(),
        "buy_value_cr": g.apply(lambda x: x.loc[x["side"] == "BUY", "value_cr"].sum(), include_groups=False),
        "sell_value_cr": g.apply(lambda x: x.loc[x["side"] == "SELL", "value_cr"].sum(), include_groups=False),
        "last_deal": g["date"].max(),
        "clients": g["client"].agg(lambda x: ", ".join(sorted(set(x))[:5])),
        "known_investors": g["known_investor"].agg(lambda x: ", ".join(sorted(set(x.dropna())))),
    }).reset_index()
    return out.sort_values("net_value_cr", ascending=False).reset_index(drop=True)
