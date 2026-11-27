# NSE Research: personal stock research and trade planning

> **Decision-support tool only. Not investment advice. The author is not a SEBI-registered
> investment adviser or research analyst.** Signals describe what the data shows; they do not
> predict outcomes. Data comes from free, unofficial sources and can be late, wrong or missing.

Built in phases, and each phase is confirmed before the next one starts:

| Phase | Scope | Status |
|---|---|---|
| 1 | Data layer, technical scanner on Nifty 500, dashboard | **this release** |
| 2 | Backtest engine and win-rate statistics | not started |
| 3 | Fundamentals and smart-money tracker | not started |
| 4 | Sector rotation, trade plans, position sizing | not started |
| 5 | Automation, alerts, forward scenarios | not started |

---

## 1. Setup on a Mac

You need Python 3.11 or newer. Check with `python3 --version`. If it's older, install it with
`brew install python@3.12`, or use the installer from python.org.

```bash
cd ~/path/to/nse-research
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                 # should end with "34 passed"
```

### Try it with demo data first (optional, 10 seconds)

```bash
python -m nse_research.cli demo
NSE_RESEARCH_DB=data/demo.duckdb streamlit run app/streamlit_app.py
```

All demo data is random, and every page shows a red **DEMO DATA** banner.

### Load real data

Run these one at a time and let each finish before starting the next. `backfill-fast` takes several
minutes, and the dashboard keeps running until you press Ctrl+C.

```bash
python -m nse_research.cli init            # Nifty 500 list + F&O list from NSE
python -m nse_research.cli backfill-fast   # 10 years of prices from Yahoo (~5-10 min, no delivery %)
python -m nse_research.cli daily           # NSE bhavcopy + delivery for recent days, then scan
streamlit run app/streamlit_app.py         # then open http://localhost:8501 in your browser
```

`backfill-fast` gets you working in minutes. To replace the Yahoo history with NSE's own files
(more accurate, and includes delivery %), run this overnight. It's slow because NSE has to be
fetched politely, about 1.5 to 2 hours for 10 years. It's safe to stop and restart, since every
day is saved as soon as it's fetched and downloaded files are cached:

```bash
python -m nse_research.cli update --since 2016-01-01
```

### Daily run after market close

```bash
./scripts/install_mac_schedule.sh
```

This installs a launchd job that runs `daily` at **19:45, Monday to Friday**, by your Mac's
clock. If your Mac isn't set to India time, edit `HOUR`/`MINUTE` in the script first. If the Mac
is asleep at 19:45, the job runs when it wakes up. Logs are in `data/logs/daily.log`. To test it
now: `launchctl start com.nseresearch.daily`.

Other commands: `python -m nse_research.cli health` shows what was fetched, from where, and
what failed. `scan` re-runs the scanner without downloading anything.

---

## 2. Settings

Every number lives in `config/settings.yaml`. To change one without editing the tracked file,
create `config/settings.local.yaml` with just the keys you want to override, e.g.

```yaml
profile:
  risk_per_trade_pct: 1.5
universe:
  mode: all_nse          # scan every NSE equity instead of Nifty 500
```

Current profile: capital ₹50,00,000, risk 2% per trade (₹1,00,000), at most 10% of capital
(₹5,00,000) in one stock, holding period 5 to 180 days. SELL means a real short. Because
multi-day shorts are only possible through F&O, bearish setups are marked as shortable only for
F&O stocks (`short_requires_fno: true`). Position sizing itself arrives in Phase 4.

---

## 3. Data sources: limits and failure modes

| Source | Used for | Limits and failure modes | How the app copes |
|---|---|---|---|
| **NSE CM bhavcopy** (UDiFF from 8 Jul 2024; old `cmDDMONYYYYbhav` before) | Daily OHLCV, prev close, ISIN for every NSE equity | No official API for scripts. NSE blocks clients without browser headers, often blocks cloud/VPN IPs, and sometimes returns an HTML page with status 200. Published around 6 to 7 PM IST. No file on holidays. The format changed in July 2024 and may change again. Prices are **raw** (not adjusted for splits/bonuses). | Browser headers and cookies, retries with back-off, polite pacing. HTML responses are treated as "not found". Each format has its own parser, and a parser error moves on to the next source. Raw files are cached before parsing. |
| **NSE full bhavcopy** (`sec_bhavdata_full`) | Delivery qty and %, plus a backup copy of prices | Same access issues. Sometimes published later than the bhavcopy. Delivery is "-" for some series. | Primary delivery source, and the second price source. If it's missing, delivery shows as n/a. It's never guessed. |
| **NSE MTO files** | Delivery % (backup, long history) | Same access issues, and a fixed-width-ish text format | Fallback delivery source |
| **NSE index close file** (`ind_close_all`) | Nifty 500 benchmark | Same access issues | Falls back to Yahoo `^CRSLDX` |
| **Nifty 500 / F&O lists** | Universe, shortable stocks | Only the **current** list is public, so past index membership isn't available (a survivorship issue for Phase 2). The list changes twice a year. | Refreshed weekly. If a refresh fails, the stored list is kept. |
| **yfinance (Yahoo, `.NS`)** | Fast history, fallback prices | Unofficial and rate-limited, and breaks when Yahoo changes. No delivery data. Its `Close` is split-adjusted *as of today*. It sometimes misses or misdates Indian bonus issues. Delisted stocks disappear. Volume can differ from NSE's. | Prices are converted back to raw using Yahoo's split list, so they match NSE. Rows are tagged `source=yahoo`, and NSE always replaces them when it becomes available. The daily run re-tries NSE for Yahoo-filled days from the last 14 days. |

Coming in later phases: NSE/BSE corporate filings, shareholding patterns (quarterly, up to
about 21 days after quarter end), bulk and block deals (next day, large trades only), and FII/DII
flows (daily market-wide totals, not per stock).

### How fallback works

For each market day the app tries `price_sources` in order (`nse_udiff`, then `nse_legacy`, then
`nse_full_bhav`, then `yahoo`). It stores the first one that works and logs every attempt. A
weekday where *no* source has a file, and that is more than a day old, is recorded as a holiday
and not asked for again. Nothing in one source can stop the run: errors are logged and shown
on the **Data health** page.

### Split and bonus adjustment

Raw prices are stored exactly as published. On an ex-date, NSE publishes the *adjusted*
previous close, so `factor = prev_close(ex-date) / close(previous day)` recovers the split or
bonus ratio without a separate corporate-actions feed. These factors are applied when data is
read, and the chart page lists the ones it used. Ordinary dividends are not adjusted, which
matches how NSE reports prices.

---

## 4. Scanner rules (Phase 1)

Each rule fires or doesn't, and shows the exact numbers behind it. There is **no combined
score**. The "# Bull / # Bear" columns only count how many rules fired. All thresholds are in
`settings.yaml → scanner`.

| Rule | Fires when | Side |
|---|---|---|
| Uptrend / Downtrend | close > 20 > 50 > 200 DMA (or the reverse) | bull / bear |
| Golden / Death cross | 50 DMA crossed the 200 DMA in the last 10 days | bull / bear |
| RSI bullish / bearish zone | RSI(14) in 50 to 70 and rising / in 30 to 50 and falling | bull / bear |
| RSI overbought / oversold | RSI ≥ 70 / ≤ 30 | info |
| MACD cross | MACD(12,26,9) crossed its signal line in the last 3 days | bull / bear |
| N-day breakout / breakdown | close beyond the prior 20- or 55-day high/low on ≥ 1.5× average volume | bull / bear |
| Volume spike | volume ≥ 2× the prior 20-day average (side follows the day's move) | bull / bear |
| High delivery | delivery ≥ 50% and ≥ 1.2× its 20-day average (side follows the day's move) | bull / bear |
| Outperforming / Underperforming | return minus Nifty 500 return is positive (negative) over both 63 and 126 days | bull / bear |
| Near support / resistance | within 1 ATR of a swing-pivot level | info |

**Support and resistance:** swing highs and lows (5 bars each side) over the last 250 days.
Pivots within 0.5 ATR of each other merge into one level, and each level shows how many times
price touched it. **ATR** is Wilder's 14-day ATR, also shown as a % of price. Stocks with less
than 210 days of history, or under ₹1 crore average daily turnover, are skipped, and the count
of skipped stocks is shown.

To add a rule, write a function in `nse_research/scanner/rules.py` that returns a `Signal`
with a reason, and add it to `RULES`.

---

## 5. Project layout

```
config/settings.yaml          all settings
nse_research/
  settings.py                 loader (+ settings.local.yaml override)
  data/http.py                HTTP client: retries, pacing, raw-file cache
  data/sources/nse.py         NSE parsers (bhavcopy x2, full bhavcopy, MTO, indices, lists)
  data/sources/yahoo.py       yfinance fallback (+ split un-adjustment)
  data/store.py               DuckDB tables and queries
  data/adjust.py              split/bonus detection and adjustment
  data/pipeline.py            fetch orchestration and fallback
  indicators/core.py          DMA, RSI, MACD, ATR, volume, delivery, relative strength
  indicators/levels.py        swing-pivot support/resistance
  scanner/rules.py            the rules above, each with its reason text
  scanner/engine.py           runs rules over the universe
  cli.py                      command line
  demo.py                     synthetic demo database
app/streamlit_app.py          dashboard: Scanner, Stock chart, Data health, Settings
scripts/                      daily run + macOS launchd installer
tests/                        pytest suite
```

The database is `data/nse_research.duckdb`, and downloaded files are cached in `data/cache/`.
Neither is committed to git. The app stores **every** NSE equity each day, not just the Nifty 500,
so switching to `universe.mode: all_nse` needs no new downloads, and stocks that later get
delisted stay in the history (useful against survivorship bias in Phase 2).

## 6. Known limitations (Phase 1)

- **Symbol renames** (e.g. after a merger or name change) split one company's history into two
  symbols. Phase 2 will add ISIN-based stitching.
- **Historical index membership** isn't available for free. Backtests on "today's Nifty 500"
  overstate results, and Phase 2 will measure and flag this.
- The NSE parsers were written against NSE's published layouts and tested on sample files.
  The first real download is the real test: if NSE has changed something, the Data health
  page will show which file failed, and Yahoo fills in meanwhile.
- End-of-day only. No intraday data.

## 7. Checking that Phase 1 works

1. `python -m pytest -q` passes.
2. `init` reports about 500 universe symbols and about 200 F&O symbols.
3. After `backfill-fast` and `daily`, the **Data health** page shows recent days sourced from
   `nse_udiff` with delivery from `nse_full_bhav`, and the benchmark covering about 10 years.
4. The **Scanner** lists stocks with reasons, and the **Stock chart** for a stock you know
   matches your broker's chart (allowing for split adjustment).
