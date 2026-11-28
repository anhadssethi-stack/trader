"""Command line entry point.

  python -m nse_research.cli init                 # fetch Nifty 500 + F&O lists
  python -m nse_research.cli backfill-fast        # 10y history from Yahoo in minutes (no delivery)
  python -m nse_research.cli update               # fetch missing days from NSE (falls back to Yahoo)
  python -m nse_research.cli update --since 2016-01-01   # full NSE backfill (slow, resumable)
  python -m nse_research.cli scan                 # run the technical scanner
  python -m nse_research.cli daily                # update + scan (what the scheduler runs)
  python -m nse_research.cli health               # what was fetched, from where, what failed
  python -m nse_research.cli check-data           # look for missed splits / bad prices
  python -m nse_research.cli repair-splits        # adjust splits/bonuses NSE didn't flag (whole history)
  python -m nse_research.cli backtest             # backtest every setup over all stored history
  python -m nse_research.cli fundamentals         # results, ratios, red flags (Screener, Yahoo fallback)
  python -m nse_research.cli smart-money          # shareholding filings, bulk/block deals, FII/DII flows
  python -m nse_research.cli demo                 # build a synthetic demo database
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from .settings import ROOT, load_settings


def _store(settings, db: str | None):
    from .data.store import Store
    return Store(db or settings.path("db_path"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nse_research")
    ap.add_argument("--settings", help="path to settings.yaml")
    ap.add_argument("--db", help="override database path")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    bf = sub.add_parser("backfill-fast")
    bf.add_argument("--years", type=int)
    up = sub.add_parser("update")
    up.add_argument("--since", type=date.fromisoformat)
    up.add_argument("--until", type=date.fromisoformat)
    sub.add_parser("scan")
    sub.add_parser("daily")
    sub.add_parser("health")
    sub.add_parser("check-data")
    sub.add_parser("repair-splits")
    sub.add_parser("demo")
    for name in ("fundamentals", "smart-money"):
        fp = sub.add_parser(name)
        fp.add_argument("--symbols", nargs="*", help="only these symbols (default: the universe)")
        fp.add_argument("--limit", type=int, help="fetch at most this many stocks this run")
        fp.add_argument("--force", action="store_true", help="re-fetch even if fetched recently")
    btp = sub.add_parser("backtest")
    btp.add_argument("--setups", nargs="*", help="setup ids (default: all)")
    btp.add_argument("--symbols", nargs="*", help="limit to these symbols (for a quick test)")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings(a.settings)

    if a.cmd == "backtest":
        from .backtest.engine import run_backtest
        from .backtest.stats import setup_summary_table, warnings_for
        st = _store(s, a.db)
        trades, grid, meta = run_backtest(s, st, a.setups, a.symbols)
        run_id = date.today().isoformat() + "_" + str(int(__import__("time").time()))
        st.save_backtest(run_id, trades, grid, meta)
        tab = setup_summary_table(trades, grid, s, meta["setups"])
        if trades.empty:
            print("No trades. Is there price history stored? (see README)")
            return 0
        cols = ["setup", "trades", "win_rate", "win_ci_low", "win_ci_high", "expectancy_r", "profit_factor",
                "max_dd_pct", "oos_expectancy_r", "wf_expectancy_r", "wf_fixed_expectancy_r", "confidence"]
        print(f"Tested {meta['symbols_tested']} stocks with at least "
              f"{s['scanner']['min_history_days']} days of history.\n")
        print(tab.reindex(columns=cols).round(2).to_string(index=False))
        for r in tab.itertuples():
            if r.trades == 0:
                print(f"\n{r.setup}: no trades. " + " ".join(warnings_for(r._asdict(), s["backtest"])))
        print(f"\nSaved as run {run_id}. Open the Backtest page in the dashboard for details.")
        return 0

    if a.cmd == "demo":
        from .demo import build_demo
        from .scanner.engine import run_scan
        path = Path(a.db or ROOT / "data" / "demo.duckdb")
        st = build_demo(path)
        df = run_scan(s, st, st.universe()["symbol"].tolist())
        print(f"Demo database written to {path} ({len(df)} stocks scanned).")
        print(f"Open it with:  NSE_RESEARCH_DB={path} streamlit run app/streamlit_app.py")
        return 0

    from .data.pipeline import Pipeline
    from .scanner.engine import run_scan

    st = _store(s, a.db)
    pipe = Pipeline(s, st)
    if a.cmd == "init":
        pipe.refresh_lists(force=True)
        print(f"Universe: {len(st.universe())} symbols, F&O: {len(st.fno_symbols())} symbols")
    elif a.cmd == "backfill-fast":
        rep = pipe.backfill_yahoo(a.years)
        print(rep.summary())
    elif a.cmd == "update":
        rep = pipe.update(a.since, a.until)
        print(rep.summary())
    elif a.cmd == "scan":
        df = run_scan(s, st, pipe.scan_symbols())
        print(f"Scanned {len(df)} stocks.")
    elif a.cmd in ("fundamentals", "smart-money"):
        syms = a.symbols or pipe.scan_symbols()
        if a.cmd == "fundamentals":
            from .fundamentals.pipeline import update_fundamentals
            from .fundamentals.screen import build_screen
            frep = update_fundamentals(s, st, syms, force=a.force, limit=a.limit)
            print(frep.summary())
            scr = build_screen(s, st)
            if len(scr):
                print(f"\n{len(scr)} companies screened: {int((scr['n_red'] == 0).sum())} with no red flag. "
                      "Most common red flags:")
                print(scr["red_flags"].explode().value_counts().head(10).to_string())
        else:
            from .smartmoney import pipeline as sm
            nse, srep = sm.client(s), sm.SmartReport()
            sm.update_deals(s, st, nse, srep)
            sm.update_flows(st, nse, srep)
            sm.update_shareholding(s, st, nse, syms, srep, force=a.force, limit=a.limit)
            print(srep.summary())
    elif a.cmd == "daily":
        rep = pipe.update()
        print(rep.summary())
        df = run_scan(s, st, pipe.scan_symbols())
        print(f"Scanned {len(df)} stocks.")
        # Phase 3: deals + flows every day; fundamentals and filings for a slice of stale stocks
        from .fundamentals.pipeline import update_fundamentals
        from .smartmoney import pipeline as sm
        nse, srep = sm.client(s), sm.SmartReport()
        sm.update_deals(s, st, nse, srep)
        sm.update_flows(st, nse, srep)
        lim = s["smart_money"]["daily_limit"]
        sm.update_shareholding(s, st, nse, pipe.scan_symbols(), srep, limit=lim)
        print(srep.summary())
        print(update_fundamentals(s, st, pipe.scan_symbols(), limit=lim).summary())
        if rep.errors:
            return 2  # non-zero so the scheduler log shows something needs a look
    elif a.cmd == "repair-splits":
        gaps = pipe.infer_gap_actions(date(1990, 1, 1))
        print(f"Added {len(gaps)} splits/bonuses that NSE's files did not flag.")
        if len(gaps):
            print(gaps.sort_values("factor").head(30).to_string(index=False))
        print("\nNow run: python -m nse_research.cli check-data")
    elif a.cmd == "check-data":
        from .data.quality import suspicious_moves
        bad = suspicious_moves(st, s["universe"]["series"])
        funds = st.fund_symbols()
        if len(bad) and funds:
            nf = bad["symbol"].isin(funds).sum()
            bad = bad[~bad["symbol"].isin(funds)]
            print(f"({nf} moves in ETFs/fund units are left out: the backtest skips those.)")
        n_sym = st.q("SELECT count(DISTINCT symbol) AS n FROM prices")["n"].iloc[0]
        print(f"{len(bad)} one-day moves above 25% in split-adjusted prices, across "
              f"{bad['symbol'].nunique() if len(bad) else 0} of {n_sym} stocks.")
        if len(bad):
            print("Large falls often mean a split/bonus that was not detected. Biggest:")
            print(bad.head(25).round({"last_close": 2, "close": 2, "move_pct": 1, "prev_close": 2})
                  .to_string(index=False))
        runs = st.backtest_runs()
        if not runs.empty:
            tr, _, _ = st.load_backtest(runs["run_id"].iloc[0])
            done = tr[tr["exit_reason"].isin(["stop", "target", "time", "data_end"])]
            if len(done):
                big = done[done["r"] < -2]
                print(f"\nLatest backtest: {len(big)} of {len(done)} trades lost more than 2R "
                      f"({big['r'].sum():.0f}R of the {done.loc[done['r'] < 0, 'r'].sum():.0f}R lost in total).")
                if len(big):
                    print(big.nsmallest(15, "r")[["setup", "symbol", "entry_date", "exit_date", "entry",
                                                  "exit", "exit_reason", "r"]].round({"entry": 2, "exit": 2, "r": 2})
                          .to_string(index=False))
    elif a.cmd == "health":
        print(st.q("""SELECT dataset, source, status, count(*) AS n, max(day) AS latest_day
                      FROM fetch_log GROUP BY ALL ORDER BY dataset, source, status""").to_string(index=False))
        print("\nLast stored price day:", st.last_price_date())
    return 0


if __name__ == "__main__":
    sys.exit(main())
