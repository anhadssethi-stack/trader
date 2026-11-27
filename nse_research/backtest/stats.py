"""Statistics for backtest results. Every number here is computed from closed
trades only (trades still open today are excluded)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

CLOSED = ("target", "stop", "time", "data_end")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion k/n, in %. Better than ±1.96·SE
    for small samples and rates near 0% or 100%."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half) * 100, min(1.0, centre + half) * 100)


def confidence_label(n: int, cfg: dict) -> str:
    if n < cfg["min_trades_reliable"]:
        return "LOW: too few trades to trust"
    if n < cfg["min_trades_moderate"]:
        return "moderate"
    return "ok"


def max_drawdown_r(r: pd.Series) -> float:
    """Largest peak-to-trough fall of the cumulative R curve (trades in exit order)."""
    if r.empty:
        return 0.0
    cum = np.concatenate([[0.0], np.cumsum(r.to_numpy(float))])
    peak = np.maximum.accumulate(cum)
    return float((peak - cum).max())


def closed(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades[trades["exit_reason"].isin(CLOSED)]


def portfolio_curve(trades: pd.DataFrame, settings) -> tuple[pd.DataFrame, dict]:
    """Replay the trades as one account with your settings: start with `capital`,
    hold at most floor(100 / max_position_pct) positions at once (10 with a 10% cap),
    take signals in entry-date order and skip any that arrive when all slots are full.
    P&L (net of costs) is booked on the exit date. No compounding: sizes stay as sized.
    Returns (daily equity curve, stats)."""
    t = closed(trades).sort_values(["entry_date", "symbol"])
    cap = settings["profile"]["capital"]
    slots = max(1, int(100 // settings["profile"]["max_position_pct"]))
    if t.empty:
        return pd.DataFrame(columns=["date", "equity"]), {"max_dd_pct": 0.0, "taken": 0, "skipped": 0}
    open_exits: list = []
    taken, skipped = [], 0
    for row in t.itertuples():
        open_exits = [e for e in open_exits if e >= row.entry_date]  # still open on entry day
        if len(open_exits) >= slots:
            skipped += 1
            continue
        open_exits.append(row.exit_date)
        taken.append(row)
    tk = pd.DataFrame(taken)
    pnl = tk.groupby("exit_date")["net_pnl"].sum().sort_index()
    eq = cap + pnl.cumsum()
    curve = pd.DataFrame({"date": pd.to_datetime(eq.index), "equity": eq.values})
    vals = np.concatenate([[cap], eq.values])
    peak = np.maximum.accumulate(vals)
    dd = min(((peak - vals) / peak).max() * 100, 100.0)  # 100% = the account was wiped out
    years = max((pd.Timestamp(tk["exit_date"].max()) - pd.Timestamp(tk["entry_date"].min())).days / 365.25, 1e-9)
    total = (vals[-1] / cap - 1) * 100
    return curve, {"max_dd_pct": float(dd), "taken": len(tk), "skipped": skipped,
                   "total_return_pct": float(total), "avg_return_pct_per_year": float(total / years),
                   "slots": slots}


def summarize(trades: pd.DataFrame, settings) -> dict:
    bt_cfg = settings["backtest"]
    t = closed(trades)
    n = len(t)
    if n == 0:
        return {"trades": 0, "confidence": confidence_label(0, bt_cfg)}
    t = t.sort_values("exit_date")
    r = t["r"]
    wins = t[r > 0]
    losses = t[r <= 0]
    k = len(wins)
    lo, hi = wilson(k, n)
    gross_win, gross_loss = wins["r"].sum(), -losses["r"].sum()
    tgt = int((t["exit_reason"] == "target").sum())
    tlo, thi = wilson(tgt, n)
    dd_r = max_drawdown_r(r)
    out = {
        "trades": n,
        "win_rate": k / n * 100, "win_ci_low": lo, "win_ci_high": hi,
        "avg_win_pct": wins["ret_pct"].mean() if k else float("nan"),
        "avg_loss_pct": losses["ret_pct"].mean() if len(losses) else float("nan"),
        "avg_win_r": wins["r"].mean() if k else float("nan"),
        "avg_loss_r": losses["r"].mean() if len(losses) else float("nan"),
        "expectancy_r": r.mean(),
        "expectancy_pct": t["ret_pct"].mean(),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_dd_r": dd_r,
        "avg_bars": t["bars"].mean(),
        "target_before_stop_pct": tgt / n * 100, "tbs_ci_low": tlo, "tbs_ci_high": thi,
        "exits": t["exit_reason"].value_counts().to_dict(),
        "costs_per_trade_r": (t["costs"] / ((t["entry"] - t["stop"]).abs() * t["qty"])).mean(),
        "costs_per_trade_pct": (t["costs"] / (t["entry"] * t["qty"]) * 100).mean(),
        "first_entry": str(t["entry_date"].min()), "last_exit": str(t["exit_date"].max()),
        "share_not_in_current_universe": float((~t["in_current_universe"]).mean() * 100),
        "share_yahoo_bars": float(t["yahoo_share"].mean() * 100),
        "confidence": confidence_label(n, bt_cfg),
    }
    _, pf = portfolio_curve(t, settings)
    out.update({"max_dd_pct": pf["max_dd_pct"], "portfolio_taken": pf["taken"],
                "portfolio_skipped": pf["skipped"], "portfolio_return_pct": pf.get("total_return_pct"),
                "portfolio_return_pct_per_year": pf.get("avg_return_pct_per_year")})
    for col in [c for c in t.columns if c.startswith("hit_") and c.endswith("r")]:
        hk = int(t[col].sum())
        out[f"{col}_pct"] = hk / n * 100
        out[f"{col}_ci"] = wilson(hk, n)
    return out


def oos_split(trades: pd.DataFrame, frac: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp | None]:
    """Split by entry date: first (1-frac) of the calendar period is in-sample."""
    t = closed(trades)
    if t.empty:
        return t, t, None
    d = pd.to_datetime(t["entry_date"])
    cut = d.min() + (d.max() - d.min()) * (1 - frac)
    return t[d < cut], t[d >= cut], cut


def yearly(trades: pd.DataFrame) -> pd.DataFrame:
    t = closed(trades)
    if t.empty:
        return pd.DataFrame()
    y = pd.to_datetime(t["entry_date"]).dt.year
    g = t.groupby(y)
    return pd.DataFrame({
        "trades": g.size(),
        "win_rate": g["r"].apply(lambda r: (r > 0).mean() * 100),
        "expectancy_r": g["r"].mean(),
        "total_r": g["r"].sum(),
    }).rename_axis("year").reset_index()


def walk_forward(grid: pd.DataFrame, wf: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """For each test year Y: choose the stop/target pair with the best average R on
    trades that *entered and finished* in the previous `train_years` years, then
    record how that pair did in year Y. Nothing from year Y is used to choose it.

    Returns (per-year table, the out-of-sample trades that were taken)."""
    g = grid[grid["exit_reason"].isin(CLOSED)].copy() if not grid.empty else grid
    if g.empty:
        return pd.DataFrame(), pd.DataFrame()
    g["entry_year"] = pd.to_datetime(g["entry_date"]).dt.year
    g["exit_ts"] = pd.to_datetime(g["exit_date"])
    years = sorted(g["entry_year"].unique())
    rows, oos = [], []
    for y in years:
        train = g[(g["entry_year"] >= y - wf["train_years"]) & (g["entry_year"] < y)
                  & (g["exit_ts"] < pd.Timestamp(year=y, month=1, day=1))]
        if train.empty or train["entry_year"].min() > y - wf["train_years"]:
            continue  # need a full training window
        perf = train.groupby(["stop_atr", "target_r"])["r"].agg(["mean", "size"])
        perf = perf[perf["size"] >= wf["min_train_trades"]]
        if perf.empty:
            continue
        (sa, tr), best = perf["mean"].idxmax(), perf["mean"].max()
        test = g[(g["entry_year"] == y) & (g["stop_atr"] == sa) & (g["target_r"] == tr)]
        rows.append({"year": y, "stop_atr": sa, "target_r": tr, "train_trades": int(perf.loc[(sa, tr), "size"]),
                     "train_expectancy_r": best, "test_trades": len(test),
                     "test_win_rate": (test["r"] > 0).mean() * 100 if len(test) else float("nan"),
                     "test_expectancy_r": test["r"].mean() if len(test) else float("nan")})
        oos.append(test)
    return pd.DataFrame(rows), (pd.concat(oos) if oos else pd.DataFrame())


def setup_summary_table(trades: pd.DataFrame, grid: pd.DataFrame, settings) -> pd.DataFrame:
    """One row per setup: full-period stats, out-of-sample split and walk-forward."""
    from .setups import SETUP_BY_ID
    bt = settings["backtest"]
    rows = []
    for sid, t in trades.groupby("setup") if not trades.empty else []:
        s = summarize(t, settings)
        ins, oos, cut = oos_split(t, bt["oos_fraction"])
        s_in, s_out = summarize(ins, settings), summarize(oos, settings)
        wf_tab, wf_trades = walk_forward(grid[grid["setup"] == sid] if not grid.empty else grid, bt["walk_forward"])
        su = SETUP_BY_ID.get(sid)
        rows.append({
            "setup": sid, "name": su.name if su else sid, "side": su.side if su else "",
            **{k: v for k, v in s.items() if k not in ("exits",)},
            "exits": s.get("exits", {}),
            "is_trades": s_in.get("trades", 0), "is_expectancy_r": s_in.get("expectancy_r"),
            "is_win_rate": s_in.get("win_rate"),
            "oos_trades": s_out.get("trades", 0), "oos_expectancy_r": s_out.get("expectancy_r"),
            "oos_win_rate": s_out.get("win_rate"), "oos_from": str(cut.date()) if cut is not None else None,
            "wf_years": len(wf_tab), "wf_trades": len(wf_trades),
            "wf_expectancy_r": wf_trades["r"].mean() if len(wf_trades) else float("nan"),
            "wf_win_rate": (wf_trades["r"] > 0).mean() * 100 if len(wf_trades) else float("nan"),
        })
    return pd.DataFrame(rows)


def warnings_for(row: dict | pd.Series, bt: dict) -> list[str]:
    """Plain-English warnings about whether this setup's numbers can be trusted."""
    w = []
    n = int(row.get("trades", 0) or 0)
    if n < bt["min_trades_reliable"]:
        w.append(f"Only {n} trades: too few to trust (need at least {bt['min_trades_reliable']}).")
    elif n < bt["min_trades_moderate"]:
        w.append(f"{n} trades: treat the win rate as a rough guide (range shown).")
    ise, ose = row.get("is_expectancy_r"), row.get("oos_expectancy_r")
    if ise is not None and ose is not None and ise == ise and ose == ose and ise > 0 and ose <= 0:
        w.append("Profitable in-sample but not out-of-sample: the edge may not be real or may have faded.")
    wfe = row.get("wf_expectancy_r")
    if wfe is not None and wfe == wfe and wfe <= 0 and (row.get("expectancy_r") or 0) > 0:
        w.append("Walk-forward result is negative: choosing stop/target from the past did not work going forward.")
    if (row.get("share_yahoo_bars") or 0) > 50:
        w.append("Most price bars came from Yahoo, not NSE. Run the NSE backfill for more reliable history.")
    if (row.get("share_not_in_current_universe") or 0) < 1:
        w.append("Almost every trade is in a stock that is in today's list, so delisted and dropped stocks are "
                 "probably missing (survivorship bias): real results are likely worse.")
    if row.get("side") == "short":
        w.append("Shorts use today's F&O list and spot prices; futures basis and rollover costs are not included.")
    return w


def probability_sentence(row: dict | pd.Series, target_r: float, bt: dict) -> str:
    """The only form in which a historical rate is shown for a trade."""
    n = int(row.get("trades", 0) or 0)
    if n == 0:
        return "No historical trades for this setup."
    lo, hi = row.get("tbs_ci_low"), row.get("tbs_ci_high")
    s = (f"This setup historically hit its {target_r:g}R target before its stop "
         f"{row['target_before_stop_pct']:.0f}% of the time over {n} trades "
         f"(95% range {lo:.0f}–{hi:.0f}%).")
    if n < bt["min_trades_reliable"]:
        s += " LOW CONFIDENCE: fewer than 30 trades."
    return s
