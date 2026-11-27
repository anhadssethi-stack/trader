"""Fetch orchestration: for each market day, try each source in order, cache raw
files, store normalised rows, and log every attempt. One source failing never stops
the run: the next source is tried, and anything still missing is reported on the
Data Health page and retried on the next run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from ..settings import Settings
from .adjust import detect_corp_actions
from .http import FetchError, HttpClient, NotFound
from .sources.nse import (NseFullBhavcopy, NseIndexClose, NseLegacyBhavcopy, NseLists, NseMto,
                          NseUdiffBhavcopy)
from .sources.yahoo import YahooSource
from .store import Store

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def latest_expected_day(publish_hour: int, now: datetime | None = None) -> date:
    """Most recent weekday whose EOD files should already be published."""
    now = now or now_ist()
    d = now.date() if now.hour >= publish_hour else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def weekdays(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.bdate_range(start, end)]


@dataclass
class RunReport:
    days_requested: int = 0
    prices: dict = field(default_factory=dict)      # day -> source or status
    delivery: dict = field(default_factory=dict)
    index: dict = field(default_factory=dict)
    corp_actions_found: int = 0
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        def count(d: dict) -> str:
            s = pd.Series(list(d.values()), dtype=object).value_counts().to_dict() if d else {}
            return ", ".join(f"{k}: {v}" for k, v in s.items()) or "nothing to do"
        return (f"days checked: {self.days_requested}\n  prices   -> {count(self.prices)}\n"
                f"  delivery -> {count(self.delivery)}\n  index    -> {count(self.index)}\n"
                f"  new corporate actions: {self.corp_actions_found}"
                + (f"\n  errors: {len(self.errors)} (see Data Health page)" if self.errors else ""))


class Pipeline:
    def __init__(self, settings: Settings, store: Store, http: HttpClient | None = None,
                 sources: dict | None = None):
        self.s = settings
        self.store = store
        d = settings["data"]
        self.http = http or HttpClient(settings.path("cache_dir"), d["request_delay_sec"],
                                       d["request_timeout_sec"], d["max_retries"])
        if sources is None:
            full = NseFullBhavcopy(self.http)
            sources = {
                "nse_udiff": NseUdiffBhavcopy(self.http), "nse_legacy": NseLegacyBhavcopy(self.http),
                "nse_full_bhav": full, "nse_mto": NseMto(self.http),
                "nse_index_close": NseIndexClose(self.http),
                "yahoo": YahooSource(cache_dir=settings.path("cache_dir")),
                "lists": NseLists(self.http),
            }
        self.src = sources

    # ------------------------------------------------------------ universe
    def refresh_lists(self, force: bool = False) -> None:
        last = self.store.get_meta("lists_refreshed")
        if not force and last and (date.today() - date.fromisoformat(last)).days < 7:
            return
        lists = self.src["lists"]
        try:
            n500 = lists.index_constituents("nifty500")
            self.store.replace_universe(n500, "nifty500")
            self.store.log_fetch(None, "universe", "nse_lists", "ok", len(n500))
        except Exception as e:  # noqa: BLE001
            self.store.log_fetch(None, "universe", "nse_lists", "error", 0, str(e))
            log.warning("Nifty 500 list refresh failed, keeping the stored list: %s", e)
        try:
            fno = lists.fno_symbols()
            self.store.replace_fno(fno["symbol"].tolist())
            self.store.log_fetch(None, "fno_list", "nse_lists", "ok", len(fno))
        except Exception as e:  # noqa: BLE001
            self.store.log_fetch(None, "fno_list", "nse_lists", "error", 0, str(e))
            log.warning("F&O list refresh failed, keeping the stored list: %s", e)
        self.store.set_meta("lists_refreshed", date.today().isoformat())

    def scan_symbols(self) -> list[str]:
        u = self.s["universe"]
        if u["mode"] == "all_nse":
            syms = self.store.all_symbols(u["series"])
        elif u["mode"] == "custom":
            syms = list(u["custom_symbols"])
        else:
            syms = self.store.universe(u["mode"])["symbol"].tolist()
        return sorted(set(syms) | set(u.get("extra_symbols") or []))

    # ------------------------------------------------------------ helpers
    def _known_holidays(self) -> set[date]:
        df = self.store.q("SELECT DISTINCT day FROM fetch_log WHERE dataset='prices' AND status='holiday'")
        return {pd.Timestamp(x).date() for x in df["day"]}

    def _try_chain(self, d: date, chain: list[str], method: str, dataset: str
                   ) -> tuple[pd.DataFrame | None, str, str]:
        """Try sources in order. Returns (frame, source_or_status, message)."""
        all_not_found, msgs = True, []
        for name in chain:
            src = self.src.get(name)
            if src is None or not callable(getattr(src, method, None)) or name == "yahoo":
                continue
            if hasattr(src, "supports") and not src.supports(d):
                continue
            try:
                df = getattr(src, method)(d)
            except NotFound as e:
                msgs.append(f"{name}: not found")
                self.store.log_fetch(d, dataset, name, "not_found", 0, str(e))
                continue
            except (FetchError, Exception) as e:  # noqa: BLE001 - parse errors too
                all_not_found = False
                msgs.append(f"{name}: {type(e).__name__}: {e}")
                self.store.log_fetch(d, dataset, name, "error", 0, f"{type(e).__name__}: {e}")
                continue
            if df is None or df.empty:
                msgs.append(f"{name}: empty file")
                all_not_found = False
                self.store.log_fetch(d, dataset, name, "empty", 0)
                continue
            self.store.log_fetch(d, dataset, name, "ok", len(df))
            return df, name, ""
        return None, ("not_found" if all_not_found else "error"), "; ".join(msgs)

    # ------------------------------------------------------------ main entry points
    def update(self, start: date | None = None, end: date | None = None, retry_missing: bool = True,
               use_nse: bool = True) -> RunReport:
        """Fetch every missing market day between start and end (default: since last stored day)."""
        d = self.s["data"]
        end = end or latest_expected_day(d["publish_hour_ist"])
        if start is None:
            # Default: new days since the last stored day, plus a short look-back so days
            # that were filled from Yahoo (NSE was down) get upgraded to NSE + delivery.
            last = self.store.last_price_date()
            start = (min(last + timedelta(days=1), end - timedelta(days=d.get("retry_window_days", 14)))
                     if last else end - timedelta(days=int(365.25 * d["history_years"])))
        self.refresh_lists()
        have_p = self.store.stored_days("prices", exclude_source="yahoo")
        have_d = self.store.stored_days("delivery")
        have_i = self.store.stored_days("index")
        holidays = self._known_holidays()
        days = [x for x in weekdays(start, end) if x not in holidays]
        rep = RunReport(days_requested=len(days))
        series = self.s["universe"]["series"]
        missing_price, missing_index = [], []

        for i, day in enumerate(days):
            if i and i % 50 == 0:
                log.info("... %d/%d days", i, len(days))
            if day not in have_p:
                df = None
                if use_nse:
                    df, how, msg = self._try_chain(day, d["price_sources"], "fetch_day", "prices")
                else:
                    how, msg = "skipped", ""
                if df is not None:
                    df = df[df["series"].isin(series)]
                    self.store.upsert_prices(df)
                    rep.prices[day] = how
                else:
                    missing_price.append(day)
                    rep.prices[day] = how
                    if how == "error":
                        rep.errors.append((day, "prices", msg))
            if use_nse and day not in have_d and rep.prices.get(day) not in ("not_found",):
                df, how, msg = self._try_chain(day, d["delivery_sources"], "fetch_delivery", "delivery")
                if df is not None:
                    self.store.upsert_delivery(df[df["series"].isin(series)])
                rep.delivery[day] = how
            if day not in have_i:
                df = None
                if use_nse and "nse_index_close" in d["index_sources"]:
                    df, how, msg = self._try_chain(day, ["nse_index_close"], "fetch_day", "index")
                if df is not None:
                    self.store.upsert_index(df)
                    rep.index[day] = how
                else:
                    missing_index.append(day)

        if missing_price and "yahoo" in d["price_sources"]:
            self._yahoo_prices(missing_price, rep)
        if missing_index and "yahoo" in d["index_sources"]:
            self._yahoo_index(missing_index, rep)

        # A weekday where every source had no file, more than a day ago, is a holiday.
        today = now_ist().date()
        for day, how in rep.prices.items():
            if how == "not_found" and (today - day).days > 1:
                self.store.log_fetch(day, "prices", "all", "holiday", 0, "no file from any source")
                rep.prices[day] = "holiday"

        # Re-check splits from the earliest new day, or from where an interrupted run stopped.
        checked = self.store.get_meta("ca_checked_through")
        since = min(rep.prices) if rep.prices else None
        if checked:
            since = min(since or date.fromisoformat(checked), date.fromisoformat(checked))
        elif since:
            since = date(1990, 1, 1)
        if since:
            rep.corp_actions_found = self.refresh_corp_actions(since)
            last = self.store.last_price_date()
            if last:
                self.store.set_meta("ca_checked_through", pd.Timestamp(last).date().isoformat())
        self.store.set_meta("last_update", now_ist().isoformat(timespec="seconds"))
        return rep

    def _yahoo_prices(self, days: list[date], rep: RunReport) -> None:
        syms = self.scan_symbols()
        if not syms:
            return
        try:
            px, ca = self.src["yahoo"].fetch_range(syms, min(days), max(days))
        except Exception as e:  # noqa: BLE001
            self.store.log_fetch(None, "prices", "yahoo", "error", 0, str(e))
            rep.errors.append((None, "prices", f"yahoo: {e}"))
            return
        if px.empty:
            self.store.log_fetch(None, "prices", "yahoo", "empty", 0)
            return
        px = px[px["date"].isin(set(days))]
        self.store.upsert_prices(px, keep_existing=True)
        self.store.upsert_corp_actions(ca)
        for day in set(px["date"]):
            rep.prices[day] = "yahoo"
            self.store.log_fetch(day, "prices", "yahoo", "ok", int((px["date"] == day).sum()))

    def _yahoo_index(self, days: list[date], rep: RunReport) -> None:
        b = self.s["benchmark"]
        try:
            ix = self.src["yahoo"].fetch_index(b["yahoo_ticker"], b["index_name"], min(days), max(days))
        except Exception as e:  # noqa: BLE001
            self.store.log_fetch(None, "index", "yahoo", "error", 0, str(e))
            return
        ix = ix[ix["date"].isin(set(days))]
        self.store.upsert_index(ix, keep_existing=True)
        for day in set(ix["date"]):
            rep.index[day] = "yahoo"

    def refresh_corp_actions(self, since: date) -> int:
        """Detect splits/bonuses from NSE prev-close for rows on/after `since`."""
        start = since - timedelta(days=15)
        raw = self.store.raw_prices(start=start, series=self.s["universe"]["series"])
        market_days = sorted(self.store.stored_days("prices"))
        found = detect_corp_actions(raw, self.s["data"]["corp_action_min_change"], market_days)
        found = found[pd.to_datetime(found["ex_date"]) >= pd.Timestamp(since)]
        if not found.empty:
            self.store.upsert_corp_actions(found)
            for r in found.itertuples():
                self.store.log_fetch(r.ex_date, "corp_action", r.source, "ok", 1,
                                     f"{r.symbol} factor {r.factor}")
        return len(found)

    def backfill_yahoo(self, years: int | None = None) -> RunReport:
        """Fast history load from Yahoo (minutes, no delivery data). NSE rows, when
        fetched later, replace these automatically."""
        years = years or self.s["data"]["history_years"]
        end = latest_expected_day(self.s["data"]["publish_hour_ist"])
        start = end - timedelta(days=int(365.25 * years))
        self.refresh_lists()
        have = self.store.stored_days("prices")
        days = [x for x in weekdays(start, end) if x not in have]
        rep = RunReport(days_requested=len(days))
        if days:
            self._yahoo_prices(days, rep)
            self._yahoo_index(weekdays(start, end), rep)
        return rep
