"""DuckDB storage. One file, raw prices kept as published, adjustments applied on read."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from .adjust import apply_adjustments

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, prev_close DOUBLE,
    volume DOUBLE, turnover DOUBLE, trades DOUBLE, source VARCHAR,
    PRIMARY KEY (date, symbol, series)
);
CREATE TABLE IF NOT EXISTS delivery (
    date DATE, symbol VARCHAR, series VARCHAR, deliv_qty DOUBLE, deliv_pct DOUBLE,
    source VARCHAR, PRIMARY KEY (date, symbol, series)
);
CREATE TABLE IF NOT EXISTS index_prices (
    date DATE, index_name VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    source VARCHAR, PRIMARY KEY (date, index_name)
);
CREATE TABLE IF NOT EXISTS corp_actions (
    symbol VARCHAR, ex_date DATE, factor DOUBLE, source VARCHAR,
    PRIMARY KEY (symbol, ex_date)
);
CREATE TABLE IF NOT EXISTS universe (
    symbol VARCHAR PRIMARY KEY, company VARCHAR, industry VARCHAR, series VARCHAR,
    isin VARCHAR, index_name VARCHAR, as_of DATE
);
CREATE TABLE IF NOT EXISTS fno (symbol VARCHAR PRIMARY KEY, as_of DATE);
CREATE TABLE IF NOT EXISTS fetch_log (
    ts TIMESTAMP, day DATE, dataset VARCHAR, source VARCHAR, status VARCHAR,
    rows INTEGER, message VARCHAR
);
CREATE TABLE IF NOT EXISTS scan_results (
    run_date DATE, symbol VARCHAR, data JSON, PRIMARY KEY (run_date, symbol)
);
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR);
CREATE TABLE IF NOT EXISTS bt_runs (run_id VARCHAR PRIMARY KEY, created TIMESTAMP, meta JSON);
CREATE TABLE IF NOT EXISTS fund_info (
    symbol VARCHAR PRIMARY KEY, source VARCHAR, basis VARCHAR, fetched DATE, sector VARCHAR,
    industry VARCHAR, is_financial BOOLEAN, ratios JSON, notes JSON
);
CREATE TABLE IF NOT EXISTS fund_statements (
    symbol VARCHAR, statement VARCHAR, item VARCHAR, period DATE, value DOUBLE, source VARCHAR,
    PRIMARY KEY (symbol, statement, item, period)
);
CREATE TABLE IF NOT EXISTS shareholding (
    symbol VARCHAR, quarter_end DATE, category VARCHAR, pct DOUBLE, source VARCHAR,
    PRIMARY KEY (symbol, quarter_end, category, source)
);
CREATE TABLE IF NOT EXISTS holders (
    symbol VARCHAR, quarter_end DATE, name VARCHAR, category VARCHAR, pct DOUBLE, shares DOUBLE,
    source VARCHAR, PRIMARY KEY (symbol, quarter_end, name)
);
CREATE TABLE IF NOT EXISTS pledges (
    symbol VARCHAR, as_of DATE, promoter_pct DOUBLE, pledge_pct DOUBLE, source VARCHAR,
    PRIMARY KEY (symbol, as_of)
);
CREATE TABLE IF NOT EXISTS deals (
    date DATE, symbol VARCHAR, kind VARCHAR, client VARCHAR, side VARCHAR, qty DOUBLE, price DOUBLE,
    source VARCHAR, PRIMARY KEY (date, symbol, kind, client, side, qty, price)
);
CREATE TABLE IF NOT EXISTS flows (
    date DATE, category VARCHAR, buy_cr DOUBLE, sell_cr DOUBLE, net_cr DOUBLE, source VARCHAR,
    PRIMARY KEY (date, category)
);
"""

SOURCE_RANK = {"nse_udiff": 0, "nse_legacy": 1, "nse_full_bhav": 2, "nse_index_close": 0,
               "nse_mto": 3, "nse_prevclose": 0, "yahoo": 9, "demo": 5,
               "gap_inferred": 10}


class Store:
    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            self.con.execute(SCHEMA)

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------ writes
    def _upsert(self, table: str, df: pd.DataFrame, keep_existing: bool = False) -> int:
        if df is None or df.empty:
            return 0
        verb = "INSERT OR IGNORE" if keep_existing else "INSERT OR REPLACE"
        cols = ", ".join(df.columns)
        self.con.register("_incoming", df)
        try:
            self.con.execute(f"{verb} INTO {table} ({cols}) SELECT {cols} FROM _incoming")
        finally:
            self.con.unregister("_incoming")
        return len(df)

    def upsert_prices(self, df: pd.DataFrame, keep_existing: bool = False) -> int:
        df = df.drop_duplicates(["date", "symbol", "series"], keep="first")
        return self._upsert("prices", df, keep_existing)

    def upsert_delivery(self, df: pd.DataFrame, keep_existing: bool = False) -> int:
        df = df.drop_duplicates(["date", "symbol", "series"], keep="first")
        return self._upsert("delivery", df, keep_existing)

    def upsert_index(self, df: pd.DataFrame, keep_existing: bool = False) -> int:
        df = df.drop_duplicates(["date", "index_name"], keep="first")
        return self._upsert("index_prices", df, keep_existing)

    def upsert_corp_actions(self, df: pd.DataFrame) -> int:
        """Add actions. When two sources report the same event (within 3 days, similar
        factor) keep only the better-ranked source, so it is never applied twice."""
        if df is None or df.empty:
            return 0
        allx = pd.concat([self.corp_actions(df["symbol"].unique().tolist()), df], ignore_index=True)
        allx["ex_date"] = pd.to_datetime(allx["ex_date"]).dt.date
        allx["rank"] = allx["source"].map(SOURCE_RANK).fillna(5)
        kept: list[dict] = []
        for _, g in allx.sort_values(["rank", "ex_date"]).groupby("symbol", sort=False):
            mine: list[dict] = []
            for row in g.to_dict("records"):
                if not any(abs((row["ex_date"] - k["ex_date"]).days) <= 3
                           and abs(row["factor"] / k["factor"] - 1) < 0.1 for k in mine):
                    mine.append(row)
            kept += mine
        out = pd.DataFrame(kept)[["symbol", "ex_date", "factor", "source"]]
        syms = out["symbol"].unique().tolist()
        self.con.execute(f"DELETE FROM corp_actions WHERE symbol IN ({','.join('?' * len(syms))})", syms)
        return self._upsert("corp_actions", out)

    def replace_universe(self, df: pd.DataFrame, index_name: str) -> int:
        df = df.copy()
        df["index_name"] = index_name
        df["as_of"] = date.today()
        self.con.execute("DELETE FROM universe WHERE index_name = ?", [index_name])
        return self._upsert("universe", df[["symbol", "company", "industry", "series", "isin",
                                            "index_name", "as_of"]])

    def replace_fno(self, symbols: list[str]) -> int:
        self.con.execute("DELETE FROM fno")
        return self._upsert("fno", pd.DataFrame({"symbol": symbols, "as_of": date.today()}))

    def log_fetch(self, day: date | None, dataset: str, source: str, status: str,
                  rows: int = 0, message: str = "") -> None:
        self.con.execute("INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [datetime.now(), day, dataset, source, status, rows, message[:500]])

    def save_scan(self, run_date: date, results: pd.DataFrame) -> int:
        self.con.execute("DELETE FROM scan_results WHERE run_date = ?", [run_date])
        rows = pd.DataFrame({
            "run_date": run_date,
            "symbol": results["symbol"],
            "data": [json.dumps(r, default=str) for r in results.to_dict("records")],
        })
        return self._upsert("scan_results", rows)

    # ------------------------------------------------------------ fundamentals / smart money
    def save_fundamentals(self, f) -> None:
        """Replace everything stored for one company with a freshly parsed Fundamentals."""
        sym = f.symbol
        self.con.execute("DELETE FROM fund_info WHERE symbol = ?", [sym])
        self.con.execute("INSERT INTO fund_info VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [sym, f.source, f.basis, f.fetched, f.sector, f.industry, bool(f.is_financial),
                          json.dumps(f.ratios, default=str), json.dumps(f.notes)])
        self.con.execute("DELETE FROM fund_statements WHERE symbol = ?", [sym])
        if not f.statements.empty:
            st = f.statements.dropna(subset=["value", "period"]).assign(symbol=sym, source=f.source)
            st["period"] = pd.to_datetime(st["period"]).dt.date
            self._upsert("fund_statements", st.drop_duplicates(["statement", "item", "period"], keep="last")[
                ["symbol", "statement", "item", "period", "value", "source"]])
        if not f.shareholding.empty:
            self.upsert_shareholding(f.shareholding.assign(symbol=sym, source=f.source))
        if not f.holders.empty:
            self.upsert_holders(f.holders.assign(symbol=sym, source=f.source))

    def load_fundamentals(self, symbol: str):
        from ..fundamentals.model import Fundamentals
        r = self.con.execute("SELECT * FROM fund_info WHERE symbol = ?", [symbol]).fetchone()
        if r is None:
            return None
        cols = [d[0] for d in self.con.description]
        info = dict(zip(cols, r))
        st = self.q("SELECT statement, item, period, value FROM fund_statements WHERE symbol = ?", [symbol])
        st["period"] = pd.to_datetime(st["period"])
        sh = self.shareholding_all([symbol])[["quarter_end", "category", "pct", "source"]]
        ho = self.q("SELECT quarter_end, name, category, pct, shares FROM holders WHERE symbol = ?", [symbol])
        ratios = json.loads(info["ratios"]) if info["ratios"] else {}
        pl = self.q("SELECT pledge_pct FROM pledges WHERE symbol = ? ORDER BY as_of DESC LIMIT 1", [symbol])
        if len(pl) and ratios.get("pledge_pct") is None:
            ratios["pledge_pct"] = float(pl["pledge_pct"].iloc[0])
        return Fundamentals(symbol=symbol, source=info["source"], basis=info["basis"], fetched=info["fetched"],
                            statements=st, ratios=ratios, sector=info["sector"], industry=info["industry"],
                            is_financial=bool(info["is_financial"]), shareholding=sh, holders=ho,
                            notes=json.loads(info["notes"]) if info["notes"] else [])

    def fundamentals_fetched(self) -> dict[str, date]:
        df = self.q("SELECT symbol, fetched FROM fund_info")
        return {r.symbol: pd.Timestamp(r.fetched).date() for r in df.itertuples()}

    def upsert_shareholding(self, df: pd.DataFrame) -> int:
        df = df.copy()
        df["quarter_end"] = pd.to_datetime(df["quarter_end"]).dt.date
        return self._upsert("shareholding", df.drop_duplicates(
            ["symbol", "quarter_end", "category", "source"], keep="last")[
            ["symbol", "quarter_end", "category", "pct", "source"]])

    def upsert_holders(self, df: pd.DataFrame) -> int:
        df = df.copy()
        df["quarter_end"] = pd.to_datetime(df["quarter_end"]).dt.date
        if "shares" not in df:
            df["shares"] = None
        return self._upsert("holders", df.drop_duplicates(["symbol", "quarter_end", "name"], keep="last")[
            ["symbol", "quarter_end", "name", "category", "pct", "shares", "source"]])

    def upsert_pledges(self, df: pd.DataFrame) -> int:
        df = df.copy()
        df["as_of"] = pd.to_datetime(df["as_of"]).dt.date
        return self._upsert("pledges", df.drop_duplicates(["symbol", "as_of"], keep="last")[
            ["symbol", "as_of", "promoter_pct", "pledge_pct", "source"]])

    def upsert_deals(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return self._upsert("deals", df.drop_duplicates(
            ["date", "symbol", "kind", "client", "side", "qty", "price"])[
            ["date", "symbol", "kind", "client", "side", "qty", "price", "source"]])

    def upsert_flows(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return self._upsert("flows", df.drop_duplicates(["date", "category"], keep="last")[
            ["date", "category", "buy_cr", "sell_cr", "net_cr", "source"]])

    # When two sources give the same holder category (Screener, NSE's filing list, the
    # XBRL filing itself) their numbers differ slightly, so mixing them would create fake
    # quarter-on-quarter changes. One source is used per stock and category: the one with
    # the most quarters (ties: the official XBRL filing first).
    SHP_SOURCE_RANK = {"nse_xbrl": 0, "nse_master": 1, "screener": 2}

    def shareholding_all(self, symbols: list[str] | None = None) -> pd.DataFrame:
        sql, params = "SELECT * FROM shareholding", []
        if symbols:
            sql += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
            params = symbols
        df = self.q(sql, params)
        if df.empty:
            return df
        n = df.groupby(["symbol", "category", "source"]).size().rename("n").reset_index()
        n["rank"] = n["source"].map(self.SHP_SOURCE_RANK).fillna(9)
        best = n.sort_values(["n", "rank"], ascending=[False, True]).drop_duplicates(["symbol", "category"])
        df = df.merge(best[["symbol", "category", "source"]], on=["symbol", "category", "source"])
        return df.sort_values(["symbol", "quarter_end", "category"]).reset_index(drop=True)

    def holders_all(self) -> pd.DataFrame:
        return self.q("SELECT * FROM holders ORDER BY symbol, quarter_end")

    def deals_all(self, since: date | None = None) -> pd.DataFrame:
        if since:
            return self.q("SELECT * FROM deals WHERE date >= ? ORDER BY date DESC", [since])
        return self.q("SELECT * FROM deals ORDER BY date DESC")

    def flows_all(self) -> pd.DataFrame:
        return self.q("SELECT * FROM flows ORDER BY date")

    # ------------------------------------------------------------ backtests
    def fund_symbols(self) -> set[str]:
        """ETFs and other fund units (ISIN starts with INF), e.g. GOLDBEES, LIQUIDETF,
        NIFTYBEES. They are not company shares, so stock setups don't apply to them."""
        return set(self.q("SELECT DISTINCT symbol FROM prices WHERE isin LIKE 'INF%'")["symbol"])

    def save_backtest(self, run_id: str, trades: pd.DataFrame, grid: pd.DataFrame, meta: dict,
                      keep_runs: int = 5) -> None:
        """Store one backtest run. Trade and grid tables are created from the frames'
        columns on first use; older runs beyond `keep_runs` are deleted."""
        self.con.execute("INSERT OR REPLACE INTO bt_runs VALUES (?, ?, ?)",
                         [run_id, pd.Timestamp.now().to_pydatetime(), json.dumps(meta, default=str)])
        for table, df in (("bt_trades", trades), ("bt_grid", grid)):
            if df is None or df.empty:
                continue
            df = df.assign(run_id=run_id)
            self.con.register("_bt", df)
            try:
                exists = self.con.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]).fetchone()[0]
                if not exists:
                    self.con.execute(f"CREATE TABLE {table} AS SELECT * FROM _bt")
                else:
                    # columns added in newer versions: add them so old databases keep working
                    have = set(self.q(f"SELECT * FROM {table} LIMIT 0").columns)
                    types = {r[0]: r[1] for r in self.con.execute("DESCRIBE _bt").fetchall()}
                    for col in [c for c in df.columns if c not in have]:
                        self.con.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" {types[col]}')
                    self.con.execute(f"DELETE FROM {table} WHERE run_id = ?", [run_id])
                    self.con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _bt")
            finally:
                self.con.unregister("_bt")
        old = self.q("SELECT run_id FROM bt_runs ORDER BY created DESC")["run_id"].tolist()[keep_runs:]
        for rid in old:
            self.con.execute("DELETE FROM bt_runs WHERE run_id = ?", [rid])
            for table in ("bt_trades", "bt_grid"):
                if self._has_table(table):
                    self.con.execute(f"DELETE FROM {table} WHERE run_id = ?", [rid])

    def _has_table(self, name: str) -> bool:
        return bool(self.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                                     [name]).fetchone()[0])

    def backtest_runs(self) -> pd.DataFrame:
        if not self._has_table("bt_runs"):
            return pd.DataFrame(columns=["run_id", "created", "meta"])
        return self.q("SELECT * FROM bt_runs ORDER BY created DESC")

    def load_backtest(self, run_id: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
        r = self.con.execute("SELECT meta FROM bt_runs WHERE run_id = ?", [run_id]).fetchone()
        meta = json.loads(r[0]) if r else {}
        trades = self.q("SELECT * FROM bt_trades WHERE run_id = ?", [run_id]) if self._has_table("bt_trades") \
            else pd.DataFrame()
        grid = self.q("SELECT * FROM bt_grid WHERE run_id = ?", [run_id]) if self._has_table("bt_grid") \
            else pd.DataFrame()
        return trades, grid, meta

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])

    # ------------------------------------------------------------ reads
    def q(self, sql: str, params: list | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return r[0] if r else default

    def stored_days(self, dataset: str = "prices", exclude_source: str | None = None) -> set[date]:
        table = {"prices": "prices", "delivery": "delivery", "index": "index_prices"}[dataset]
        sql, params = f"SELECT DISTINCT date FROM {table}", []
        if exclude_source:
            sql += " WHERE source <> ?"
            params.append(exclude_source)
        return {pd.Timestamp(d).date() for d in self.q(sql, params)["date"]}

    def last_price_date(self) -> date | None:
        r = self.con.execute("SELECT max(date) FROM prices").fetchone()[0]
        return r

    def universe(self, index_name: str | None = None) -> pd.DataFrame:
        if index_name:
            return self.q("SELECT * FROM universe WHERE index_name = ? ORDER BY symbol", [index_name])
        return self.q("SELECT * FROM universe ORDER BY symbol")

    def all_symbols(self, series: list[str]) -> list[str]:
        last = self.last_price_date()
        if last is None:
            return []
        ph = ",".join("?" * len(series))
        return self.q(f"SELECT DISTINCT symbol FROM prices WHERE date = ? AND series IN ({ph})",
                      [last, *series])["symbol"].tolist()

    def fno_symbols(self) -> set[str]:
        return set(self.q("SELECT symbol FROM fno")["symbol"])

    def corp_actions(self, symbols: list[str] | None = None) -> pd.DataFrame:
        if symbols:
            ph = ",".join("?" * len(symbols))
            return self.q(f"SELECT * FROM corp_actions WHERE symbol IN ({ph})", symbols)
        return self.q("SELECT * FROM corp_actions")

    def raw_prices(self, symbols: list[str] | None = None, start: date | None = None,
                   series: list[str] | None = None) -> pd.DataFrame:
        where, params = [], []
        if symbols:
            where.append(f"symbol IN ({','.join('?' * len(symbols))})")
            params += symbols
        if start:
            where.append("date >= ?")
            params.append(start)
        if series:
            where.append(f"series IN ({','.join('?' * len(series))})")
            params += series
        sql = "SELECT * FROM prices" + (" WHERE " + " AND ".join(where) if where else "")
        df = self.q(sql + " ORDER BY symbol, date", params)
        # one row per symbol/day: prefer EQ over BE etc. when a stock moved series
        if not df.empty and series and len(series) > 1:
            rank = {s: i for i, s in enumerate(series)}
            df["_r"] = df["series"].map(rank)
            df = df.sort_values(["symbol", "date", "_r"]).drop_duplicates(["symbol", "date"]).drop(columns="_r")
        return df.reset_index(drop=True)

    def adjusted_prices(self, symbols: list[str] | None = None, start: date | None = None,
                        series: list[str] | None = None) -> pd.DataFrame:
        """Split/bonus-adjusted OHLCV joined with delivery %. Raw stays untouched in the DB."""
        raw = self.raw_prices(symbols, None, series or ["EQ", "BE"])
        if raw.empty:
            return raw
        adj = apply_adjustments(raw, self.corp_actions(symbols))
        dl = self.delivery(symbols)
        if not dl.empty:
            adj = adj.merge(dl[["date", "symbol", "deliv_pct", "deliv_qty"]], on=["date", "symbol"], how="left")
        else:
            adj["deliv_pct"] = float("nan")
            adj["deliv_qty"] = float("nan")
        if start:
            adj = adj[adj["date"] >= pd.Timestamp(start)]
        return adj.reset_index(drop=True)

    def delivery(self, symbols: list[str] | None = None) -> pd.DataFrame:
        sql = "SELECT date, symbol, max(deliv_pct) AS deliv_pct, max(deliv_qty) AS deliv_qty FROM delivery"
        params: list = []
        if symbols:
            sql += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
            params = symbols
        df = self.q(sql + " GROUP BY date, symbol", params)
        df["date"] = pd.to_datetime(df["date"])
        return df

    def index_prices(self, index_name: str) -> pd.DataFrame:
        df = self.q("SELECT * FROM index_prices WHERE index_name = ? ORDER BY date", [index_name])
        df["date"] = pd.to_datetime(df["date"])
        return df

    def latest_scan(self) -> tuple[date | None, pd.DataFrame]:
        r = self.con.execute("SELECT max(run_date) FROM scan_results").fetchone()[0]
        if r is None:
            return None, pd.DataFrame()
        rows = self.q("SELECT data FROM scan_results WHERE run_date = ?", [r])
        return r, pd.DataFrame([json.loads(x) for x in rows["data"]])

    def fetch_log(self, limit: int = 500) -> pd.DataFrame:
        return self.q("SELECT * FROM fetch_log ORDER BY ts DESC LIMIT ?", [limit])
