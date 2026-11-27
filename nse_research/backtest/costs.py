"""Indian transaction costs. All rates come from settings.yaml (`costs:`), because
they change with budgets and differ by broker. Check them against a contract note.

Long trades are modelled as delivery (cash) trades. Short trades are modelled as
stock futures, since a multi-day short is only possible through F&O; futures
basis and rollover costs are not modelled (prices are spot).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp


def _brokerage(value: float, c: dict) -> float:
    b = value * c["brokerage_pct"] / 100
    cap = c.get("brokerage_cap")
    return min(b, cap) if cap is not None else b


def trade_costs(buy_value: float, sell_value: float, segment: str, costs_cfg: dict) -> CostBreakdown:
    """Costs for one round trip. buy_value/sell_value in rupees.
    segment: "delivery" (long) or "futures" (short)."""
    c = costs_cfg[segment]
    brokerage = _brokerage(buy_value, c) + _brokerage(sell_value, c)
    stt = buy_value * c["stt_buy_pct"] / 100 + sell_value * c["stt_sell_pct"] / 100
    exchange = (buy_value + sell_value) * c["exchange_pct"] / 100
    sebi = (buy_value + sell_value) * c["sebi_per_crore"] / 1e7
    stamp = buy_value * c["stamp_buy_pct"] / 100
    gst = (brokerage + exchange + sebi) * c["gst_pct"] / 100
    dp = c.get("dp_per_sell", 0.0)
    return CostBreakdown(brokerage, stt, exchange, sebi, stamp, gst, dp)
