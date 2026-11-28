"""NSE Research dashboard: scanner, charts, fundamentals, smart money, backtests.

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


@st.cache_data(ttl=300)
def load_fundamentals_screen():
    from nse_research.fundamentals.screen import build_screen
    return _with_store(lambda s: build_screen(S, s))


@st.cache_data(ttl=300)
def load_fund_detail(symbol: str):
    return _with_store(lambda s: s.load_fundamentals(symbol))


@st.cache_data(ttl=300)
def load_smart_money():
    def f(s: Store):
        return dict(shareholding=s.shareholding_all(), holders=s.holders_all(), deals=s.deals_all(),
                    flows=s.flows_all(),
                    shp_checked=s.q("SELECT max(value) AS v FROM meta WHERE key LIKE 'shp_checked:%'")["v"].iloc[0])
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

    fs = load_fundamentals_screen()
    if fs is not None and not fs.empty:
        fsi = fs.set_index("symbol")
        df = df.assign(f_red=df["symbol"].map(fsi["n_red"]), f_green=df["symbol"].map(fsi["n_green"]))
        max_red = st.slider("Maximum fundamental red flags (stocks without fundamentals are kept)", 0, 8, 8)
        df = df[df["f_red"].isna() | (df["f_red"] <= max_red)]
    rs_cols = [f"rs{n}" for n in S["scanner"]["rs_lookbacks"]]
    view = pd.DataFrame({
        "Symbol": df["symbol"], "Company": df["company"], "Industry": df["industry"],
        "Close": df["close"], "Chg %": df["change_pct"], "RSI": df["rsi"],
        "Vol × avg": df["vol_ratio"], "Deliv %": df["deliv_pct"],
        **{f"RS {c[2:]}d": df[c] for c in rs_cols if c in df},
        "ATR %": df["atr_pct"], "# Bull": df["n_bullish"], "# Bear": df["n_bearish"],
        "Bullish signals": df["bullish"].apply(fmt_list), "Bearish signals": df["bearish"].apply(fmt_list),
        "Info": df["info"].apply(fmt_list), "F&O": df["fno"], "Stale": df.get("stale", False),
        **({"Fund. red": df["f_red"], "Fund. green": df["f_green"]} if "f_red" in df else {}),
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
    show_fund_flags(row["symbol"])
    show_smart_money_for(row["symbol"])


def show_fund_flags(sym: str):
    fs = load_fundamentals_screen()
    if fs is None or fs.empty or sym not in set(fs["symbol"]):
        st.caption("No fundamentals stored for this stock yet (run `python -m nse_research.cli fundamentals`).")
        return
    r = fs[fs["symbol"] == sym].iloc[0]
    st.markdown(f"**Fundamentals** ({r['source']}, {r['basis']}, fetched {r['fetched']})")
    if not r["flags"]:
        st.markdown("- No red or green fundamental flag.")
    for fl in sorted(r["flags"], key=lambda x: x["side"] != "red"):
        st.markdown(f"- {'🔴' if fl['side'] == 'red' else '🟢'} **{fl['label']}**: {fl['reason']}")


def show_smart_money_for(sym: str):
    sm = load_smart_money()
    if sm is None:
        return
    from nse_research.smartmoney.tracker import holder_changes, recent_deals, stake_changes
    c = S["smart_money"]
    stakes = stake_changes(sm["shareholding"][sm["shareholding"]["symbol"] == sym], c["min_stake_change_pp"])
    hold = holder_changes(sm["holders"][sm["holders"]["symbol"] == sym], c["investors"], c["min_holder_change_pp"])
    deals = recent_deals(sm["deals"][sm["deals"]["symbol"] == sym], c["investors"], c["deals_lookback_days"])
    if stakes.empty and hold.empty and deals.empty:
        return
    st.markdown("**Smart money** (delayed data: shareholding is quarterly; deals are end of day)")
    for r in stakes.itertuples():
        st.markdown(f"- {r.holder} {'raised' if r.change_pp > 0 else 'cut'} their stake "
                    f"{r.prev_pct:.2f}% → {r.pct:.2f}% ({r.change_pp:+.2f} pts) in the quarter to "
                    f"{pd.Timestamp(r.quarter_end):%d %b %Y}.")
    for r in hold.head(8).itertuples():
        who = f"{r.holder}" + (f" ({r.known_investor})" if isinstance(r.known_investor, str) else "")
        st.markdown(f"- {who}: {r.event}, {_pct(r.prev_pct, 2)} → {_pct(r.pct, 2)} "
                    f"(quarter to {pd.Timestamp(r.quarter_end):%d %b %Y}).")
    for r in deals.head(8).itertuples():
        st.markdown(f"- {pd.Timestamp(r.date):%d %b %Y} {r.kind} deal: {r.client} "
                    f"{'bought' if r.side == 'BUY' else 'sold'} {r.qty:,.0f} shares at ₹{r.price:,.2f} "
                    f"(₹{r.value_cr:,.1f} Cr).")


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
        f"Only stocks with ≥ ₹{p['min_avg_turnover_cr']} cr average daily turnover *on the signal day*"
        + (f", and among the {meta.get('top_n')} most-traded stocks that day." if meta.get("universe") == "top_turnover"
           else "."))
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


FUND_COLS = {
    "symbol": "Symbol", "company": "Company", "industry": "Industry", "price": "Price", "pe": "P/E",
    "sector_pe_median": "Sector P/E", "pb": "P/B", "roce_pct": "ROCE %", "roe_pct": "ROE %",
    "debt_to_equity": "Debt/Eq", "sales_cagr_3y": "Sales 3y %", "profit_cagr_3y": "Profit 3y %",
    "qtr_profit_yoy": "Qtr profit YoY %", "cfo_to_profit_3y": "CFO/Profit 3y", "promoter_pct": "Promoter %",
    "promoter_chg_4q": "Promoter Δ4q", "pledge_pct": "Pledged %", "n_red": "# Red", "n_green": "# Green",
}


def page_fundamentals(ov):
    st.header("Fundamental screen")
    fs = load_fundamentals_screen()
    if fs is None or fs.empty:
        st.info("No fundamentals yet. Run `python -m nse_research.cli fundamentals` (about 20 minutes for the "
                "Nifty 500 the first time; after that the daily job keeps them fresh).")
        return
    st.caption(f"{len(fs)} companies. Results are as reported (₹ crore), from Screener.in with Yahoo as a fallback; "
               "they change once a quarter and are usually 1-2 months old. Each flag is one threshold from "
               "settings.yaml and shows its numbers. There is no combined score. Banks/NBFCs/insurers skip the "
               "debt, ROCE and cash-flow flags and use ROE instead.")
    all_red = sorted({x for v in fs["red_flags"] for x in v})
    all_green = sorted({x for v in fs["green_flags"] for x in v})
    c1, c2, c3 = st.columns(3)
    max_red = c1.slider("Maximum red flags", 0, max(1, int(fs["n_red"].max())), 0)
    no_red = c2.multiselect("Must NOT have", all_red)
    need_green = c3.multiselect("Must have", all_green)
    inds = st.multiselect("Industry", sorted(fs["industry"].dropna().unique()))
    df = fs[fs["n_red"] <= max_red]
    if no_red:
        df = df[df["red_flags"].apply(lambda v: not set(no_red) & set(v))]
    if need_green:
        df = df[df["green_flags"].apply(lambda v: set(need_green) <= set(v))]
    if inds:
        df = df[df["industry"].isin(inds)]
    view = df.reindex(columns=list(FUND_COLS)).rename(columns=FUND_COLS)
    view["Red flags"] = df["red_flags"].apply(fmt_list)
    view["Green flags"] = df["green_flags"].apply(fmt_list)
    st.write(f"**{len(view)}** companies match.")
    st.dataframe(view, hide_index=True, width="stretch", height=480,
                 column_config={c: st.column_config.NumberColumn(format="%.2f") for c in FUND_COLS.values()
                                if c not in ("Symbol", "Company", "Industry", "# Red", "# Green")})
    st.download_button("Download as CSV", view.to_csv(index=False), "fundamentals.csv", "text/csv")

    st.subheader("Company detail")
    pick = st.selectbox("Company", df["symbol"].tolist(), index=None, placeholder="Choose a company")
    if not pick:
        return
    r = df[df["symbol"] == pick].iloc[0]
    f = load_fund_detail(pick)
    st.caption(f"Source: {r['source']} ({r['basis']} accounts), fetched {r['fetched']}. "
               + (f"[Open on Screener](https://www.screener.in/company/{pick}/)" if r["source"] == "screener" else ""))
    show_fund_flags(pick)
    with st.expander("Every metric and how it is calculated"):
        rows = [
            ("Sales growth, 3y / 5y (% a year)", r["sales_cagr_3y"], r["sales_cagr_5y"], "CAGR of annual sales"),
            ("Profit growth, 3y / 5y (% a year)", r["profit_cagr_3y"], r["profit_cagr_5y"],
             "CAGR of annual net profit (blank if either end is a loss)"),
            ("Latest quarter vs a year ago: sales / profit (%)", r["qtr_sales_yoy"], r["qtr_profit_yoy"],
             f"quarter ending {r['latest_quarter']}"),
            ("ROCE % / ROE %", r["roce_pct"], r["roe_pct"],
             "ROCE = EBIT / (equity + borrowings); ROE = profit / average equity. " + str(r["metric_sources"])),
            ("Debt/equity / interest cover", r["debt_to_equity"], r["interest_cover"],
             "borrowings / (share capital + reserves); (PBT + interest) / interest, last 12 months"),
            ("Operating cash / profit, 3y / free cash flow 3y (₹ Cr)", r["cfo_to_profit_3y"], r["fcf_3y_cr"],
             "sum of 3 years' operating cash flow / sum of 3 years' net profit"),
            ("Debtor days now / 3 years ago", r["debtor_days"], r["debtor_days_3y_ago"], "receivables / sales × 365"),
            ("P/E / sector median", r["pe"], r["sector_pe_median"],
             f"price / last 4 quarters' EPS; median of {r['sector_peers']} profitable peers in {r['peer_group']}"),
            ("Promoter % / change over 4 quarters", r["promoter_pct"], r["promoter_chg_4q"],
             f"shareholding quarter {r['shareholding_quarter']}"),
            ("Pledged % of promoter shares", r["pledge_pct"], None, "NSE pledge disclosures"),
        ]
        st.dataframe(pd.DataFrame(rows, columns=["Metric", "Value", "Second value", "How"]).round(2),
                     hide_index=True, width="stretch")
    if f is not None:
        c1, c2 = st.columns(2)
        ann = pd.DataFrame({"Sales": f.series("annual", "sales"), "Net profit": f.series("annual", "net_profit")})
        if not ann.empty:
            fig = go.Figure([go.Bar(x=ann.index, y=ann[c], name=c) for c in ann])
            fig.update_layout(title="Annual (₹ Cr)", height=320, margin=dict(l=10, r=10, t=40, b=10))
            c1.plotly_chart(fig, width="stretch")
        q = pd.DataFrame({"Sales": f.series("quarterly", "sales"), "Net profit": f.series("quarterly", "net_profit")})
        if not q.empty:
            fig = go.Figure([go.Bar(x=q.index, y=q[c], name=c) for c in q])
            fig.update_layout(title="Quarterly (₹ Cr)", height=320, margin=dict(l=10, r=10, t=40, b=10))
            c2.plotly_chart(fig, width="stretch")
        sh = f.shareholding
        if not sh.empty:
            fig = go.Figure([go.Scatter(x=pd.to_datetime(g["quarter_end"]), y=g["pct"], name=cat, mode="lines+markers")
                             for cat, g in sh.groupby("category")])
            fig.update_layout(title="Shareholding (% of shares)", height=320, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, width="stretch")
    show_smart_money_for(pick)


def _r2(df: pd.DataFrame) -> pd.DataFrame:
    """Round number columns only (rounding a date column warns)."""
    return df.round({c: 2 for c in df.select_dtypes("number").columns})


def page_smart_money(ov):
    from nse_research.smartmoney.tracker import deal_summary, holder_changes, recent_deals, stake_changes
    st.header("Smart-money tracker")
    sm = load_smart_money()
    c = S["smart_money"]
    st.info("**This data is delayed.** Shareholding patterns are quarterly and companies file them up to 21 days "
            "after the quarter ends, so a stake change can be 3 to 15 weeks old when you see it. Bulk and block "
            "deals are published after market close. FII/DII flows are NSE's provisional figures for the day. "
            "A stake change says who bought, not what happens next.", icon="⏱️")
    if sm is None:
        return
    uni = load_overview()["scan"]
    names = dict(zip(uni["symbol"], uni["company"])) if not uni.empty else {}
    t1, t2, t3, t4 = st.tabs(["Stake changes", "Well-known investors", "Bulk & block deals", "FII / DII flows"])
    with t1:
        cats = st.multiselect("Holders", ["fii", "dii", "mf", "promoter"], default=["fii", "dii", "mf"],
                              format_func=lambda x: {"fii": "FIIs", "dii": "DIIs", "mf": "Mutual funds",
                                                     "promoter": "Promoters"}[x])
        mn = st.number_input("Smallest change (percentage points)", 0.0, 10.0, float(c["min_stake_change_pp"]), 0.25)
        ch = stake_changes(sm["shareholding"], mn, tuple(cats))
        way = st.radio("Direction", ["Increased", "Decreased", "Both"], horizontal=True)
        if way == "Increased":
            ch = ch[ch["change_pp"] > 0]
        elif way == "Decreased":
            ch = ch[ch["change_pp"] < 0].sort_values("change_pp")
        ch = ch.assign(company=ch["symbol"].map(names))
        st.write(f"**{len(ch)}** changes in each stock's latest filed quarter vs the one before.")
        st.dataframe(_r2(ch[["symbol", "company", "holder", "prev_pct", "pct", "change_pp", "quarter_end"]])
                     .rename(columns={"prev_pct": "Before %", "pct": "Now %", "change_pp": "Change (pts)",
                                      "quarter_end": "Quarter"}), hide_index=True, width="stretch")
    with t2:
        only = st.checkbox("Only names on my watch list (settings.yaml → smart_money.investors)", value=True)
        hc = holder_changes(sm["holders"], c["investors"], c["min_holder_change_pp"], only_known=only)
        hc = hc.assign(company=hc["symbol"].map(names))
        st.caption("Holders named in the shareholding filing (all promoters, and other holders above 1%). "
                   "A holder below 1% is not named, so 'exited / below 1%' can mean either.")
        st.dataframe(_r2(hc[["symbol", "company", "holder", "known_investor", "event", "prev_pct", "pct",
                             "change_pp", "quarter_end"]]), hide_index=True, width="stretch")
    with t3:
        days = st.slider("Last N days", 7, 365, int(c["deals_lookback_days"]))
        rd = recent_deals(sm["deals"], c["investors"], days)
        if rd.empty:
            st.write("No deals stored yet. They are collected by the daily job from NSE's end-of-day files.")
        else:
            st.markdown("**Net buying by deal clients, per stock**")
            st.dataframe(_r2(deal_summary(rd)), hide_index=True, width="stretch")
            st.markdown("**All deals**")
            st.dataframe(_r2(rd), hide_index=True, width="stretch", height=400)
    with t4:
        fl = sm["flows"]
        if fl.empty:
            st.write("No flows stored yet. NSE only publishes the latest day, so the history builds up from the "
                     "first daily run.")
        else:
            fl = fl.assign(date=pd.to_datetime(fl["date"]))
            fig = go.Figure([go.Bar(x=g["date"], y=g["net_cr"], name=cat) for cat, g in fl.groupby("category")])
            fig.update_layout(barmode="group", height=360, title="Net buying in the cash market (₹ Cr)",
                              margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, width="stretch")
            last20 = fl[fl["date"] >= fl["date"].max() - pd.Timedelta(days=30)].groupby("category")["net_cr"].sum()
            st.write("Last 30 days, net: " + ", ".join(f"{k} ₹{v:,.0f} Cr" for k, v in last20.items()))


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


PAGES = {"Scanner": page_scanner, "Stock chart": page_chart, "Fundamentals": page_fundamentals,
         "Smart money": page_smart_money, "Backtest": page_backtest,
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
    st.caption("Phase 3 · scanner, backtest, fundamentals, smart money")

header(ov)
if ov is None:
    st.info(f"No database at `{DB}`. Follow the README: `python -m nse_research.cli init`, then "
            "`backfill-fast`, then `daily`. Or try `python -m nse_research.cli demo` first.")
else:
    PAGES[page](ov)
