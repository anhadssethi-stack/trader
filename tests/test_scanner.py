import numpy as np
import pandas as pd

from nse_research.indicators.core import add_indicators
from nse_research.scanner.engine import analyse_symbol, run_scan
from nse_research.scanner.rules import breakouts, evaluate, trend_stack, volume_spike

from .conftest import make_ohlc


def _ind(df, cfg, bench=None):
    d = add_indicators(df, cfg, bench)
    d["prev_close_adj"] = d["close"].shift(1)
    return d


def test_uptrend_and_downtrend(cfg):
    up = _ind(make_ohlc(np.linspace(100, 300, 260)), cfg)
    s = trend_stack(up, cfg)
    assert s.id == "trend_up" and "DMA200" in s.reason
    down = _ind(make_ohlc(np.linspace(300, 100, 260)), cfg)
    assert trend_stack(down, cfg).id == "trend_down"


def test_breakout_needs_volume(cfg):
    close = np.r_[np.full(80, 100.0), 110.0]
    df = make_ohlc(close)
    quiet = breakouts(_ind(df, cfg), cfg)
    assert quiet == []
    df.loc[df.index[-1], "volume"] = 2e5  # 2x average
    loud = breakouts(_ind(df, cfg), cfg)
    assert [s.id for s in loud] == ["breakout_55"]
    assert "2.0× 20-day avg" in loud[0].reason


def test_volume_spike_side_follows_the_day(cfg):
    close = np.r_[np.full(40, 100.0), 95.0]
    df = make_ohlc(close)
    df.loc[df.index[-1], "volume"] = 3e5
    s = volume_spike(_ind(df, cfg), cfg)
    assert s.side == "bearish" and "down day" in s.reason


def test_every_signal_has_a_reason_with_numbers(cfg):
    rng = np.random.default_rng(5)
    df = make_ohlc(100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, 400))))
    df["volume"] = rng.lognormal(11, 0.6, 400)
    df["deliv_pct"] = rng.uniform(20, 80, 400)
    bench = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400))), index=df["date"])
    for end in range(250, 400, 7):
        for s in evaluate(_ind(df.iloc[:end], cfg, bench), cfg):
            assert s.side in {"bullish", "bearish", "info"}
            assert any(ch.isdigit() for ch in s.reason), s


def test_analyse_symbol_summary(cfg):
    df = make_ohlc(np.linspace(100, 200, 300))
    df["symbol"] = "AAA"
    _, summ = analyse_symbol(df, cfg, None)
    assert summ["close"] == 200 and "Uptrend (DMAs stacked)" in summ["bullish"]
    assert summ["rs63"] is None  # no benchmark -> blank, not invented


def test_run_scan_on_demo_db(settings, tmp_path):
    from nse_research.demo import build_demo
    st = build_demo(tmp_path / "demo.duckdb", n_symbols=6, years=2)
    df = run_scan(settings, st, st.universe()["symbol"].tolist())
    assert len(df) == 6
    assert df["fno"].sum() == 3
    assert (df.loc[~df["fno"], "short_possible"] == False).all()  # noqa: E712
    run_date, saved = st.latest_scan()
    assert len(saved) == 6 and isinstance(saved["signals"].iloc[0], list)
