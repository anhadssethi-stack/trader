"""Store, adjustment and fallback behaviour of the data layer."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from nse_research.data.adjust import apply_adjustments, detect_corp_actions
from nse_research.data.http import FetchError, NotFound
from nse_research.data.pipeline import Pipeline, latest_expected_day
from nse_research.data.sources.base import PRICE_COLS, finish
from nse_research.data.sources.yahoo import unadjust_splits
from nse_research.data.store import Store


def px_rows(day, symbols=("AAA", "BBB"), close=100.0, prev=None, source="nse_udiff"):
    return finish(pd.DataFrame([{
        "date": day, "symbol": s, "series": "EQ", "isin": None, "open": close, "high": close,
        "low": close, "close": close, "prev_close": prev if prev is not None else close,
        "volume": 1000, "turnover": 1000 * close, "trades": 10, "source": source} for s in symbols]),
        PRICE_COLS)


# ------------------------------------------------------------------ store
def test_upsert_is_idempotent_and_keep_existing_protects_nse_rows(tmp_path):
    st = Store(tmp_path / "a.duckdb")
    st.upsert_prices(px_rows(date(2026, 1, 5)))
    st.upsert_prices(px_rows(date(2026, 1, 5)))
    assert len(st.raw_prices()) == 2
    st.upsert_prices(px_rows(date(2026, 1, 5), close=999, source="yahoo"), keep_existing=True)
    assert set(st.raw_prices()["source"]) == {"nse_udiff"}
    st.upsert_prices(px_rows(date(2026, 1, 5), close=101))  # NSE re-fetch replaces
    assert st.raw_prices()["close"].iloc[0] == 101


def test_corp_action_dedupe_prefers_nse(tmp_path):
    st = Store(tmp_path / "a.duckdb")
    st.upsert_corp_actions(pd.DataFrame([{"symbol": "AAA", "ex_date": date(2026, 1, 7), "factor": 0.5,
                                          "source": "yahoo"}]))
    st.upsert_corp_actions(pd.DataFrame([{"symbol": "AAA", "ex_date": date(2026, 1, 6), "factor": 0.5,
                                          "source": "nse_prevclose"}]))
    ca = st.corp_actions()
    assert len(ca) == 1 and ca.iloc[0]["source"] == "nse_prevclose"


# ------------------------------------------------------------------ adjustments
def test_detect_and_apply_split():
    days = pd.bdate_range("2026-01-05", periods=4).date
    raw = pd.concat([
        px_rows(days[0], ["AAA"], 200, 198), px_rows(days[1], ["AAA"], 202, 200),
        px_rows(days[2], ["AAA"], 102, 101),  # 1:2 split, NSE prev close = 202/2
        px_rows(days[3], ["AAA"], 104, 102),
    ])
    ca = detect_corp_actions(raw, 0.02)
    assert len(ca) == 1 and ca.iloc[0]["factor"] == pytest.approx(0.5)
    adj = apply_adjustments(raw, ca)
    assert list(adj["close"]) == pytest.approx([100, 101, 102, 104])
    assert list(adj["volume"]) == pytest.approx([2000, 2000, 1000, 1000])


def test_detect_ignores_gaps_in_our_data():
    days = pd.bdate_range("2026-01-05", periods=3).date
    raw = pd.concat([px_rows(days[0], ["AAA"], 200), px_rows(days[2], ["AAA"], 150, prev=160)])
    market_days = list(days)  # day 1 exists in the market but is missing for AAA
    assert detect_corp_actions(raw, 0.02, market_days).empty


def test_yahoo_unadjust_restores_raw_prices():
    idx = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    yahoo = pd.DataFrame({"Open": [100, 101, 102.0], "High": [100, 101, 102.0], "Low": [100, 101, 102.0],
                          "Close": [100, 101, 102.0], "Volume": [2000, 2000, 1000.0],
                          "Stock Splits": [0, 0, 2.0]}, index=idx)
    raw, acts = unadjust_splits(yahoo)
    assert list(raw["Close"]) == [200, 202, 102]
    assert list(raw["Volume"]) == [1000, 1000, 1000]
    assert acts == [(date(2026, 1, 7), 0.5)]


# ------------------------------------------------------------------ fallback
class FakeSrc:
    def __init__(self, behaviour, name):
        self.behaviour, self.name = behaviour, name
        self.calls = 0

    def supports(self, d):
        return True

    def fetch_day(self, d):
        self.calls += 1
        if self.behaviour == "notfound":
            raise NotFound("x")
        if self.behaviour == "blocked":
            raise FetchError("HTTP 403")
        if self.behaviour == "broken":
            raise KeyError("TckrSymb")  # e.g. NSE changed the file format
        return px_rows(d, source=self.name)

    fetch_delivery = None


class FakeYahoo:
    def __init__(self):
        self.calls = 0

    def fetch_range(self, symbols, start, end):
        self.calls += 1
        days = pd.bdate_range(start, end).date
        return pd.concat([px_rows(d, symbols, source="yahoo") for d in days]), \
            pd.DataFrame(columns=["symbol", "ex_date", "factor", "source"])

    def fetch_index(self, *a):
        return finish(pd.DataFrame(), ["date", "index_name", "open", "high", "low", "close", "source"])


class FakeLists:
    def index_constituents(self, index):
        return pd.DataFrame({"symbol": ["AAA", "BBB"], "company": ["A", "B"], "industry": ["X", "Y"],
                             "series": ["EQ", "EQ"], "isin": [None, None]})

    def fno_symbols(self):
        return pd.DataFrame({"symbol": ["AAA"]})


def _pipe(settings, primary, secondary):
    st = Store(settings.path("db_path"))
    srcs = {"nse_udiff": primary, "nse_legacy": FakeSrc("notfound", "nse_legacy"),
            "nse_full_bhav": secondary, "nse_mto": None, "nse_index_close": FakeSrc("notfound", "ix"),
            "yahoo": FakeYahoo(), "lists": FakeLists()}
    return Pipeline(settings, st, http=object(), sources=srcs), st


def test_falls_back_to_next_nse_source_when_primary_is_broken(settings):
    pipe, st = _pipe(settings, FakeSrc("broken", "nse_udiff"), FakeSrc("ok", "nse_full_bhav"))
    rep = pipe.update(date(2026, 1, 5), date(2026, 1, 9))
    assert set(rep.prices.values()) == {"nse_full_bhav"}
    assert set(st.raw_prices()["source"]) == {"nse_full_bhav"}
    errs = st.q("SELECT * FROM fetch_log WHERE status='error'")
    assert len(errs) == 5 and "KeyError" in errs["message"].iloc[0]


def test_falls_back_to_yahoo_when_nse_blocked(settings):
    pipe, st = _pipe(settings, FakeSrc("blocked", "nse_udiff"), FakeSrc("blocked", "nse_full_bhav"))
    rep = pipe.update(date(2026, 1, 5), date(2026, 1, 9))
    assert set(rep.prices.values()) == {"yahoo"}
    assert pipe.src["yahoo"].calls == 1  # one batched call for all missing days
    assert len(st.raw_prices()) == 10


def test_days_with_no_file_anywhere_become_holidays_and_are_not_retried(settings):
    pipe, st = _pipe(settings, FakeSrc("notfound", "nse_udiff"), FakeSrc("notfound", "nse_full_bhav"))
    pipe.src["yahoo"].fetch_range = lambda *a: (finish(pd.DataFrame(), PRICE_COLS),
                                               pd.DataFrame(columns=["symbol", "ex_date", "factor", "source"]))
    rep = pipe.update(date(2026, 1, 5), date(2026, 1, 6))
    assert set(rep.prices.values()) == {"holiday"}
    rep2 = pipe.update(date(2026, 1, 5), date(2026, 1, 6))
    assert rep2.days_requested == 0


def test_already_stored_days_are_not_refetched(settings):
    primary = FakeSrc("ok", "nse_udiff")
    pipe, st = _pipe(settings, primary, FakeSrc("ok", "nse_full_bhav"))
    pipe.update(date(2026, 1, 5), date(2026, 1, 9))
    calls = primary.calls
    pipe.update(date(2026, 1, 5), date(2026, 1, 9))
    assert primary.calls == calls


def test_latest_expected_day_respects_publish_time_and_weekends():
    from datetime import datetime

    from nse_research.data.pipeline import IST
    assert latest_expected_day(19, datetime(2026, 11, 27, 18, 0, tzinfo=IST)) == date(2026, 11, 26)
    assert latest_expected_day(19, datetime(2026, 11, 27, 20, 0, tzinfo=IST)) == date(2026, 11, 27)
    assert latest_expected_day(19, datetime(2026, 11, 29, 20, 0, tzinfo=IST)) == date(2026, 11, 27)


def test_yahoo_filled_days_are_upgraded_to_nse_when_it_comes_back(settings):
    primary = FakeSrc("blocked", "nse_udiff")
    pipe, st = _pipe(settings, primary, FakeSrc("blocked", "nse_full_bhav"))
    pipe.update(date(2026, 1, 5), date(2026, 1, 9))
    assert set(st.raw_prices()["source"]) == {"yahoo"}
    pipe.src["nse_udiff"] = FakeSrc("ok", "nse_udiff")
    pipe.update(date(2026, 1, 5), date(2026, 1, 9))
    assert set(st.raw_prices()["source"]) == {"nse_udiff"}


class StaleFileSrc(FakeSrc):
    """Mimics NSE serving the previous trading day's file under a holiday's date."""

    def fetch_day(self, d):
        self.calls += 1
        return px_rows(pd.Timestamp(d) - pd.tseries.offsets.BDay(1), source=self.name)


def test_file_for_a_different_day_is_not_stored_and_day_becomes_holiday(settings):
    pipe, st = _pipe(settings, FakeSrc("notfound", "nse_udiff"), StaleFileSrc("ok", "nse_full_bhav"))
    pipe.src["yahoo"].fetch_range = lambda *a: (finish(pd.DataFrame(), PRICE_COLS),
                                               pd.DataFrame(columns=["symbol", "ex_date", "factor", "source"]))
    rep = pipe.update(date(2026, 11, 24), date(2026, 11, 24))
    assert rep.prices[date(2026, 11, 24)] == "holiday"
    assert st.raw_prices().empty
    msg = st.q("SELECT message FROM fetch_log WHERE source='nse_full_bhav'")["message"].iloc[0]
    assert "2026-11-23" in msg


def test_index_days_from_yahoo_are_upgraded_to_nse(settings):
    pipe, st = _pipe(settings, FakeSrc("ok", "nse_udiff"), FakeSrc("ok", "nse_full_bhav"))
    st.upsert_index(pd.DataFrame({"date": [date(2026, 1, 5)], "index_name": ["Nifty 500"], "open": [1.0],
                                  "high": [1.0], "low": [1.0], "close": [1.0], "source": ["yahoo"]}))

    class Ix:
        calls = 0

        def fetch_day(self, d):
            Ix.calls += 1
            return pd.DataFrame({"date": [d], "index_name": ["Nifty 500"], "open": [2.0], "high": [2.0],
                                 "low": [2.0], "close": [2.0], "source": ["nse_index_close"]})
    pipe.src["nse_index_close"] = Ix()
    pipe.update(date(2026, 1, 5), date(2026, 1, 5))
    assert st.index_prices("Nifty 500")["source"].tolist() == ["nse_index_close"]
