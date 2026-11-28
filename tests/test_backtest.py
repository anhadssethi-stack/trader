"""Unit tests for the backtester: exits, costs, sizing, statistics, look-ahead."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from nse_research.backtest.costs import trade_costs
from nse_research.backtest.engine import backtest_symbol, position_size, simulate, trade_pnl
from nse_research.backtest.setups import SETUP_BY_ID, SETUPS, setup_signals
from nse_research.backtest.stats import (fixed_params_in_years, max_drawdown_r, oos_split, portfolio_curve,
                                         probability_sentence, setup_summary_table, summarize, walk_forward,
                                         wilson)
from nse_research.indicators.core import add_indicators
from nse_research.scanner.rules import breakouts

from .conftest import make_ohlc


def arr(*rows):
    """rows of (open, high, low, close) -> four arrays"""
    a = np.array(rows, float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


# ------------------------------------------------------------------ simulate()
def test_long_hits_target():
    o, h, l, c = arr((100, 101, 99, 100), (100, 103, 99.5, 102), (102, 106, 101, 105))
    r = simulate(o, h, l, c, 0, "long", atr=1.0, stop_atr=2, target_r=2, max_hold=10, slip=0)
    assert (r.entry, r.stop, r.target) == (100, 98, 104)
    assert r.reason == "target" and r.exit_price == 104 and r.exit_idx == 2


def test_long_hits_stop():
    o, h, l, c = arr((100, 101, 99, 100), (99, 99.5, 97.5, 98))
    r = simulate(o, h, l, c, 0, "long", 1.0, 2, 2, 10, 0)
    assert r.reason == "stop" and r.exit_price == 98


def test_same_bar_touching_both_counts_as_stop():
    o, h, l, c = arr((100, 100.5, 99.5, 100), (100, 105, 97, 101))
    r = simulate(o, h, l, c, 0, "long", 1.0, 2, 2, 10, 0)
    assert r.reason == "stop"


def test_gap_through_stop_fills_at_open_not_stop():
    o, h, l, c = arr((100, 100.5, 99.5, 100), (95, 96, 94, 95))
    r = simulate(o, h, l, c, 0, "long", 1.0, 2, 2, 10, 0)
    assert r.reason == "stop" and r.exit_price == 95  # worse than the 98 stop


def test_gap_over_target_fills_at_open():
    o, h, l, c = arr((100, 100.5, 99.5, 100), (107, 108, 106, 107))
    r = simulate(o, h, l, c, 0, "long", 1.0, 2, 2, 10, 0)
    assert r.reason == "target" and r.exit_price == 107


def test_time_exit_and_data_end():
    flat = [(100, 100.5, 99.5, 100)] * 6
    o, h, l, c = arr(*flat)
    r = simulate(o, h, l, c, 0, "long", 1.0, 2, 2, max_hold=3, slip=0)
    assert r.reason == "time" and r.exit_idx == 2
    r = simulate(o, h, l, c, 3, "long", 1.0, 2, 2, max_hold=10, slip=0)
    assert r.reason == "data_end" and r.exit_idx == 5


def test_short_mirror_and_slippage_is_against_you():
    o, h, l, c = arr((100, 100.5, 99.5, 100), (99, 99.5, 95, 96))
    r = simulate(o, h, l, c, 0, "short", 1.0, 2, 2, 10, slip=0.001)
    assert r.entry == pytest.approx(99.9)           # sold lower
    assert r.stop == pytest.approx(101.9) and r.target == pytest.approx(95.9)
    assert r.reason == "target"
    assert r.exit_price == pytest.approx(95.9 * 1.001)  # bought back higher


def test_hit_levels_before_stop():
    o, h, l, c = arr((100, 100.5, 99.5, 100), (100, 102.5, 99, 102), (102, 102, 97, 97.5))
    r = simulate(o, h, l, c, 0, "long", 1.0, 2, 3, 10, 0, r_levels=(1, 2))
    assert r.hit_levels == {1: True, 2: False}
    assert r.reason == "stop"


# ------------------------------------------------------------------ costs & sizing
def test_delivery_costs_match_hand_calculation(settings):
    c = trade_costs(100_000, 110_000, "delivery", settings["costs"])
    assert c.brokerage == pytest.approx(20 + 20)              # 0.03% capped at ₹20 per order
    assert c.stt == pytest.approx(100 + 110)                  # 0.1% each side
    assert c.stamp == pytest.approx(15)                       # 0.015% on buy
    assert c.exchange == pytest.approx(210_000 * 0.00297 / 100)
    assert c.gst == pytest.approx((c.brokerage + c.exchange + c.sebi) * 0.18)
    assert c.dp == pytest.approx(15.93)


def test_futures_short_pays_stt_on_sell_side_only(settings):
    c = trade_costs(buy_value=95_000, sell_value=100_000, segment="futures", costs_cfg=settings["costs"])
    assert c.stt == pytest.approx(100_000 * 0.02 / 100)
    assert c.stamp == pytest.approx(95_000 * 0.002 / 100)


def test_position_size_uses_risk_then_cap(settings):
    # ₹50L capital, 2% risk = ₹1L, 10% cap = ₹5L
    assert position_size(100, 50, settings) == 2000          # 1L / 50 = 2000 sh = ₹2L (< cap)
    assert position_size(100, 98, settings) == 5000          # 1L / 2 = 50,000 sh, capped to 5L / 100


def test_trade_pnl_net_of_costs(settings):
    gross, cost = trade_pnl("long", 100, 110, 1000, settings["costs"])
    assert gross == 10_000 and 200 < cost < 300


# ------------------------------------------------------------------ statistics
def test_wilson_interval():
    lo, hi = wilson(50, 100)
    assert lo == pytest.approx(40.4, abs=0.1) and hi == pytest.approx(59.6, abs=0.1)
    lo, hi = wilson(0, 10)
    assert lo == 0 and 25 < hi < 35
    assert all(np.isnan(wilson(0, 0)))


def test_max_drawdown_r():
    assert max_drawdown_r(pd.Series([1, 1, -1, -2, 3, -1])) == 3
    assert max_drawdown_r(pd.Series([-1, -1])) == 2


def _trades(rs, start="2020-01-01", side="long", setup="X"):
    d = pd.bdate_range(start, periods=len(rs), freq="10B")
    return pd.DataFrame({
        "trade_id": range(len(rs)), "setup": setup, "side": side, "symbol": [f"S{i % 7}" for i in range(len(rs))],
        "entry_date": d.date, "exit_date": (d + pd.Timedelta(days=5)).date, "bars": 5,
        "entry": 100.0, "stop": 98.0, "qty": 1000, "r": rs, "ret_pct": np.array(rs) * 2.0,
        "net_pnl": np.array(rs) * 2000.0, "gross_pnl": np.array(rs) * 2000.0 + 250, "costs": 250.0,
        "exit_reason": ["target" if r > 0 else "stop" for r in rs], "hit_1r": [r > 0 for r in rs],
        "in_current_universe": True, "yahoo_share": 0.0,
    })


def test_summarize_core_numbers(settings):
    s = summarize(_trades([2, -1, -1, 2, -1]), settings)
    assert s["trades"] == 5 and s["win_rate"] == 40
    assert s["expectancy_r"] == pytest.approx(0.2)
    assert s["profit_factor"] == pytest.approx(4 / 3)
    assert s["avg_win_r"] == 2 and s["avg_loss_r"] == -1
    assert s["confidence"].startswith("LOW")
    assert s["target_before_stop_pct"] == 40


def test_open_trades_are_excluded(settings):
    t = _trades([2, -1, 1])
    t.loc[2, "exit_reason"] = "open"
    assert summarize(t, settings)["trades"] == 2


def test_portfolio_respects_slot_limit(settings):
    t = _trades([1] * 15)
    t["entry_date"] = date(2021, 1, 4)        # 15 signals on one day, only 10 slots
    t["exit_date"] = date(2021, 2, 1)
    _, pf = portfolio_curve(t, settings)
    assert pf["taken"] == 10 and pf["skipped"] == 5


def test_portfolio_stops_when_capital_is_gone(settings):
    t = _trades([-1] * 100)
    t["net_pnl"] = -1_000_000.0                 # ₹10 lakh lost per trade on ₹50 lakh capital
    _, pf = portfolio_curve(t, settings)
    assert pf["taken"] == 5 and pf["ruined_on"] is not None and pf["max_dd_pct"] == 100.0


def test_summary_table_keeps_setups_without_trades(settings):
    tab = setup_summary_table(_trades([1, -1] * 20, setup="L_golden_cross"), pd.DataFrame(), settings,
                              ["L_golden_cross", "L_delivery_up"])
    row = tab.set_index("setup").loc["L_delivery_up"]
    assert row["trades"] == 0 and row["confidence"]


def test_fixed_params_in_years():
    g = pd.DataFrame({"stop_atr": [2.0, 2.0, 3.0], "target_r": [2.0, 2.0, 3.0], "exit_reason": "target",
                      "entry_date": [date(2020, 5, 1), date(2018, 5, 1), date(2020, 5, 1)], "r": [1.0, 5.0, 9.0]})
    assert fixed_params_in_years(g, [2020], 2.0, 2.0)["r"].tolist() == [1.0]


def test_oos_split_by_date():
    ins, oos, cut = oos_split(_trades([1] * 100), 0.3)
    assert len(ins) + len(oos) == 100 and 25 <= len(oos) <= 35
    assert pd.to_datetime(ins["entry_date"]).max() < cut <= pd.to_datetime(oos["entry_date"]).min()


def test_walk_forward_uses_only_past_trades():
    rows = []
    for y in range(2016, 2022):
        for k in range(30):
            ed = date(y, 3, 1) + pd.Timedelta(days=k).to_pytimedelta()
            # combo A is great before 2020 and terrible after; combo B is the opposite
            a = 1.0 if y < 2020 else -1.0
            rows += [{"trade_id": f"{y}{k}", "setup": "X", "stop_atr": 2.0, "target_r": 2.0, "entry_date": ed,
                      "exit_date": ed, "exit_reason": "target", "r": a},
                     {"trade_id": f"{y}{k}", "setup": "X", "stop_atr": 3.0, "target_r": 3.0, "entry_date": ed,
                      "exit_date": ed, "exit_reason": "target", "r": -a}]
    tab, oos = walk_forward(pd.DataFrame(rows), {"train_years": 3, "min_train_trades": 20})
    assert tab["year"].tolist() == [2019, 2020, 2021]
    r2020 = tab[tab.year == 2020].iloc[0]
    # 2020 is chosen from 2017-2019 only, where A looked best, so it loses in 2020
    assert (r2020.stop_atr, r2020.test_expectancy_r) == (2.0, -1.0)


def test_probability_sentence_wording(settings):
    s = summarize(_trades([2, -1, -1, 2, -1]), settings)
    txt = probability_sentence(s, 2.0, settings["backtest"])
    assert txt.startswith("This setup historically hit its 2R target before its stop 40% of the time over 5 trades")
    assert "LOW CONFIDENCE" in txt and "chance" not in txt


# ------------------------------------------------------------------ setups & look-ahead
def test_setup_matches_scanner_rule(cfg):
    rng = np.random.default_rng(3)
    df = make_ohlc(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, 400))))
    df["volume"] = rng.lognormal(11, 0.7, 400)
    d = add_indicators(df, cfg)
    sig = setup_signals(d, cfg, [SETUP_BY_ID["L_breakout_short"], SETUP_BY_ID["L_breakout_long"]])
    for t in range(220, 400):
        fired = {s.id for s in breakouts(d.iloc[:t + 1], cfg)}
        if "breakout_55" in fired:
            assert sig["L_breakout_long"].iloc[t]
        if "breakout_20" in fired:
            assert sig["L_breakout_short"].iloc[t]


def test_signals_do_not_change_when_future_bars_are_added(cfg):
    rng = np.random.default_rng(4)
    df = make_ohlc(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400))))
    df["volume"] = rng.lognormal(11, 0.7, 400)
    full = setup_signals(add_indicators(df, cfg), cfg)
    cut = setup_signals(add_indicators(df.iloc[:300], cfg), cfg)
    pd.testing.assert_frame_equal(full.iloc[:300], cut)


def test_entry_is_next_day_open_and_no_overlap(settings):
    close = np.r_[np.full(230, 100.0), np.linspace(101, 140, 60)]
    df = make_ohlc(close, vol=1e6)          # ₹10 crore a day: passes the liquidity filter
    df["open"] = df["close"] * 0.995
    df["high"] = df["close"] * 1.001        # so a rising close clears the prior high
    df.loc[230:, "volume"] = 3e6            # volume to qualify breakouts
    df["symbol"] = "AAA"
    df["source"] = "nse_udiff"
    trades, _ = backtest_symbol("AAA", df, settings, None, [SETUP_BY_ID["L_breakout_short"]], set(), {"AAA"})
    assert trades, "expected at least one breakout trade"
    t0 = trades[0]
    sig_i = df.index[df["date"].dt.date == t0["signal_date"]][0]
    assert t0["entry_date"] == df["date"].iloc[sig_i + 1].date()
    assert t0["entry"] == pytest.approx(df["open"].iloc[sig_i + 1] * 1.001)
    for a, b in zip(trades, trades[1:]):     # one trade at a time per symbol and setup
        assert b["entry_date"] > a["exit_date"]


def test_shorts_only_on_fno_stocks(settings):
    df = make_ohlc(np.r_[np.full(230, 100.0), np.linspace(99, 60, 60)], vol=1e6)
    df.loc[230:, "volume"] = 3e6
    df["low"] = df["close"] * 0.999
    df["symbol"], df["source"] = "BBB", "nse_udiff"
    s = [SETUP_BY_ID["S_breakdown_short"]]
    assert backtest_symbol("BBB", df, settings, None, s, set(), set())[0] == []
    assert backtest_symbol("BBB", df, settings, None, s, {"BBB"}, set())[0] != []


def test_every_setup_has_logic_text_and_valid_side():
    assert len({s.id for s in SETUPS}) == len(SETUPS)
    for s in SETUPS:
        assert s.side in ("long", "short") and len(s.logic) > 20


def test_top_turnover_days_is_point_in_time(tmp_path, settings):
    from nse_research.backtest.engine import top_turnover_days
    from nse_research.data.sources.base import PRICE_COLS
    from nse_research.data.store import Store
    st = Store(tmp_path / "t.duckdb")
    days = pd.bdate_range("2021-01-01", periods=60)
    rows = []
    for k, d in enumerate(days):
        # BIG is the most traded early on; SMALL overtakes it after day 30; MID always second-ish
        for sym, t in [("BIG", 9e8 if k < 30 else 1e7), ("SMALL", 1e6 if k < 30 else 5e9), ("MID", 5e8)]:
            rows.append({"date": d.date(), "symbol": sym, "series": "EQ", "isin": None, "open": 100, "high": 101,
                         "low": 99, "close": 100, "prev_close": 100, "volume": t / 100, "turnover": t,
                         "trades": 1, "source": "nse_udiff"})
    st.upsert_prices(pd.DataFrame(rows)[PRICE_COLS])
    settings["backtest"]["top_n"] = 2
    top = top_turnover_days(st, settings)
    assert pd.Timestamp(days[5]) in top["BIG"] and pd.Timestamp(days[5]) not in top.get("SMALL", set())
    assert pd.Timestamp(days[59]) in top["SMALL"] and pd.Timestamp(days[59]) not in top["BIG"]


def test_save_backtest_adds_new_columns_to_old_tables(tmp_path):
    from nse_research.data.store import Store
    st = Store(tmp_path / "b.duckdb")
    old = _trades([1, -1])
    st.save_backtest("r1", old, pd.DataFrame(), {})
    st.save_backtest("r2", old.assign(turnover_cr=12.5), pd.DataFrame(), {})
    tr, _, _ = st.load_backtest("r2")
    assert tr["turnover_cr"].tolist() == [12.5, 12.5]


def test_tiny_atr_stocks_are_not_traded(cfg, settings):
    from nse_research.backtest.engine import _eligible
    d = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=3), "close": [1000.0] * 3,
                      "atr": [0.05, 5.0, 20.0], "atr_pct": [0.005, 0.5, 2.0], "turnover_avg_cr": [50.0] * 3,
                      f"dma{max(cfg['dma_periods'])}": [1000.0] * 3})
    assert _eligible(d, settings["backtest"], cfg).tolist() == [False, True, True]
