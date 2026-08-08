"""
Tests for connector/risk.py and connector/broker_time.py.

Run from repo root:  python3 -m connector.test_risk

These cover the maths that decides how much money is at stake, using the actual
account numbers from this project (~$78 equity, BTCUSDm, 0.01 minimum lot).
"""
from connector.risk import (
    SymbolSpec, calc_position_size, check_spread, min_stop_distance, evaluate_daily_loss,
)
from connector import broker_time

passed = 0
def check(name, cond):
    global passed
    assert cond, f"FAIL: {name}"
    passed += 1
    print(f"  ok  {name}")


# Exness-style BTCUSDm: 1 lot = 1 BTC, $0.01 per $0.01 move => $1 per $1 per lot.
BTC = SymbolSpec(tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=100.0,
                 volume_step=0.01, digits=2, point=0.01, stops_level=0, freeze_level=0)


# ---------------------------------------------------------------------------
print("A. position sizing — contract conversion")

check("value per price unit = tick_value/tick_size", BTC.value_per_price_unit() == 1.0)

# A symbol where 1 lot = 0.1 BTC would have half the tick value: sizing must
# double the lots for the same risk. This is the conversion the old code lacked.
HALF = SymbolSpec(tick_size=0.01, tick_value=0.005, volume_min=0.01, volume_max=100.0,
                  volume_step=0.01, digits=2, point=0.01)
# $50 risk over a $250 stop => 0.20 lots on BTC, exactly on the 0.01 step.
big = calc_position_size(10000, 100000, 99750, BTC, 0.5, 5.0)
half = calc_position_size(10000, 100000, 99750, HALF, 0.5, 5.0)
check("BTC spec sizes to 0.20 lots", abs(big.lots - 0.20) < 1e-9)
check("halved tick value doubles the lot size", abs(half.lots - 0.40) < 1e-9)
check("both risk the same money despite different lots",
      abs(big.effective_risk - half.effective_risk) < 1e-6)


# ---------------------------------------------------------------------------
print("B. the real account: min lot blows through the risk target")

# $78 equity, 0.5% target = $0.39. ATR-based stop ~$300 => correct size 0.0013
# lots, far under the 0.01 minimum. Minimum lot risks ~$3 = ~3.8%.
r = calc_position_size(equity=78.0, entry=115000.0, stop=114700.0, spec=BTC,
                       risk_pct=0.5, max_effective_risk_pct=1.5)
check("intended risk is $0.39", abs(r.intended_risk - 0.39) < 0.01)
check("min lot floor was hit", r.floored_by_min_lot)
check("lots landed on the 0.01 minimum", r.lots == 0.01)
check("effective risk is ~$3", abs(r.effective_risk - 3.0) < 0.01)
check("effective risk pct is ~3.8%", 3.7 < r.effective_risk_pct < 3.9)
check("TRADE REFUSED: 3.8% exceeds the 1.5% ceiling", not r.ok)
check("refusal explains itself", "min lot" in r.reason and "refused" in r.reason.lower())

# Same setup on a properly-sized account: no floor, risk on target, allowed.
r2 = calc_position_size(equity=5000.0, entry=115000.0, stop=114700.0, spec=BTC,
                        risk_pct=0.5, max_effective_risk_pct=1.5)
check("healthy account is not floored", not r2.floored_by_min_lot)
check("healthy account risk ~0.5%", abs(r2.effective_risk_pct - 0.5) < 0.15)
check("healthy account trade allowed", r2.ok)

# Tighter stop makes min lot affordable even on the small account.
r3 = calc_position_size(equity=78.0, entry=115000.0, stop=114990.0, spec=BTC,
                        risk_pct=0.5, max_effective_risk_pct=1.5)
check("tight stop keeps small account under the ceiling", r3.ok)
check("tight stop needs no min-lot floor at all", not r3.floored_by_min_lot)
check("tight stop risk lands on the 0.5% target", abs(r3.effective_risk_pct - 0.5) < 0.05)

# Guards
check("zero stop distance refused", not calc_position_size(78, 115000, 115000, BTC, 0.5, 1.5).ok)
check("zero equity refused", not calc_position_size(0, 115000, 114700, BTC, 0.5, 1.5).ok)

# Volume step rounding must land on a legal multiple.
STEP = SymbolSpec(tick_size=0.01, tick_value=0.01, volume_min=0.1, volume_max=50.0,
                  volume_step=0.1, digits=2, point=0.01)
rs = calc_position_size(100000, 115000, 114700, STEP, 0.5, 5.0)
check("lots align to volume_step", abs(rs.lots / 0.1 - round(rs.lots / 0.1)) < 1e-6)
check("lots never exceed volume_max", rs.lots <= STEP.volume_max)


# ---------------------------------------------------------------------------
print("C. spread gate")

ok = check_spread(bid=114990, ask=115000, atr=300.0, max_atr_fraction=0.15)
check("$10 spread vs $300 ATR passes", ok.ok and abs(ok.spread - 10) < 1e-9)
bad = check_spread(bid=114940, ask=115000, atr=300.0, max_atr_fraction=0.15)
check("$60 spread vs $300 ATR (20%) is rejected", not bad.ok)
check("rejection names the numbers", "ATR" in bad.reason)
check("no ATR yet => allowed (warmup)", check_spread(114990, 115000, None, 0.15).ok)
check("negative spread rejected", not check_spread(115000, 114990, 300.0, 0.15).ok)

# Stop floor must keep spread a small share of risk.
spec_stops = SymbolSpec(tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=100,
                        volume_step=0.01, digits=2, point=0.01, stops_level=500)
floor = min_stop_distance(spec_stops, spread=20.0, spread_multiple=3.0)
check("stop floor respects broker stops_level", floor >= 500 * 0.01 * 1.5)
check("stop floor respects 3x spread", floor >= 60.0)


# ---------------------------------------------------------------------------
print("D. daily loss breaker")

# Started the day at $100 (balance 90 after realized -10), equity now 96 => -4%.
d = evaluate_daily_loss(equity_now=96.0, balance_now=90.0, realized_today=-10.0,
                        max_daily_loss_pct=3.0)
check("day start balance reconstructed as $100", abs(d.day_start_balance - 100.0) < 1e-9)
check("loss of 4% trips the 3% limit", d.halted)
check("loss pct reported positive when down", abs(d.loss_pct - 4.0) < 1e-9)

d2 = evaluate_daily_loss(equity_now=98.0, balance_now=98.0, realized_today=-2.0,
                         max_daily_loss_pct=3.0)
check("2% loss does not halt", not d2.halted)

# Floating losses must count — nothing realized yet, but equity is down 5%.
d3 = evaluate_daily_loss(equity_now=95.0, balance_now=100.0, realized_today=0.0,
                         max_daily_loss_pct=3.0)
check("unrealized drawdown alone trips the breaker", d3.halted)

d4 = evaluate_daily_loss(equity_now=110.0, balance_now=105.0, realized_today=5.0,
                         max_daily_loss_pct=3.0)
check("profitable day never halts", not d4.halted and d4.loss_pct < 0)
check("exactly at the limit halts", evaluate_daily_loss(97.0, 100.0, 0.0, 3.0).halted)


# ---------------------------------------------------------------------------
print("E. broker time")

UTC_NOW = 1_754_600_000.0
check("UTC+3 server detected", broker_time.detect_offset_hours(UTC_NOW + 3*3600, UTC_NOW) == 3.0)
check("UTC+2 server detected", broker_time.detect_offset_hours(UTC_NOW + 2*3600, UTC_NOW) == 2.0)
check("UTC+0 server detected", broker_time.detect_offset_hours(UTC_NOW, UTC_NOW) == 0.0)
check("half-hour offset supported", broker_time.detect_offset_hours(UTC_NOW + 5.5*3600, UTC_NOW) == 5.5)
check("poll jitter of a few seconds doesn't skew offset",
      broker_time.detect_offset_hours(UTC_NOW + 3*3600 + 7, UTC_NOW) == 3.0)
check("negative offset (UTC-5) supported",
      broker_time.detect_offset_hours(UTC_NOW - 5*3600, UTC_NOW) == -5.0)

check("to_utc_epoch removes the offset",
      broker_time.to_utc_epoch(UTC_NOW + 3*3600, 3.0) == UTC_NOW)
check("normalise handles plain lists",
      broker_time.normalise_candle_times([UTC_NOW + 3*3600], 3.0) == [UTC_NOW])
check("bar_seconds M5", broker_time.bar_seconds("M5") == 300)
check("bar_seconds M15", broker_time.bar_seconds("M15") == 900)

# The bug in practice: a 07:00 UTC+3 candle is really 04:00 UTC. Treating it as
# UTC put it inside the London window (07-16) when it was actually pre-session.
import datetime as _dt
server_7am = _dt.datetime(2026, 8, 7, 7, 0, tzinfo=_dt.timezone.utc).timestamp()
true_utc = broker_time.to_utc_epoch(server_7am, 3.0)
check("server 07:00 is really 04:00 UTC",
      _dt.datetime.fromtimestamp(true_utc, tz=_dt.timezone.utc).hour == 4)

from strategy.sessions import in_session
check("uncorrected time wrongly reads as in-London",
      in_session("london", _dt.datetime.fromtimestamp(server_7am, tz=_dt.timezone.utc)))
check("corrected time correctly reads as outside London",
      not in_session("london", _dt.datetime.fromtimestamp(true_utc, tz=_dt.timezone.utc)))

print(f"\nALL PASSED ({passed} assertions)")
