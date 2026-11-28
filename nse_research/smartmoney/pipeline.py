"""Fetch smart-money data into the store: deals and FII/DII flows (daily), and
shareholding filings (weekly per stock; a new filing only appears once a quarter)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from ..data.http import HttpClient
from ..data.store import Store
from ..settings import Settings
from .nse import NseSmartMoney

log = logging.getLogger(__name__)


@dataclass
class SmartReport:
    deals: int = 0
    flows: int = 0
    shp_symbols: int = 0
    shp_filings: int = 0
    skipped_fresh: int = 0
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        s = (f"Smart money: {self.deals} bulk/block deals, {self.flows} FII/DII flow rows, "
             f"shareholding for {self.shp_symbols} stocks ({self.shp_filings} filings read, "
             f"{self.skipped_fresh} already fresh).")
        if self.errors:
            s += f" {len(self.errors)} problems:\n" + "\n".join(f"  {e}" for e in self.errors[:15])
        return s


def client(settings: Settings) -> NseSmartMoney:
    d = settings["data"]
    return NseSmartMoney(HttpClient(settings.path("cache_dir"), delay=d["request_delay_sec"],
                                    timeout=d["request_timeout_sec"], retries=d["max_retries"]))


def update_deals(settings: Settings, store: Store, nse: NseSmartMoney, rep: SmartReport) -> None:
    try:
        d = nse.deals_today()
        rep.deals += store.upsert_deals(d)
        store.log_fetch(d["date"].max().date() if len(d) else None, "deals", "nse_archive", "ok", len(d))
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"deals today: {type(e).__name__} {e}")
        store.log_fetch(None, "deals", "nse_archive", "error", 0, str(e))
    # the daily files only cover the latest day; try the history API until it works once,
    # at most once a week (it often answers 503 for days)
    tried = store.get_meta("deals_backfill_tried")
    recent_try = tried and date.fromisoformat(tried) > date.today() - timedelta(days=7)
    if store.get_meta("deals_backfilled") != "1" and not recent_try:
        store.set_meta("deals_backfill_tried", date.today().isoformat())
        end = date.today()
        start = end - timedelta(days=settings["smart_money"]["deals_backfill_days"])
        got, failed = 0, 0
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=30), end)
            try:
                got += store.upsert_deals(nse.deals_history(cur, nxt))
            except Exception as e:  # noqa: BLE001 - this API often answers 503
                failed += 1
                log.info("historical deals %s..%s failed: %s", cur, nxt, e)
                break                      # down: don't spend minutes retrying every month
            cur = nxt + timedelta(days=1)
        rep.deals += got
        if failed == 0:
            store.set_meta("deals_backfilled", "1")
        else:
            rep.errors.append("deal history: NSE's history API is not answering (it is often down). It is tried "
                              "again in a week; meanwhile the daily files build up the history.")


def update_flows(store: Store, nse: NseSmartMoney, rep: SmartReport) -> None:
    try:
        f = nse.fiidii()
        rep.flows += store.upsert_flows(f)
        store.log_fetch(f["date"].max().date() if len(f) else None, "flows", "nse_api", "ok", len(f))
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"FII/DII flows: {type(e).__name__} {e}")
        store.log_fetch(None, "flows", "nse_api", "error", 0, str(e))


def update_shareholding_symbol(settings: Settings, store: Store, nse: NseSmartMoney, sym: str,
                               rep: SmartReport) -> None:
    cfg = settings["smart_money"]
    master = nse.shp_master(sym)
    if master.empty:
        raise ValueError("no shareholding filings listed")
    store.upsert_shareholding(pd.DataFrame({"symbol": sym, "quarter_end": master["quarter_end"],
                                            "category": "promoter", "pct": master["promoter_pct"],
                                            "source": "nse_master"}).dropna(subset=["pct"]))
    pledged_latest = None    # set when a filing says "pledged" without giving the number
    for r in master.dropna(subset=["xbrl"]).tail(cfg["xbrl_quarters"]).itertuples():
        totals, holders, extra = nse.shp_xbrl(r.xbrl, sym, r.quarter_end)
        rep.shp_filings += 1
        if len(totals):
            store.upsert_shareholding(totals.assign(symbol=sym, source="nse_xbrl"))
        if len(holders):
            store.upsert_holders(holders.assign(symbol=sym, source="nse_xbrl"))
        if extra.get("pledge_pct") is not None:
            prom = totals.loc[totals["category"] == "promoter", "pct"]
            store.upsert_pledges(pd.DataFrame({"symbol": [sym], "as_of": [r.quarter_end],
                                               "promoter_pct": [float(prom.iloc[0]) if len(prom) else None],
                                               "pledge_pct": [extra["pledge_pct"]], "source": ["nse_xbrl"]}))
        else:
            pledged_latest = (r.quarter_end, extra.get("promoter_pledged"))
    if pledged_latest is not None:
        # the filing doesn't give a number: try NSE's pledge API (often empty)
        try:
            pl = nse.pledges(sym)
        except Exception as e:  # noqa: BLE001 - pledge % is optional
            pl = pd.DataFrame()
            log.debug("pledge API failed for %s: %s", sym, e)
        if len(pl):
            store.upsert_pledges(pl.assign(symbol=sym, source="nse_pledge"))
    store.set_meta(f"shp_checked:{sym}", date.today().isoformat())


def update_shareholding(settings: Settings, store: Store, nse: NseSmartMoney, symbols: list[str],
                        rep: SmartReport, force: bool = False, limit: int | None = None) -> None:
    cfg = settings["smart_money"]
    stale = date.today() - timedelta(days=cfg["shareholding_refresh_days"])
    checked = {r.key.split(":", 1)[1]: r.value for r in
               store.q("SELECT key, value FROM meta WHERE key LIKE 'shp_checked:%'").itertuples()}
    todo = [s for s in symbols if force or s not in checked or date.fromisoformat(checked[s]) <= stale]
    rep.skipped_fresh += len(symbols) - len(todo)
    for i, sym in enumerate(todo[:limit] if limit else todo, 1):
        try:
            update_shareholding_symbol(settings, store, nse, sym, rep)
            rep.shp_symbols += 1
            store.log_fetch(None, "shareholding", "nse", "ok", 0, sym)
        except Exception as e:  # noqa: BLE001 - one stock must not stop the run
            rep.errors.append(f"shareholding {sym}: {type(e).__name__} {str(e)[:150]}")
            store.log_fetch(None, "shareholding", "nse", "error", 0, f"{sym}: {e}")
        if i % 25 == 0:
            log.info("shareholding: %d / %d", i, len(todo))
