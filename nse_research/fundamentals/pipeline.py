"""Fetch fundamentals for the universe: Screener first, Yahoo as the fallback,
each stock at most once per `refresh_days`. Resumable: stop it any time and the
next run carries on with the stocks that are still missing or stale."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..data.http import HttpClient
from ..data.store import Store
from ..settings import Settings

log = logging.getLogger(__name__)


@dataclass
class FundReport:
    fetched: dict = field(default_factory=dict)    # source -> count
    skipped_fresh: int = 0
    failed: list = field(default_factory=list)     # (symbol, reason)

    def summary(self) -> str:
        got = ", ".join(f"{n} from {s}" for s, n in self.fetched.items()) or "none"
        lines = [f"Fundamentals fetched: {got}. Already fresh: {self.skipped_fresh}. Failed: {len(self.failed)}."]
        for sym, why in self.failed[:15]:
            lines.append(f"  {sym}: {why}")
        return "\n".join(lines)


def make_sources(settings: Settings, http: HttpClient | None = None) -> list:
    from .sources.screener import ScreenerSource
    from .sources.yahoo import YahooFundamentals
    cfg = settings["fundamentals"]
    d = settings["data"]
    http = http or HttpClient(settings.path("cache_dir"), delay=cfg["request_delay_sec"],
                              timeout=d["request_timeout_sec"], retries=d["max_retries"])
    avail = {"screener": lambda: ScreenerSource(http, cfg["basis"]), "yahoo": YahooFundamentals}
    return [avail[n]() for n in cfg["sources"] if n in avail]


def update_fundamentals(settings: Settings, store: Store, symbols: list[str], force: bool = False,
                        sources: list | None = None, limit: int | None = None) -> FundReport:
    cfg = settings["fundamentals"]
    sources = sources if sources is not None else make_sources(settings)
    have = store.fundamentals_fetched()
    stale_before = date.today() - timedelta(days=cfg["refresh_days"])
    rep = FundReport()
    todo = [s for s in symbols if force or s not in have or have[s] <= stale_before]
    rep.skipped_fresh = len(symbols) - len(todo)
    if limit:
        todo = todo[:limit]
    for i, sym in enumerate(todo, 1):
        errors = []
        for src in sources:
            try:
                f = src.fetch(sym)
            except Exception as e:  # noqa: BLE001 - try the next source
                errors.append(f"{src.name}: {type(e).__name__} {str(e)[:120]}")
                continue
            if f.statements.empty:
                errors.append(f"{src.name}: no statements on the page")
                continue
            f.fetched = date.today()
            store.save_fundamentals(f)
            store.log_fetch(None, "fundamentals", src.name, "ok", len(f.statements), sym)
            rep.fetched[src.name] = rep.fetched.get(src.name, 0) + 1
            break
        else:
            if all("NotFound" in e or "no statements" in e for e in errors):
                errors.append("symbol renamed or delisted? (e.g. ZOMATO is now ETERNAL); run `init` to refresh the list")
            rep.failed.append((sym, "; ".join(errors)))
            store.log_fetch(None, "fundamentals", "all", "failed", 0, f"{sym}: {'; '.join(errors)}")
        if i % 25 == 0:
            log.info("fundamentals: %d / %d", i, len(todo))
    return rep
