"""Command line entry point.

  python -m nse_research.cli init                 # fetch Nifty 500 + F&O lists
  python -m nse_research.cli backfill-fast        # 10y history from Yahoo in minutes (no delivery)
  python -m nse_research.cli update               # fetch missing days from NSE (falls back to Yahoo)
  python -m nse_research.cli update --since 2016-01-01   # full NSE backfill (slow, resumable)
  python -m nse_research.cli scan                 # run the technical scanner
  python -m nse_research.cli daily                # update + scan (what the scheduler runs)
  python -m nse_research.cli health               # what was fetched, from where, what failed
  python -m nse_research.cli backtest             # backtest every setup over all stored history
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
    sub.add_parser("demo")
    btp = sub.add_parser("backtest")
    btp.add_argument("--setups", nargs="*", help="setup ids (default: all)")
    btp.add_argument("--symbols", nargs="*", help="limit to these symbols (for a quick test)")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings(a.settings)

    if a.cmd == "backtest":
        from .backtest.engine import run_backtest
        from .backtest.stats import setup_summary_table
        st = _store(s, a.db)
        trades, grid, meta = run_backtest(s, st, a.setups, a.symbols)
        run_id = date.today().isoformat() + "_" + str(int(__import__("time").time()))
        st.save_backtest(run_id, trades, grid, meta)
        tab = setup_summary_table(trades, grid, s)
        if tab.empty:
            print("No trades. Is there price history stored? (see README)")
            return 0
        cols = ["setup", "trades", "win_rate", "win_ci_low", "win_ci_high", "expectancy_r", "profit_factor",
                "max_dd_pct", "oos_expectancy_r", "wf_expectancy_r", "confidence"]
        print(tab[cols].round(2).to_string(index=False))
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
    elif a.cmd == "daily":
        rep = pipe.update()
        print(rep.summary())
        df = run_scan(s, st, pipe.scan_symbols())
        print(f"Scanned {len(df)} stocks.")
        if rep.errors:
            return 2  # non-zero so the scheduler log shows something needs a look
    elif a.cmd == "health":
        print(st.q("""SELECT dataset, source, status, count(*) AS n, max(day) AS latest_day
                      FROM fetch_log GROUP BY ALL ORDER BY dataset, source, status""").to_string(index=False))
        print("\nLast stored price day:", st.last_price_date())
    return 0


if __name__ == "__main__":
    sys.exit(main())
