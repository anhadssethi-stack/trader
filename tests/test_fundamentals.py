from datetime import date

import pandas as pd
import pytest

from nse_research.data.store import Store
from nse_research.fundamentals.flags import add_sector_context, evaluate
from nse_research.fundamentals.metrics import cagr, compute, yoy
from nse_research.fundamentals.model import Fundamentals
from nse_research.fundamentals.screen import build_screen
from nse_research.smartmoney.tracker import deal_summary, holder_changes, recent_deals, stake_changes


def _years(n, end=2025):
    return [pd.Timestamp(f"{y}-03-31") for y in range(end - n + 1, end + 1)]


def _quarters(n, end="2025-09-30"):
    return list(pd.date_range(end=end, periods=n, freq="QE"))


def make_fund(sales=(100, 110, 121, 133.1, 146.41, 161.05), profit=(10, 11, 12.1, 13.31, 14.64, 16.1),
              cfo=None, borrowings=50, equity=(200, 210, 222, 235, 250, 266), q_profit=None,
              promoter=(60, 60, 59, 58, 57), debtor_days=None, fin=False, interest=5):
    yrs = _years(len(sales))
    rows = []
    for i, p in enumerate(yrs):
        rows += [("annual", "sales", p, sales[i]), ("annual", "net_profit", p, profit[i]),
                 ("annual", "pbt", p, profit[i] * 1.33), ("annual", "interest", p, interest),
                 ("balance", "equity_capital", p, 10), ("balance", "reserves", p, equity[i] - 10),
                 ("balance", "borrowings", p, borrowings),
                 ("cashflow", "cfo", p, (cfo[i] if cfo else profit[i] * 1.1)),
                 ("cashflow", "cfi", p, -profit[i] * 0.5)]
        if debtor_days:
            rows.append(("ratios", "debtor_days", p, debtor_days[i]))
    qs = _quarters(8)
    qp = q_profit or [3, 3.2, 3.4, 3.6, 3.8, 4.0, 4.2, 4.4]
    for i, p in enumerate(qs):
        rows += [("quarterly", "sales", p, 30 + i), ("quarterly", "net_profit", p, qp[i]),
                 ("quarterly", "eps", p, qp[i] / 10)]
    st = pd.DataFrame(rows, columns=["statement", "item", "period", "value"])
    sh = pd.DataFrame([(q, "promoter", v) for q, v in zip(_quarters(len(promoter)), promoter)],
                      columns=["quarter_end", "category", "pct"])
    return Fundamentals(symbol="ABC", source="test", statements=st, shareholding=sh,
                        ratios={"book_value": 26.6, "pledge_pct": 0.0}, sector="Industrials",
                        industry="Capital Goods", is_financial=fin, fetched=date(2025, 11, 1))


def test_cagr_and_yoy_basic():
    s = pd.Series([100, 110, 121, 133.1])
    assert cagr(s, 3) == pytest.approx(10.0)
    assert cagr(pd.Series([-5, 10, 20, 30]), 3) is None   # from a loss: no CAGR
    assert cagr(s, 5) is None                            # not enough history
    q = pd.Series([10, 11, 12, 13, 15])
    assert yoy(q) == pytest.approx(50.0)


def test_compute_core_metrics():
    m = compute(make_fund(), price=40.0)
    assert m["sales_cagr_3y"] == pytest.approx(10.0, abs=0.01)
    assert m["profit_cagr_5y"] == pytest.approx(10.0, abs=0.05)
    assert m["debt_to_equity"] == pytest.approx(50 / 266)
    # ROE = latest profit / average of last two equity values
    assert m["roe_pct"] == pytest.approx(16.1 / ((266 + 250) / 2) * 100)
    # computed ROCE = (PBT + interest) / (equity + borrowings)
    assert m["roce_pct"] == pytest.approx((16.1 * 1.33 + 5) / (266 + 50) * 100)
    assert m["cfo_to_profit_3y"] == pytest.approx(1.1)
    # P/E from price / sum of last 4 quarterly EPS
    assert m["pe"] == pytest.approx(40 / ((3.8 + 4.0 + 4.2 + 4.4) / 10))
    assert m["promoter_chg_4q"] == pytest.approx(-3.0)
    assert m["promoter_chg_1q"] == pytest.approx(-1.0)
    assert m["metric_sources"]["pe"].startswith("computed")


def _ids(m, cfg):
    return {f.id for f in evaluate(m, cfg)}


def test_flags_red(settings):
    cfg = settings["fundamentals"]
    f = make_fund(cfo=[2, 2, 2, 2, 2, 2], borrowings=400, promoter=(60, 59, 58, 57, 55),
                  q_profit=[3, 3, 3, 3, -1, 2, -2, 1], debtor_days=[40, 45, 50, 60, 75, 90])
    f.ratios["pledge_pct"] = 25.0
    m = compute(f, price=40.0)
    ids = _ids(m, cfg)
    assert {"weak_cash_conversion", "high_debt", "promoter_selling", "pledge_high", "recent_losses",
            "receivables_rising"} <= ids
    reasons = {x.id: x.reason for x in evaluate(m, cfg)}
    assert "25.0% of promoter shares" in reasons["pledge_high"]
    assert "3 years" in reasons["weak_cash_conversion"]


def test_flags_green_and_financials_skip_debt(settings):
    cfg = settings["fundamentals"]
    m = compute(make_fund(sales=(100, 120, 144, 173, 207, 249), profit=(10, 12, 14.4, 17.3, 20.7, 24.9),
                          borrowings=0, promoter=(55, 56, 57, 58, 58)), price=40.0)
    ids = _ids(m, cfg)
    assert {"strong_growth", "debt_free", "promoter_buying"} <= ids
    bank = compute(make_fund(borrowings=5000, fin=True), price=40.0)
    ids = _ids(bank, cfg)
    assert "high_debt" not in ids and "low_interest_cover" not in ids and "low_roce" not in ids


def test_sector_pe_context(settings):
    cfg = settings["fundamentals"]
    df = pd.DataFrame({"symbol": list("ABCDEF"), "industry": ["Cement"] * 5 + ["Rare"],
                       "sector": ["Materials"] * 6, "pe": [10, 20, 20, 20, 45, 30],
                       "profit_ttm": [1] * 6})
    out = add_sector_context(df, min_peers=4).set_index("symbol")
    assert out.loc["A", "sector_pe_median"] == 20
    assert out.loc["A", "sector_level"] == "industry"
    assert out.loc["F", "sector_level"] == "sector"     # only 1 peer in its industry
    e = {f.id for f in evaluate(out.loc["E"].to_dict(), cfg)}
    c = {f.id for f in evaluate(out.loc["A"].to_dict(), cfg)}
    assert "expensive_vs_sector" in e and "cheap_vs_sector" in c


def test_loss_making_not_valued(settings):
    m = {"pe": None, "profit_ttm": -50.0, "sector_pe_median": 20}
    assert "loss_making" in {f.id for f in evaluate(m, settings["fundamentals"])}


def test_store_roundtrip_and_screen(settings, tmp_path):
    st = Store(tmp_path / "f.duckdb")
    f = make_fund()
    st.save_fundamentals(f)
    st.upsert_pledges(pd.DataFrame({"symbol": ["ABC"], "as_of": ["2025-09-30"], "promoter_pct": [57.0],
                                    "pledge_pct": [12.0], "source": ["nse"]}))
    st.upsert_prices(pd.DataFrame({"date": [pd.Timestamp("2025-11-03")], "symbol": ["ABC"], "series": ["EQ"],
                                   "isin": ["INE0"], "open": [40.0], "high": [41.0], "low": [39.0], "close": [40.0],
                                   "prev_close": [40.0], "volume": [1.0], "turnover": [40.0], "trades": [1.0],
                                   "source": ["test"]}))
    g = st.load_fundamentals("ABC")
    assert g.series("annual", "sales").iloc[-1] == pytest.approx(161.05)
    assert g.holding("promoter").iloc[-1] == 57
    f.ratios.pop("pledge_pct")
    st.save_fundamentals(f)                # no pledge in the source: the NSE pledge table fills it
    assert st.load_fundamentals("ABC").ratios["pledge_pct"] == 12.0
    df = build_screen(settings, st)
    assert len(df) == 1 and "Promoter shares pledged" in df.loc[0, "red_flags"]
    assert df.loc[0, "price"] == 40.0


# ---------------------------------------------------------------- smart money
def test_stake_changes():
    sh = pd.DataFrame({
        "symbol": ["X"] * 4 + ["Y"] * 2,
        "quarter_end": ["2025-06-30", "2025-09-30"] * 3,
        "category": ["fii", "fii", "mf", "mf", "fii", "fii"],
        "pct": [10.0, 12.5, 5.0, 5.2, 20.0, 18.0],
    })
    out = stake_changes(sh, 0.5)
    assert list(zip(out["symbol"], out["category"])) == [("X", "fii"), ("Y", "fii")]
    assert out.loc[0, "change_pp"] == pytest.approx(2.5)
    assert out.loc[1, "change_pp"] == pytest.approx(-2.0)


def test_holder_changes_and_known_investor(settings):
    inv = settings["smart_money"]["investors"]
    h = pd.DataFrame({
        "symbol": ["X"] * 4,
        "quarter_end": ["2025-06-30", "2025-06-30", "2025-09-30", "2025-09-30"],
        "name": ["Ashish Kacholia", "Old Fund LP", "ASHISH  KACHOLIA", "New Fund"],
        "category": ["public", "fii", "public", "fii"],
        "pct": [1.2, 2.0, 2.1, 1.5],
    })
    out = holder_changes(h, inv, 0.25)
    k = out[out["known_investor"] == "Ashish Kacholia"].iloc[0]   # spacing/case differ across quarters
    assert k["event"] == "increased" and k["change_pp"] == pytest.approx(0.9)
    out = out.set_index("holder")
    assert out.loc["New Fund", "event"] == "new above 1%"
    assert out.loc["Old Fund LP", "event"] == "exited / below 1%"
    known = holder_changes(h, inv, 0.25, only_known=True)
    assert len(known) == 1


def test_deals(settings):
    inv = settings["smart_money"]["investors"]
    d = pd.DataFrame({
        "date": ["2025-11-20", "2025-11-21", "2025-11-21", "2025-01-01"],
        "symbol": ["X", "X", "Y", "Z"], "kind": ["bulk", "bulk", "block", "bulk"],
        "client": ["MUKUL MAHAVIR AGRAWAL", "SOME BROKER", "ABC FUND", "OLD"],
        "side": ["BUY", "SELL", "BUY", "BUY"], "qty": [1e6, 2e5, 1e5, 1.0], "price": [100.0, 100.0, 50.0, 1.0],
    })
    r = recent_deals(d, inv, 90, today=date(2025, 11, 28))
    assert len(r) == 3 and "Z" not in set(r["symbol"])
    assert r.loc[r["client"].str.startswith("MUKUL"), "known_investor"].iloc[0] == "Mukul Agrawal"
    s = deal_summary(r).set_index("symbol")
    assert s.loc["X", "net_value_cr"] == pytest.approx(10 - 2)
    assert s.loc["X", "known_investors"] == "Mukul Agrawal"


# ---------------------------------------------------------------- parsers
def test_screener_parser():
    from pathlib import Path
    from nse_research.fundamentals.sources.screener import parse_page, period_end, to_number
    assert period_end("Mar 2021 15m") == pd.Timestamp("2021-03-31")
    assert to_number("₹ 1,23,456 Cr.") == 123456 and to_number("-1,234") == -1234 and to_number("") is None
    html = (Path(__file__).parent / "fixtures" / "screener_sample.html").read_text()
    f = parse_page(html, "ABC", "consolidated")
    assert f.series("annual", "sales").iloc[-1] == 4350
    assert f.ttm("sales") == 4500
    assert f.series("quarterly", "net_profit").iloc[2] == -5
    assert f.series("balance", "borrowings").index[-1] == pd.Timestamp("2025-09-30")
    assert f.series("cashflow", "cfi").iloc[0] == -150
    assert f.series("ratios", "debtor_days").iloc[-1] == 95
    assert f.holding("fii").iloc[-1] == pytest.approx(23.1)
    assert f.holding("promoter").iloc[-1] == 52.5          # quarterly table, not yearly
    assert f.ratios["mcap_cr"] == 123456 and f.ratios["pe"] == 25.4 and f.ratios["price"] == 1250
    assert f.sector == "Industrials" and f.industry == "Heavy Electrical Equipment"
    assert not f.is_financial
    m = compute(f, price=1250)
    assert m["pe"] == pytest.approx(1250 / 27)    # price / TTM EPS
    assert m["roce_pct"] == 18


def test_nse_shareholding_parsers():
    from pathlib import Path
    from nse_research.smartmoney.nse import parse_shp_master, parse_shp_xbrl
    master = parse_shp_master(b'[{"date":"30-SEP-2026","pr_and_prgrp":"50.44","public_val":"49.56",'
                              b'"submissionDate":"21-OCT-2026","revisedData":"N","xbrl":"https://x/SHP_1.xml"},'
                              b'{"date":"30-JUN-2026","pr_and_prgrp":"50.48","public_val":"49.52",'
                              b'"submissionDate":"21-JUL-2026","xbrl":null}]')
    assert list(master["quarter_end"]) == [pd.Timestamp("2026-06-30"), pd.Timestamp("2026-09-30")]
    assert master["promoter_pct"].iloc[-1] == 50.44 and pd.isna(master["xbrl"].iloc[0])
    raw = (Path(__file__).parent / "fixtures" / "shp_sample.xml").read_bytes()
    t, h, extra = parse_shp_xbrl(raw)
    tot = t.set_index("category")["pct"]
    assert tot["promoter"] == pytest.approx(50.44) and tot["fii"] == pytest.approx(16.67)
    assert tot["mf"] == pytest.approx(10.31) and tot["public"] == pytest.approx(11.17)
    assert (t["quarter_end"] == pd.Timestamp("2026-09-30")).all()
    hh = h.set_index("name")
    assert hh.loc["SBI Mutual Fund - Large Cap", "category"] == "mf"
    assert hh.loc["SBI Mutual Fund - Large Cap", "pct"] == pytest.approx(1.85)
    assert hh.loc["Srichakra Commercials LLP", "category"] == "promoter"
    assert hh.loc["ASHISH KACHOLIA", "category"] == "public"
    assert extra["promoter_pledged"] is False


def test_nse_deal_and_flow_parsers():
    from nse_research.smartmoney.nse import parse_deals_api, parse_deals_csv, parse_fiidii, parse_pledge
    bulk = (b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
            b"27-NOV-2026,AGIIL,Agi Infra Limited,THAKKAR NILESHKUMAR FARSHURAM HUF,BUY,1150000,307.77,-\n")
    block = (b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
             b"27-NOV-2026,IGIL,Intl Gemological Inst Ltd,OLD BRIDGE MUTUAL FUND,BUY,2400000,417.00\n")
    b = parse_deals_csv(bulk, "bulk")
    assert b.iloc[0]["qty"] == 1150000 and b.iloc[0]["price"] == 307.77 and b.iloc[0]["side"] == "BUY"
    k = parse_deals_csv(block, "block")
    assert k.iloc[0]["date"] == pd.Timestamp("2026-11-27") and k.iloc[0]["kind"] == "block"
    api = parse_deals_api({"data": [{"BD_DT_DATE": "27-Nov-2026", "BD_SYMBOL": "X", "BD_CLIENT_NAME": "C",
                                     "BD_BUY_SELL": "SELL", "BD_QTY_TRD": "1,000", "BD_TP_WATP": "12.5"}]}, "bulk")
    assert api.iloc[0]["qty"] == 1000 and api.iloc[0]["side"] == "SELL"
    f = parse_fiidii(b'[{"buyValue":"17263.65","category":"DII","date":"27-Nov-2026","netValue":"-556.42",'
                     b'"sellValue":"17820.07"},{"buyValue":"14259.45","category":"FII/FPI","date":"27-Nov-2026",'
                     b'"netValue":"1634.33","sellValue":"12625.12"}]').set_index("category")
    assert f.loc["FII", "net_cr"] == 1634.33 and f.loc["DII", "sell_cr"] == 17820.07
    assert parse_pledge(b'{"comNameList":[],"data":[]}').empty


def test_shareholding_source_preference(tmp_path):
    st = Store(tmp_path / "s.duckdb")
    rows = [("X", f"2025-{m:02d}-28", "promoter", 50 + i * 0.1, "screener") for i, m in enumerate((3, 6, 9))]
    rows += [("X", "2025-09-28", "promoter", 51.0, "nse_xbrl"), ("X", "2025-09-28", "mf", 9.0, "nse_xbrl")]
    st.upsert_shareholding(pd.DataFrame(rows, columns=["symbol", "quarter_end", "category", "pct", "source"]))
    sh = st.shareholding_all()
    # promoter: screener has 3 quarters vs 1 from the filing, so screener is used throughout
    assert set(sh.loc[sh["category"] == "promoter", "source"]) == {"screener"}
    assert set(sh.loc[sh["category"] == "mf", "source"]) == {"nse_xbrl"}


# ---------------------------------------------------------------- pipelines (no network)
class _FakeSource:
    def __init__(self, name, ok=True):
        self.name, self.ok, self.calls = name, ok, 0

    def fetch(self, symbol):
        self.calls += 1
        if not self.ok:
            raise RuntimeError("blocked")
        f = make_fund()
        f.symbol, f.source = symbol, self.name
        return f


def test_update_fundamentals_fallback_and_freshness(settings, tmp_path):
    from nse_research.fundamentals.pipeline import update_fundamentals
    st = Store(tmp_path / "p.duckdb")
    bad, good = _FakeSource("screener", ok=False), _FakeSource("yahoo")
    rep = update_fundamentals(settings, st, ["AAA", "BBB"], sources=[bad, good])
    assert rep.fetched == {"yahoo": 2} and not rep.failed
    assert st.load_fundamentals("AAA").source == "yahoo"
    rep2 = update_fundamentals(settings, st, ["AAA", "BBB"], sources=[bad, good])
    assert rep2.skipped_fresh == 2 and good.calls == 2      # fresh: not fetched again
    rep3 = update_fundamentals(settings, st, ["CCC"], sources=[bad])
    assert rep3.failed and "blocked" in rep3.failed[0][1]


class _FakeNse:
    def __init__(self):
        from pathlib import Path
        self.xml = (Path(__file__).parent / "fixtures" / "shp_sample.xml").read_bytes()

    def shp_master(self, symbol):
        return pd.DataFrame({"quarter_end": pd.to_datetime(["2026-06-30", "2026-09-30"]),
                             "promoter_pct": [50.48, 50.44], "public_pct": [49.52, 49.56],
                             "xbrl": [None, "https://x/SHP_1.xml"]})

    def shp_xbrl(self, url, symbol, q):
        from nse_research.smartmoney.nse import parse_shp_xbrl
        return parse_shp_xbrl(self.xml)

    def pledges(self, symbol):
        return pd.DataFrame(columns=["as_of", "promoter_pct", "pledge_pct"])


def test_update_shareholding(settings, tmp_path):
    from nse_research.smartmoney.pipeline import SmartReport, update_shareholding
    st = Store(tmp_path / "q.duckdb")
    rep = SmartReport()
    update_shareholding(settings, st, _FakeNse(), ["RELIANCE"], rep)
    assert rep.shp_symbols == 1 and rep.shp_filings == 1 and not rep.errors
    sh = st.shareholding_all(["RELIANCE"])
    prom = sh[sh["category"] == "promoter"]
    assert prom["source"].unique().tolist() == ["nse_master"] and len(prom) == 2
    assert sh.loc[sh["category"] == "mf", "pct"].iloc[0] == pytest.approx(10.31)
    assert "ASHISH KACHOLIA" in set(st.holders_all()["name"])
    # the filing says no pledged promoter shares and the pledge API has no records -> 0%
    assert st.q("SELECT pledge_pct FROM pledges WHERE symbol='RELIANCE'")["pledge_pct"].iloc[0] == 0
    rep2 = SmartReport()
    update_shareholding(settings, st, _FakeNse(), ["RELIANCE"], rep2)
    assert rep2.skipped_fresh == 1


def test_yahoo_mapping_stub():
    from types import SimpleNamespace
    from nse_research.fundamentals.sources.yahoo import from_yfinance
    cols = pd.to_datetime(["2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31"])
    fin = pd.DataFrame([[4e11, 3.5e11, 3e11, 2.5e11], [4e10, 3.5e10, 3e10, 2.5e10], [60.0, 52.0, 45.0, 38.0]],
                       index=["Total Revenue", "Net Income", "Diluted EPS"], columns=cols)
    bal = pd.DataFrame([[1e10] * 4, [2e11] * 4, [5e10] * 4, [4e10] * 4],
                       index=["Common Stock", "Stockholders Equity", "Total Debt", "Accounts Receivable"], columns=cols)
    cf = pd.DataFrame([[4.5e10] * 4], index=["Operating Cash Flow"], columns=cols)
    t = SimpleNamespace(financials=fin, quarterly_financials=pd.DataFrame(), balance_sheet=bal, cashflow=cf,
                        info={"sector": "Financial Services", "industry": "Banks - Regional", "trailingPE": 20.0,
                              "currentPrice": 1200.0, "marketCap": 1e13})
    f = from_yfinance("X", t)
    assert f.series("annual", "sales").iloc[-1] == pytest.approx(40000)       # ₹ crore
    assert f.series("annual", "eps").iloc[-1] == 60.0                          # per share stays in ₹
    assert f.series("balance", "reserves").iloc[-1] == pytest.approx(19000)
    assert f.series("ratios", "debtor_days").iloc[-1] == pytest.approx(4e10 / 4e11 * 365)
    assert f.is_financial and f.ratios["mcap_cr"] == pytest.approx(1e6)
