"""
Risk management: position sizing, spread gating, and the daily-loss breaker.

All pure functions taking plain values/dicts — no MT5 imports — so the maths
that decides how much money is at stake can actually be unit-tested without a
live terminal. The connector adapts MT5 objects into these.

Three bugs this module fixes
----------------------------
1. SIZING IGNORED CONTRACT SPECS. The old calc_position_size did
   `lots = risk_amount / stop_distance`, which silently assumes one lot moves
   $1 per $1 of price. Correct conversion needs the symbol's tick value and
   tick size: value_per_price_unit = tick_value / tick_size.

2. MINIMUM LOT SIZE SILENTLY BLEW THROUGH THE RISK TARGET. At ~$78 equity and
   0.5% risk, intended risk is ~$0.39. With an ATR-based stop around $300 the
   correct size is ~0.0013 lots — far below Exness's 0.01 minimum. The old code
   did `max(lots, 0.01)`, quietly turning a 0.5% trade into roughly 4%, and with
   MAX_CONCURRENT_TRADES=2 about 8% of the account at risk at once. Now the
   floor is detected, the true effective risk is reported, and the trade is
   REFUSED when it exceeds MAX_EFFECTIVE_RISK_PCT rather than being taken
   silently oversized.

3. SPREAD WAS NEVER CHECKED. Scalping M5 BTC where the spread can be a large
   fraction of a 1.5x ATR stop, with no spread awareness anywhere. Now the
   spread is compared to ATR, and the stop is never allowed to sit so close
   that spread alone dominates the trade.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

@dataclass
class SymbolSpec:
    """The broker facts that determine what a lot is actually worth."""
    tick_size: float        # symbol_info.trade_tick_size (price increment)
    tick_value: float       # symbol_info.trade_tick_value (account ccy per tick, per lot)
    volume_min: float       # symbol_info.volume_min
    volume_max: float       # symbol_info.volume_max
    volume_step: float      # symbol_info.volume_step
    digits: int             # symbol_info.digits
    point: float            # symbol_info.point
    stops_level: int = 0    # symbol_info.trade_stops_level (in points)
    freeze_level: int = 0   # symbol_info.trade_freeze_level (in points)

    def value_per_price_unit(self) -> float:
        """Account-currency P&L per 1.0 of price movement, per 1.0 lot."""
        if self.tick_size <= 0:
            raise ValueError("tick_size must be > 0")
        return self.tick_value / self.tick_size


@dataclass
class SizingResult:
    lots: float
    intended_risk: float          # what RISK_PER_TRADE_PCT asked for
    effective_risk: float         # what this lot size actually risks
    effective_risk_pct: float     # as % of equity
    floored_by_min_lot: bool      # min lot forced us above the intended risk
    capped_by_max_lot: bool
    ok: bool                      # False => do not trade
    reason: str = ""


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    # round() then normalise float noise (0.30000000000000004 -> 0.3)
    return round(round(value / step) * step, 8)


def calc_position_size(
    equity: float,
    entry: float,
    stop: float,
    spec: SymbolSpec,
    risk_pct: float,
    max_effective_risk_pct: float,
) -> SizingResult:
    """
    Size a position from a real risk budget, honouring contract specs.

    Returns a SizingResult; when `ok` is False the caller must NOT place the
    trade — that's the guard against min-lot silently multiplying your risk.
    """
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, False, False, False,
                            "stop distance is zero")
    if equity <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, False, False, False,
                            "equity is zero or negative")

    intended_risk = equity * (risk_pct / 100.0)
    risk_per_lot = stop_distance * spec.value_per_price_unit()
    if risk_per_lot <= 0:
        return SizingResult(0.0, intended_risk, 0.0, 0.0, False, False, False,
                            "risk per lot is zero (bad tick value/size)")

    raw_lots = intended_risk / risk_per_lot
    lots = _round_to_step(raw_lots, spec.volume_step)

    floored = False
    capped = False
    if lots < spec.volume_min:
        lots = spec.volume_min
        floored = True
    if spec.volume_max and lots > spec.volume_max:
        lots = spec.volume_max
        capped = True

    effective_risk = lots * risk_per_lot
    effective_pct = (effective_risk / equity) * 100.0

    ok = True
    reason = ""
    if effective_pct > max_effective_risk_pct:
        ok = False
        reason = (
            f"min lot {spec.volume_min} forces {effective_pct:.2f}% risk "
            f"(${effective_risk:.2f}) vs target {risk_pct:.2f}% (${intended_risk:.2f}); "
            f"ceiling is {max_effective_risk_pct:.2f}%. Account too small for this "
            f"stop distance — trade refused."
        )

    return SizingResult(lots, intended_risk, effective_risk, effective_pct,
                        floored, capped, ok, reason)


# ---------------------------------------------------------------------------
# Spread gate
# ---------------------------------------------------------------------------

@dataclass
class SpreadCheck:
    ok: bool
    spread: float
    spread_atr_fraction: Optional[float]
    reason: str = ""


def check_spread(bid: float, ask: float, atr: Optional[float],
                 max_atr_fraction: float) -> SpreadCheck:
    """
    Refuse to trade when the spread is large relative to the move we're
    targeting. A $40 spread against a $150 ATR means the trade starts deep
    underwater and the edge has to overcome the broker before it overcomes
    the market.
    """
    spread = ask - bid
    if spread < 0:
        return SpreadCheck(False, spread, None, "negative spread (bad tick)")
    if not atr or atr <= 0:
        # No ATR yet (warmup): allow, since the ATR filter upstream also
        # abstains in that state rather than blocking everything.
        return SpreadCheck(True, spread, None, "no ATR available, spread check skipped")
    fraction = spread / atr
    if fraction > max_atr_fraction:
        return SpreadCheck(False, spread, fraction,
                           f"spread {spread:.2f} is {fraction:.1%} of ATR {atr:.2f} "
                           f"(max {max_atr_fraction:.0%})")
    return SpreadCheck(True, spread, fraction)


def min_stop_distance(spec: SymbolSpec, spread: float,
                      spread_multiple: float = 3.0) -> float:
    """
    Floor for the stop distance: the broker's own minimum, and enough room that
    the spread isn't a meaningful share of the stop.
    """
    broker_min = max(spec.stops_level, spec.freeze_level, 1) * spec.point
    return max(broker_min * 1.5, spread * spread_multiple)


# ---------------------------------------------------------------------------
# Daily loss breaker
# ---------------------------------------------------------------------------

@dataclass
class DailyLossState:
    halted: bool
    day_start_balance: float
    realized_today: float
    equity_now: float
    loss_pct: float
    reason: str = ""


def evaluate_daily_loss(equity_now: float, balance_now: float,
                        realized_today: float, max_daily_loss_pct: float) -> DailyLossState:
    """
    The kill switch that config.MAX_DAILY_LOSS_PCT promised but was never wired
    to anything (the constant appeared in config.py and nowhere else in the
    codebase).

    Measures from the day's STARTING BALANCE and compares against current
    EQUITY, so open floating losses count too — otherwise the bot could sail
    past the limit as long as it hadn't closed anything yet.

    Stateless by design: recomputed from broker truth every cycle, so there's
    no "halted" flag that can get stuck on across a restart.
    """
    day_start_balance = balance_now - realized_today
    if day_start_balance <= 0:
        return DailyLossState(True, day_start_balance, realized_today, equity_now, 0.0,
                              "cannot determine day-start balance")

    change_pct = (equity_now - day_start_balance) / day_start_balance * 100.0
    loss_pct = -change_pct  # positive number means we're down

    if loss_pct >= max_daily_loss_pct:
        return DailyLossState(
            True, day_start_balance, realized_today, equity_now, loss_pct,
            f"daily loss {loss_pct:.2f}% >= limit {max_daily_loss_pct:.2f}% "
            f"(day start ${day_start_balance:.2f} -> equity ${equity_now:.2f}). "
            f"No new trades until the next UTC day."
        )
    return DailyLossState(False, day_start_balance, realized_today, equity_now, loss_pct)
