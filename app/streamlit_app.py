"""NSE Research dashboard (Phase 1: technical scanner).

Run:  streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nse_research.data.store import Store  # noqa: E402
from nse_research.indicators.levels import find_levels  # noqa: E402
from nse_research.scanner.engine import analyse_symbol  # noqa: E402
from nse_research.settings import load_settings  # noqa: E402

DISCLAIMER = (
    "**Decision-support tool only. This is not investment advice and the author is not a "
    "SEBI-registered investment adviser or research analyst.** Signals describe what the data "
    "shows; they do not predict outcomes. Data comes from free, unofficial sources and can be "
    "late, wrong or missing. Check prices with your broker before acting."
)

st.set_page_config(page_title="NSE Research", page_icon="📈", layout="wide")
S = load_settings()
DB = Path(os.environ.get("NSE_RESEARCH_DB") or S.path("db_path"))


# The DB is opened briefly for each read and closed again, so the daily job can
# write to it while the dashboard is open (DuckDB allows one writer at a time).
def _with_store(fn):
    if not DB.exists():
        return None
    try:
        st_ = Store(DB, read_only=True)
    except Exception as e:  # noqa: BLE001 - usually the daily job holds the write lock
        st.error(f"Database is busy (the daily update may be running). Try Reload in a minute. ({e})")
        st.stop()
    try:
        return fn(st_)
    finally:
        st_.close()


@st.cache_data(ttl=300)
def load_overview():
    def f(s: Store):
        run_date, scan = s.latest_scan()
        return {
            "run_date": run_date, "scan": scan, "demo": s.get_meta("demo") == "1",
            "last_update": s.get_meta("last_update"), "last_price": s.last_price_date(),
            "skipped": s.get_meta("last_scan_skipped"),
        }
    return _with_store(f)


@st.cache_data(ttl=300)
def load_symbol(symbol: str):
    def f(s: Store):
        px = s.adjusted_prices([symbol], series=S["universe"]["series"])
        b = s.index_prices(S["benchmark"]["index_name"])
        ca = s.corp_actions([symbol])
        return px, b, ca
    return _with_store(f)


@st.cache_data(ttl=120)
def load_health():
    def f(s: Store):
        summary = s.q("""SELECT dataset, source, status, count(*) AS attempts, max(day) AS latest_day
                         FROM fetch_log GROUP BY ALL ORDER BY dataset, source, status""")
        problems = s.q("""SELECT ts, day, dataset, source, status, message FROM fetch_log
                          WHERE status IN ('error', 'empty') ORDER BY ts DESC LIMIT 200""")
        coverage = s.q("""SELECT source, count(DISTINCT date) AS days, min(date) AS first, max(date) AS last,
                                 count(*) AS rows FROM prices GROUP BY source ORDER BY source""")
        dcov = s.q("""SELECT source, count(DISTINCT date) AS days, min(date) AS first, max(date) AS last
                      FROM delivery GROUP BY source""")
        icov = s.q("""SELECT index_name, source, count(*) AS days, min(date) AS first, max(date) AS last
                      FROM index_prices WHERE index_name = ? GROUP BY ALL""", [S["benchmark"]["index_name"]])
        ca = s.q("SELECT * FROM corp_actions ORDER BY ex_date DESC LIMIT 200")
        uni = s.q("SELECT index_name, count(*) AS symbols, max(as_of) AS as_of FROM universe GROUP BY ALL")
        fno = s.q("SELECT count(*) AS symbols, max(as_of) AS as_of FROM fno")
        return dict(summary=summary, problems=problems, coverage=coverage, dcov=dcov, icov=icov,
                    ca=ca, uni=uni, fno=fno)
    return _with_store(f)


def header(ov):
    st.warning(DISCLAIMER, icon="⚠️")
    if ov and ov["demo"]:
        st.error("DEMO DATA: every number on this screen is randomly generated. Run the real "
                 "data commands in the README to use NSE data.", icon="🧪")


def fmt_list(x) -> str:
    return ", ".join(x) if isinstance(x, list) else ""


# ------------------------------------------------------------------ pages
def page_scanner(ov):
    st.header("Technical scanner")
    scan: pd.DataFrame = ov["scan"]
    if scan.empty:
        st.info("No scan yet. Run `python -m nse_research.cli daily` (see README).")
        return
    st.caption(f"Scan of {len(scan)} stocks using data up to **{ov['run_date']}**. "
               f"Last data update: {ov['last_update']}. Skipped: {ov['skipped']}. "
               "Counts are just the number of signals that fired, not a score. Open a stock to see every reason.")

    all_sigs = sorted({sig["label"] for row in scan["signals"] for sig in row})
    c1, c2, c3, c4 = st.columns([1.2, 2, 2, 1])
    side = c1.radio("Show", ["Bullish", "Bearish", "All"], horizontal=True)
    need = c2.multiselect("Must have all of these signals", all_sigs)
    inds = c3.multiselect("Industry", sorted(scan["industry"].dropna().unique()))
    fno_only = c4.checkbox("F&O only", value=(side == "Bearish"),
                           help="Multi-day shorts need F&O. Bearish setups on non-F&O stocks can't be shorted for a swing hold.")
    min_n = st.slider("Minimum number of signals on that side", 0, 6, 1 if side != "All" else 0)

    df = scan.copy()
    if side == "Bullish":
        df = df[df["n_bullish"] >= min_n].sort_values(["n_bullish", "n_bearish"], ascending=[False, True])
    elif side == "Bearish":
        df = df[df["n_bearish"] >= min_n].sort_values(["n_bearish", "n_bullish"], ascending=[False, True])
    if need:
        df = df[df["signals"].apply(lambda s: set(need) <= {x["label"] for x in s})]
    if inds:
        df = df[df["industry"].isin(inds)]
    if fno_only:
        df = df[df["fno"]]

    rs_cols = [f"rs{n}" for n in S["scanner"]["rs_lookbacks"]]
    view = pd.DataFrame({
        "Symbol": df["symbol"], "Company": df["company"], "Industry": df["industry"],
        "Close": df["close"], "Chg %": df["change_pct"], "RSI": df["rsi"],
        "Vol × avg": df["vol_ratio"], "Deliv %": df["deliv_pct"],
        **{f"RS {c[2:]}d": df[c] for c in rs_cols if c in df},
        "ATR %": df["atr_pct"], "# Bull": df["n_bullish"], "# Bear": df["n_bearish"],
        "Bullish signals": df["bullish"].apply(fmt_list), "Bearish signals": df["bearish"].apply(fmt_list),
        "Info": df["info"].apply(fmt_list), "F&O": df["fno"], "Stale": df.get("stale", False),
    })
    st.write(f"**{len(view)}** stocks match.")
    st.dataframe(
        view, hide_index=True, use_container_width=True, height=520,
        column_config={
            "Close": st.column_config.NumberColumn(format="%.2f"),
            "Chg %": st.column_config.NumberColumn(format="%.2f"),
            "RSI": st.column_config.NumberColumn(format="%.1f"),
            "Vol × avg": st.column_config.NumberColumn(format="%.2f"),
            "Deliv %": st.column_config.NumberColumn(format="%.1f"),
            "ATR %": st.column_config.NumberColumn(format="%.2f"),
            **{f"RS {c[2:]}d": st.column_config.NumberColumn(format="%+.1f",
                                                            help="Stock return minus benchmark return, % points")
               for c in rs_cols},
            "Stale": st.column_config.CheckboxColumn(help="Latest bar is older than the newest market day stored"),
        },
    )
    st.download_button("Download as CSV", view.to_csv(index=False), "scan.csv", "text/csv")

    st.subheader("Why each stock is listed")
    pick = st.selectbox("Stock", df["symbol"].tolist(), index=None, placeholder="Choose a stock to see every reason")
    if pick:
        show_reasons(df[df["symbol"] == pick].iloc[0])
        if st.button(f"Open {pick} chart"):
            st.session_state["symbol"] = pick
            st.session_state["nav_to"] = "Stock chart"
            st.rerun()


def show_reasons(row):
    sigs = row["signals"]
    for side, icon in (("bullish", "🟢"), ("bearish", "🔴"), ("info", "⚪")):
        items = [s for s in sigs if s["side"] == side]
        if items:
            st.markdown(f"**{icon} {side.title()}**")
            for s in items:
                st.markdown(f"- **{s['label']}**: {s['reason']}")
    if not sigs:
        st.write("No rule fired for this stock today.")
    if row.get("n_bearish", 0) and not row.get("short_possible", False):
        st.caption("Bearish signals here can't be used as a multi-day short: the stock is not in F&O "
                   "(cash-market shorts must be squared off the same day). Treat them as exit/avoid.")


def page_chart(ov):
    st.header("Stock chart")
    scan = ov["scan"]
    syms = sorted(scan["symbol"]) if not scan.empty else []
    default = st.session_state.get("symbol")
    sym = st.selectbox("Stock", syms, index=syms.index(default) if default in syms else None,
                       placeholder="Choose a stock")
    if not sym:
        return
    px, bench, ca = load_symbol(sym)
    if px.empty:
        st.info("No prices stored for this stock.")
        return
    cfg = S["scanner"]
    bser = bench.set_index("date")["close"] if not bench.empty else None
    d, summ = analyse_symbol(px, cfg, bser)
    years = st.select_slider("Window", options=[0.5, 1, 2, 3, 5, 10], value=1, format_func=lambda y: f"{y} y")
    d = d[d["date"] >= d["date"].max() - pd.Timedelta(days=int(365 * years))]

    meta = scan[scan["symbol"] == sym].iloc[0] if sym in set(scan["symbol"]) else None
    if meta is not None:
        st.caption(f"{meta['company'] or ''} · {meta['industry'] or ''} · "
                   f"{'F&O stock' if meta['fno'] else 'Not in F&O'} · data source of last bar: {summ['source']}")
    m = st.columns(6)
    m[0].metric("Close", f"{summ['close']:,.2f}", f"{summ['change_pct']:+.2f}%" if summ["change_pct"] is not None else None)
    m[1].metric("RSI", f"{summ['rsi']:.1f}" if summ["rsi"] else "n/a")
    m[2].metric("ATR", f"{summ['atr']:,.2f}" if summ["atr"] else "n/a",
                f"{summ['atr_pct']:.2f}% of price" if summ["atr_pct"] else None, delta_color="off")
    m[3].metric("Volume × 20d avg", f"{summ['vol_ratio']:.2f}" if summ["vol_ratio"] else "n/a")
    m[4].metric("Delivery %", f"{summ['deliv_pct']:.1f}" if summ["deliv_pct"] else "n/a",
                f"avg {summ['deliv_avg']:.1f}" if summ["deliv_avg"] else None, delta_color="off")
    rsk = f"rs{cfg['rs_lookbacks'][0]}"
    m[5].metric(f"RS vs {S['benchmark']['index_name']} ({cfg['rs_lookbacks'][0]}d)",
                f"{summ[rsk]:+.1f} pts" if summ.get(rsk) is not None else "n/a")

    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.02,
                        row_heights=[0.55, 0.15, 0.15, 0.15])
    fig.add_trace(go.Candlestick(x=d["date"], open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                                 name="Price", showlegend=False), 1, 1)
    for n, color in zip(cfg["dma_periods"], ["#1f77b4", "#ff7f0e", "#9467bd"]):
        fig.add_trace(go.Scatter(x=d["date"], y=d[f"dma{n}"], name=f"{n} DMA", line=dict(width=1.2, color=color)), 1, 1)
    levels = find_levels(d, cfg["pivot_window"], cfg["sr_lookback_days"], cfg["sr_cluster_atr"], summ["atr"])
    close = summ["close"]
    strong = [lv for lv in levels if lv.touches >= 2] or levels  # single-touch pivots are noise
    for lv in sorted(strong, key=lambda lv: abs(lv.price - close))[:6]:
        fig.add_hline(y=lv.price, line_dash="dot", line_width=1,
                      line_color="green" if lv.price < close else "red",
                      annotation_text=f"{'S' if lv.price < close else 'R'} {lv.price:,.1f} ×{lv.touches}",
                      annotation_position="right", row=1, col=1)
    fig.add_trace(go.Bar(x=d["date"], y=d["volume"], name="Volume", marker_color="#999"), 2, 1)
    fig.add_trace(go.Scatter(x=d["date"], y=d["vol_avg"], name="20d avg vol", line=dict(width=1, color="black")), 2, 1)
    fig.add_trace(go.Scatter(x=d["date"], y=d["rsi"], name="RSI", line=dict(width=1.2, color="#8c564b")), 3, 1)
    for y in (cfg["rsi_oversold"], 50, cfg["rsi_overbought"]):
        fig.add_hline(y=y, line_dash="dot", line_width=0.8, line_color="#aaa", row=3, col=1)
    fig.add_trace(go.Bar(x=d["date"], y=d["macd_hist"], name="MACD hist", marker_color="#bbb"), 4, 1)
    fig.add_trace(go.Scatter(x=d["date"], y=d["macd"], name="MACD", line=dict(width=1.2, color="#1f77b4")), 4, 1)
    fig.add_trace(go.Scatter(x=d["date"], y=d["macd_signal"], name="Signal", line=dict(width=1.2, color="#d62728")), 4, 1)
    fig.update_layout(height=820, xaxis_rangeslider_visible=False, margin=dict(l=10, r=60, t=10, b=10),
                      legend=dict(orientation="h", y=1.02))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Signals today and why")
    show_reasons(pd.Series({**summ, "short_possible": meta["short_possible"] if meta is not None else False}))
    if "deliv_pct" in d and d["deliv_pct"].notna().any():
        with st.expander("Delivery % history"):
            st.line_chart(d.set_index("date")[["deliv_pct", "deliv_avg"]])
    if ca is not None and not ca.empty:
        with st.expander("Corporate actions applied to this chart (split/bonus adjustment)"):
            st.dataframe(ca, hide_index=True)


def page_health(_ov):
    st.header("Data health")
    h = load_health()
    if h is None:
        st.info("No database yet.")
        return
    st.caption("Every download attempt is logged. If a source fails the next one is tried; "
               "anything still missing is retried on the next run.")
    c1, c2 = st.columns(2)
    c1.subheader("Price coverage by source")
    c1.dataframe(h["coverage"], hide_index=True)
    c2.subheader("Delivery coverage")
    c2.dataframe(h["dcov"], hide_index=True)
    c1.subheader("Benchmark coverage")
    c1.dataframe(h["icov"], hide_index=True)
    c2.subheader("Lists")
    c2.dataframe(h["uni"], hide_index=True)
    c2.dataframe(h["fno"].assign(list="F&O"), hide_index=True)
    st.subheader("Fetch attempts")
    st.dataframe(h["summary"], hide_index=True, use_container_width=True)
    st.subheader("Recent problems")
    if h["problems"].empty:
        st.success("No errors logged.")
    else:
        st.dataframe(h["problems"], hide_index=True, use_container_width=True)
    st.subheader("Detected corporate actions (latest 200)")
    st.dataframe(h["ca"], hide_index=True, use_container_width=True)


def page_settings(_ov):
    st.header("Settings in use")
    st.caption("Edit config/settings.yaml (or create config/settings.local.yaml) and restart the app.")
    p = S["profile"]
    c = st.columns(4)
    c[0].metric("Capital", f"₹{p['capital']:,.0f}")
    c[1].metric("Risk per trade", f"{p['risk_per_trade_pct']}% = ₹{S.risk_amount:,.0f}")
    c[2].metric("Max in one stock", f"{p['max_position_pct']}% = ₹{S.max_position_value:,.0f}")
    c[3].metric("Shorts", "F&O stocks only" if p["short_requires_fno"] else "allowed")
    st.json(dict(S), expanded=False)


PAGES = {"Scanner": page_scanner, "Stock chart": page_chart, "Data health": page_health, "Settings": page_settings}

if "nav_to" in st.session_state:  # must be set before the radio widget is drawn
    st.session_state["page"] = st.session_state.pop("nav_to")

ov = load_overview()
with st.sidebar:
    st.title("NSE Research")
    page = st.radio("Page", list(PAGES), key="page")
    if ov:
        st.caption(f"Database: `{DB.name}`\n\nLatest price day: {ov['last_price']}")
    if st.button("Reload data"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Phase 1 · technical scanner")

header(ov)
if ov is None:
    st.info(f"No database at `{DB}`. Follow the README: `python -m nse_research.cli init`, then "
            "`backfill-fast`, then `daily`. Or try `python -m nse_research.cli demo` first.")
else:
    PAGES[page](ov)
