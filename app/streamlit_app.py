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

from nse_research.backtest.setups import SETUP_BY_ID  # noqa: E402
from nse_research.backtest.stats import (portfolio_curve, probability_sentence, setup_summary_table,  # noqa: E402
                                         summarize, walk_forward, warnings_for, wilson, yearly, oos_split)
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
        view, hide_index=True, width="stretch", height=520,
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
    setups = row.get("setups_today") or []
    if isinstance(setups, list) and setups:
        st.markdown("**Backtested setups that fired today**")
        tab = load_bt_summary()
        for sid in setups:
            su = SETUP_BY_ID.get(sid)
            name = su.name if su else sid
            if su and su.side == "short" and not row.get("short_possible", False):
                st.markdown(f"- **{name}**: short setup, but this stock is not in F&O, so it can't be shorted for a swing hold.")
                continue
            hit = tab[tab["setup"] == sid] if tab is not None and not tab.empty else None
            if hit is None or hit.empty:
                st.markdown(f"- **{name}**: no backtest yet (run `python -m nse_research.cli backtest`).")
            else:
                st.markdown(f"- **{name}**: " + probability_sentence(hit.iloc[0], S["backtest"]["target_r"], S["backtest"]))
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
    st.plotly_chart(fig, width="stretch")

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
    st.dataframe(h["summary"], hide_index=True, width="stretch")
    st.subheader("Recent problems")
    if h["problems"].empty:
        st.success("No errors logged.")
    else:
        st.dataframe(h["problems"], hide_index=True, width="stretch")
    st.subheader("Detected corporate actions (latest 200)")
    st.dataframe(h["ca"], hide_index=True, width="stretch")


@st.cache_data(ttl=600)
def load_bt_runs():
    r = _with_store(lambda s: s.backtest_runs())
    return r if r is not None else pd.DataFrame()


@st.cache_data(ttl=600)
def load_bt(run_id: str):
    return _with_store(lambda s: s.load_backtest(run_id))


@st.cache_data(ttl=600)
def load_bt_summary(run_id: str | None = None):
    runs = load_bt_runs()
    if runs.empty:
        return None
    run_id = run_id or runs["run_id"].iloc[0]
    trades, grid, meta = load_bt(run_id)
    return setup_summary_table(trades, grid, S, meta.get("setups")) if not trades.empty else None


def _pct(x, nd=1):
    return "n/a" if x is None or x != x else f"{x:.{nd}f}%"


def _num(x, nd=2):
    return "n/a" if x is None or x != x else f"{x:.{nd}f}"


def page_backtest(_ov):
    st.header("Backtest")
    runs = load_bt_runs()
    if runs.empty:
        st.info("No backtest yet. Run `python -m nse_research.cli backtest` (a few minutes), then press Reload data.")
        return
    run_id = st.selectbox("Run", runs["run_id"].tolist(), index=0)
    trades, grid, meta = load_bt(run_id)
    tab = load_bt_summary(run_id)
    bt = S["backtest"]
    p = meta.get("params", bt)
    st.caption(
        f"Data {meta.get('data_first')} to {meta.get('data_last')}, {meta.get('symbols_tested')} symbols. "
        f"Entry next day's open; stop {p['stop_atr']}× ATR; target {p['target_r']}R; time exit after "
        f"{p['max_hold_days']} days; slippage {p['slippage_bps']} bps each side; all Indian costs included. "
        f"Longs = cash delivery, shorts = stock futures on F&O stocks only. "
        f"Only stocks with ≥ ₹{p['min_avg_turnover_cr']} cr average daily turnover *on the signal day*.")
    with st.expander("How to read this"):
        st.markdown(
            "- **Win rate** is net of costs, with its 95% range. A wide range means few trades.\n"
            "- **Expectancy** is the average result per trade in R (1R = the amount risked). "
            "+0.2R means you made 0.2× your risk per trade on average.\n"
            "- **Max drawdown** replays the trades as one account with your capital, risk and 10% cap "
            "(at most 10 positions at once). It is the worst fall from a peak.\n"
            "- **Out-of-sample** = the last 30% of the period. **Walk-forward** = each year, stop and target "
            "were chosen from the previous 3 years only, then tested on that year. They cover different "
            "years; **Same years, fixed stop/target** shows the default settings over the walk-forward years, "
            "so you can see whether re-picking stop/target helped or the years were simply better.\n"
            "- Fewer than 30 trades is labelled **LOW** confidence. These are historical frequencies, not "
            "predictions.")
    if tab is None or tab.empty:
        st.warning("This run produced no trades.")
        return
    side = st.radio("Side", ["All", "Long", "Short"], horizontal=True)
    t2 = tab if side == "All" else tab[tab["side"] == side.lower()]
    view = pd.DataFrame({
        "Setup": t2["name"], "Side": t2["side"], "Trades": t2["trades"],
        "Win rate": t2["win_rate"], "95% range": t2.apply(lambda r: f"{r.win_ci_low:.0f}–{r.win_ci_high:.0f}%"
                              if r.trades else "", axis=1),
        "Expectancy (R)": t2["expectancy_r"], "Expectancy %": t2["expectancy_pct"],
        "Avg win %": t2["avg_win_pct"], "Avg loss %": t2["avg_loss_pct"],
        "Profit factor": t2["profit_factor"], "Max DD %": t2["max_dd_pct"],
        f"Hit {p['target_r']}R before stop": t2["target_before_stop_pct"],
        "Out-of-sample (R)": t2["oos_expectancy_r"], "Walk-forward (R)": t2["wf_expectancy_r"],
        "Same years, fixed stop/target (R)": t2["wf_fixed_expectancy_r"],
        "Confidence": t2["confidence"],
    })
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        c: st.column_config.NumberColumn(format="%.2f") for c in
        ["Expectancy (R)", "Expectancy %", "Avg win %", "Avg loss %", "Profit factor", "Out-of-sample (R)",
         "Walk-forward (R)", "Same years, fixed stop/target (R)"]} | {c: st.column_config.NumberColumn(format="%.1f") for c in
                                 ["Win rate", "Max DD %", f"Hit {p['target_r']}R before stop"]})

    st.subheader("Setup detail")
    names = dict(zip(t2["name"], t2["setup"]))
    pick = st.selectbox("Setup", list(names), index=0)
    sid = names[pick]
    row = t2[t2["setup"] == sid].iloc[0]
    su = SETUP_BY_ID.get(sid)
    st.markdown(f"**Rule:** {su.logic if su else sid}")
    st.info(probability_sentence(row, p["target_r"], bt))
    for w in warnings_for(row, bt):
        st.warning(w)
    if not row.trades:
        return
    tr = trades[trades["setup"] == sid]
    m = st.columns(6)
    m[0].metric("Trades", f"{row.trades:,}")
    m[1].metric("Win rate", _pct(row.win_rate), f"range {row.win_ci_low:.0f}–{row.win_ci_high:.0f}%", delta_color="off")
    m[2].metric("Expectancy", f"{row.expectancy_r:+.2f}R", f"{row.expectancy_pct:+.2f}% per trade", delta_color="off")
    m[3].metric("Profit factor", _num(row.profit_factor))
    m[4].metric("Max drawdown", _pct(row.max_dd_pct), "your account settings", delta_color="off")
    m[5].metric("Avg holding", f"{row.avg_bars:.0f} days")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Reached nR before the stop** (target ignored)")
        lv = []
        for r in p.get("r_levels", [1, 2, 3]):
            k = f"hit_{r}r_pct"
            if k in row and row[k] == row[k]:
                lo, hi = row[f"hit_{r}r_ci"]
                lv.append({"Level": f"{r}R", "Reached first": f"{row[k]:.0f}%", "95% range": f"{lo:.0f}–{hi:.0f}%"})
        st.dataframe(pd.DataFrame(lv), hide_index=True)
        st.markdown("**How trades ended**")
        st.dataframe(pd.DataFrame([row.exits]).T.rename(columns={0: "trades"}), width="stretch")
    with c2:
        st.markdown("**In-sample vs out-of-sample**")
        st.dataframe(pd.DataFrame([
            {"Period": "In-sample", "Trades": row.is_trades, "Win rate": _pct(row.is_win_rate),
             "Expectancy": f"{row.is_expectancy_r:+.2f}R" if row.is_expectancy_r == row.is_expectancy_r else "n/a"},
            {"Period": f"Out-of-sample (from {row.oos_from})", "Trades": row.oos_trades,
             "Win rate": _pct(row.oos_win_rate),
             "Expectancy": f"{row.oos_expectancy_r:+.2f}R" if row.oos_expectancy_r == row.oos_expectancy_r else "n/a"},
            {"Period": f"Walk-forward (test years from {row.wf_from})", "Trades": row.wf_trades,
             "Win rate": _pct(row.wf_win_rate),
             "Expectancy": f"{row.wf_expectancy_r:+.2f}R" if row.wf_expectancy_r == row.wf_expectancy_r else "n/a"},
            {"Period": f"Same years, fixed {p['stop_atr']}× ATR / {p['target_r']}R", "Trades": None,
             "Win rate": "",
             "Expectancy": _num(row.wf_fixed_expectancy_r) + "R"},
        ]), hide_index=True)
        st.caption("Out-of-sample and walk-forward cover different years, so they can disagree. Compare "
                   "walk-forward with the fixed-stop/target row: if both are similar, the difference comes "
                   "from which years were tested, not from re-picking the stop and target.")
        st.caption(f"Costs average {row.costs_per_trade_pct:.2f}% of position value per round trip "
                   f"({row.costs_per_trade_r:.2f}R).")

    curve, pf = portfolio_curve(tr, S)
    if not curve.empty:
        st.markdown(f"**Account replay**: ₹{S['profile']['capital']:,.0f} start, {pf['slots']} positions max, "
                    f"{pf['taken']} trades taken, {pf['skipped']} skipped because all slots were full."
                    + (f" **The capital was used up by {pf['ruined_on']}; no trades after that.**"
                       if pf.get("ruined_on") else ""))
        st.line_chart(curve.set_index("date")["equity"])
    yr = yearly(tr)
    if not yr.empty:
        st.markdown("**By year** (entry year)")
        st.bar_chart(yr.set_index("year")["total_r"])
        st.dataframe(yr.round(2), hide_index=True)
    wf_tab, _ = walk_forward(grid[grid["setup"] == sid] if not grid.empty else grid, bt["walk_forward"])
    if not wf_tab.empty:
        st.markdown("**Walk-forward**: stop/target chosen each year from the previous years only")
        st.dataframe(wf_tab.round(2), hide_index=True)
    with st.expander(f"All {len(tr)} trades"):
        show = tr.drop(columns=[c for c in ("run_id",) if c in tr]).sort_values("entry_date", ascending=False)
        st.dataframe(show, hide_index=True, width="stretch")
        st.download_button("Download trades CSV", show.to_csv(index=False), f"{sid}_trades.csv", "text/csv")


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


PAGES = {"Scanner": page_scanner, "Stock chart": page_chart, "Backtest": page_backtest,
         "Data health": page_health, "Settings": page_settings}

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
    st.caption("Phase 2 · scanner + backtest")

header(ov)
if ov is None:
    st.info(f"No database at `{DB}`. Follow the README: `python -m nse_research.cli init`, then "
            "`backfill-fast`, then `daily`. Or try `python -m nse_research.cli demo` first.")
else:
    PAGES[page](ov)
