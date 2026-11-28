# NSE Research: personal stock research and trade planning

> **Decision-support tool only. Not investment advice. The author is not a SEBI-registered
> investment adviser or research analyst.** Signals describe what the data shows; they do not
> predict outcomes. Data comes from free, unofficial sources and can be late, wrong or missing.

Built in phases, and each phase is confirmed before the next one starts:

| Phase | Scope | Status |
|---|---|---|
| 1 | Data layer, technical scanner on Nifty 500, dashboard | done |
| 2 | Backtest engine and win-rate statistics | **this release** |
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

## 4b. Backtest (Phase 2)

Run it after `daily` (or `backfill-fast`) has stored history. If the dashboard is running, press
**Ctrl+C** in its terminal first (or use a second terminal), because commands typed into the
dashboard's terminal don't run:

```bash
cd ~/trader && source .venv/bin/activate
git fetch && git checkout phase2-backtest && git pull
pip install -r requirements.txt
```

Then:

```bash
python -m nse_research.cli backtest                       # all setups, all stored history (a few minutes)
python -m nse_research.cli backtest --setups L_golden_cross S_death_cross
python -m nse_research.cli backtest --symbols RELIANCE TCS   # quick check
```

It prints a summary table and saves the run (the last 5 runs are kept). Open the **Backtest**
page in the dashboard for the detail of each setup. The Scanner's reasons panel also shows
which backtested setups fired on today's bar, with their historical record.

### Setups tested

| id | Rule |
|---|---|
| L_breakout_long | Close above the prior 55-day high, on volume ≥ 1.5× its 20-day average |
| L_breakout_short | Close above the prior 20-day high, on volume ≥ 1.5× its 20-day average |
| L_breakout_rs | 20-day breakout on volume, and beating the Nifty 500 over 63 and 126 days |
| L_trend_start | Close > 20 > 50 > 200 DMA for the first time |
| L_golden_cross | 50 DMA crosses above the 200 DMA |
| L_macd_uptrend | MACD crosses above its signal line, close above the 200 DMA |
| L_rsi_50_uptrend | RSI crosses above 50, close above the 200 DMA |
| L_volume_up | Volume ≥ 2× average on an up day, close above the 50 DMA |
| L_delivery_up | Delivery ≥ 50% and ≥ 1.2× its average on an up day, close above the 50 DMA |
| S_breakdown_long / S_breakdown_short | Mirror of the breakouts (55 / 20-day low), shorts |
| S_trend_start | Close < 20 < 50 < 200 DMA for the first time, short |
| S_death_cross | 50 DMA crosses below the 200 DMA, short |
| S_macd_downtrend | MACD crosses below its signal line, close below the 200 DMA, short |
| S_volume_down | Volume ≥ 2× average on a down day, close below the 50 DMA, short |

Shorts are only taken on F&O stocks (multi-day shorts need futures). `L_delivery_up` needs
delivery % history, which only NSE files have, so it shows 0 trades until you run the NSE backfill.

**Which stocks are tested** (`backtest.universe`):

- `top_turnover` (default): on each signal day, only the `top_n` (500) most-traded stocks *that
  day* by 20-day average turnover. This is a point-in-time stand-in for "the Nifty 500 back
  then": it includes stocks that later fell out or were delisted (no survivorship bias), but
  not the thousands of small stocks the full NSE backfill also stores. The run log says how
  many stocks were ever in the top 500 (typically 1,000+ over ten years).
- `all_stored`: every stored stock that passes the liquidity filter (₹5 cr/day on the signal day).
- `current`: today's universe only (has survivorship bias).

ETFs and other fund units (ISIN starting `INF`, e.g. GOLDBEES, LIQUIDETF, NIFTYBEES) are never
backtested: they aren't company shares, and a liquid fund's tiny daily range would put the stop
inside the costs. For the same reason any stock whose ATR is below 0.5% of its price
(`min_atr_pct`) is skipped on that day.

**Splits NSE didn't flag.** Normally a split or bonus is detected because NSE's file carries an
adjusted previous close on the ex-date. Some older files (and ETF unit splits) don't, so the
stock appears to crash 50–99% overnight. Since cash-segment stocks can move at most 20% a day,
an overnight gap of more than 40% that NSE didn't flag is treated as a corporate action and
adjusted (`data.gap_action_min`). The daily update does this automatically for new days; for
history already stored, run once:

```bash
python -m nse_research.cli repair-splits
```

Run `python -m nse_research.cli check-data` after a backfill. It lists one-day moves above 25% in
split-adjusted prices (usually a split or bonus that wasn't detected) and the backtest trades
that lost more than 2R, so a data problem can't hide inside the results. To add a setup, add a
function and a `Setup(...)` entry in `nse_research/backtest/setups.py`.

### How each trade is simulated

All numbers below are in `config/settings.yaml` under `backtest:` and `costs:`.

- **Signal** on the close of day *t*. **Entry** at the open of day *t+1*, plus slippage
  (10 bps each side).
- **Stop** = entry ∓ 2 × ATR(14) of the signal day. **Target** = entry ± 2R, where R is the
  distance to the stop.
- Each day: if the open gaps through the stop or target, the trade fills **at the open**
  (so gaps can make a loss bigger than 1R). If the same bar touches both stop and target,
  the **stop is assumed first** (conservative). Otherwise the stop or target price fills.
- **Time exit** at the close after 60 trading days. If the stock stops trading (delisted,
  suspended) the trade closes at its last close (`data_end`). Trades still running today are
  shown as `open` and left out of the statistics.
- **Size** from your profile: risk 2% of capital per trade, capped at 10% of capital per stock.
- **Costs** per trade: brokerage (0.03%, max ₹20 per order), STT, exchange charges, SEBI fee,
  stamp duty, GST and DP charges. Longs use cash-delivery rates; shorts use stock-futures rates.
- **Liquidity** is checked as of the signal day (average turnover ≥ ₹5 cr, price ≥ ₹20), not
  today, so a stock that was illiquid back then is excluded back then.
- One open trade per stock per setup at a time.

### Statistics shown

- **Win rate** = net-profitable trades / closed trades, with a 95% **Wilson** confidence range.
- **Expectancy** in R and in %, average win and loss, **profit factor** (gross wins / gross losses).
- **"Hit nR before stop"**: "this setup historically hit 2R before its stop X% of the time
  over N trades", for 1R, 2R and 3R. It is a historical frequency, never "X% chance".
- **Confidence label**: fewer than 30 trades = **LOW** (treat as anecdote), 30–99 = moderate,
  100+ = higher. Small samples are flagged everywhere they appear.
- **Max drawdown**: trades are replayed through a ₹50 lakh account with 10 position slots
  (signals that arrive while all slots are full are skipped), and the peak-to-trough fall of
  that equity curve is reported. If losses use up all the capital, the replay stops there and the
  drawdown shows 100%.
- **Out-of-sample**: the last 30% of trades by entry date are reported separately. A setup
  whose edge disappears out-of-sample is probably curve-fit.
- **Walk-forward**: for each year, the stop/target pair (from a 3 × 3 grid of 1.5/2/3 ATR ×
  1.5/2/3 R) that worked best over the previous 3 years is picked, then tested on that year
  only. Only trades that had closed before the year began are used for picking.
  Out-of-sample and walk-forward cover **different years** (walk-forward starts once 3 years
  of history exist, out-of-sample is only the last 30%), so they can disagree. The column
  `wf_fixed_expectancy_r` ("Same years, fixed stop/target") shows the default 2 ATR / 2R over the
  walk-forward years: if walk-forward is much better than that, the gain came from re-picking
  the stop and target (usually the wider 3 ATR stop); if they are close, those years were simply better.
- Results by year, by exit reason, and the full trade list (CSV download).

### Caveats you should read

- **Survivorship bias.** `backfill-fast` only fetches today's Nifty 500 from Yahoo, so stocks
  that fell out or were delisted are missing and results look better than reality. Running
  `python -m nse_research.cli update --since 2016-01-01` (slow, resumable) stores every NSE
  stock for every day, including later-delisted ones; the backtest then uses all of them. The
  Backtest page shows what share of trades came from Yahoo data and from stocks no longer in
  the universe.
- **Historical index membership** isn't available free, so the universe is "every stock that
  was liquid at the time", not "the Nifty 500 at the time".
- **Shorts** assume you could hold a futures position through rollovers at no extra cost;
  in reality, rollover spreads, lot sizes and ban periods add friction.
- Past frequencies don't guarantee future ones, especially across market regimes.

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
  backtest/setups.py          the backtested setups (entry rules)
  backtest/engine.py          trade simulator, sizing, costs applied per trade
  backtest/costs.py           Indian brokerage, STT, exchange, SEBI, stamp, GST, DP
  backtest/stats.py           win rate + CI, expectancy, drawdown, out-of-sample, walk-forward
app/streamlit_app.py          dashboard: Scanner, Stock chart, Backtest, Data health, Settings
scripts/                      daily run + macOS launchd installer
tests/                        pytest suite
```

The database is `data/nse_research.duckdb`, and downloaded files are cached in `data/cache/`.
Neither is committed to git. The app stores **every** NSE equity each day, not just the Nifty 500,
so switching to `universe.mode: all_nse` needs no new downloads, and stocks that later get
delisted stay in the history (useful against survivorship bias in Phase 2).

## 6. Known limitations

- **Symbol renames** (e.g. after a merger or name change) split one company's history into two
  symbols. ISIN-based stitching is still to do.
- **Historical index membership** isn't available for free (see the backtest caveats above).
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

## 8. Checking that Phase 2 works

1. `python -m pytest -q` passes (61 tests, 25 of them for the backtester).
2. `python -m nse_research.cli backtest --symbols RELIANCE TCS INFY` finishes in seconds and
   prints a table.
3. A full `python -m nse_research.cli backtest` finishes in a few minutes and the **Backtest**
   page shows every setup, with trades from about 10 years.
4. Pick one trade from the trade list and check it on your broker's chart: entry at the next
   day's open, and an exit at the stop, target or after 60 days.
