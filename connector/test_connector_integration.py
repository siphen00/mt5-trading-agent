"""
Connector integration tests against a mocked MetaTrader5 module.

Run from repo root:  python3 -m connector.test_connector_integration

Verifies the behaviour that can't be checked in pure functions:
  1. get_candles drops the currently-forming bar and normalises times to UTC.
  2. The new-bar gate stops the same closed bar being traded on every 15s poll.
  3. The daily-loss breaker blocks new entries.
  4. place_trade refuses a trade when min lot would exceed the risk ceiling,
     and rejects entries when the spread is too wide.
"""
import sys
import types
from datetime import datetime, timezone

import numpy as np
import pandas as pd

passed = 0
def check(name, cond):
    global passed
    assert cond, f"FAIL: {name}"
    passed += 1
    print(f"  ok  {name}")


# ---------------------------------------------------------------------------
# Mock MetaTrader5
# ---------------------------------------------------------------------------
BROKER_OFFSET_H = 3.0   # pretend the broker runs UTC+3
BAR = 300               # M5

class _Info:
    visible = True
    digits = 2
    point = 0.01
    trade_tick_size = 0.01
    trade_tick_value = 0.01
    volume_min = 0.01
    volume_max = 100.0
    volume_step = 0.01
    trade_stops_level = 0
    trade_freeze_level = 0

class _Tick:
    def __init__(self, bid, ask, t):
        self.bid, self.ask, self.time = bid, ask, t

class _Acct:
    def __init__(self, equity, balance):
        self.equity, self.balance = equity, balance

class _Result:
    def __init__(self, retcode, order):
        self.retcode, self.order, self.comment = retcode, order, "ok"


def make_mock(equity=5000.0, balance=5000.0, spread=10.0, deals=(), n_bars=120,
              last_closed_utc=None):
    """Build a fake MT5 module. Candle times are returned in BROKER time."""
    m = types.ModuleType("MetaTrader5")
    m.TIMEFRAME_M5, m.TIMEFRAME_M15 = 5, 15
    m.TRADE_ACTION_DEAL = 1
    m.ORDER_TYPE_BUY, m.ORDER_TYPE_SELL = 0, 1
    m.ORDER_TIME_GTC, m.ORDER_FILLING_IOC = 0, 1
    m.TRADE_RETCODE_DONE = 10009
    m.POSITION_TYPE_BUY = 0

    # last_closed_utc = UTC time of the newest CLOSED bar.
    if last_closed_utc is None:
        last_closed_utc = 1_754_600_000 - (1_754_600_000 % BAR)
    m._last_closed_utc = last_closed_utc
    m._orders = []

    def copy_rates_from_pos(symbol, tf, start, count):
        # Position 0 is the forming bar: one bar AFTER the last closed one.
        # Read from the module attribute (not the closure local) so tests can
        # advance the clock mid-run.
        forming_utc = m._last_closed_utc + BAR
        times_utc = [forming_utc - i * BAR for i in range(count)][::-1]
        # MT5 hands these back on the BROKER clock.
        times_broker = [t + BROKER_OFFSET_H * 3600 for t in times_utc]
        rows = []
        for i, t in enumerate(times_broker):
            base = 115000 + (i % 5) * 20
            rows.append((t, base, base + 60, base - 60, base + 10, 500, 2, 500))
        return np.array(rows, dtype=[("time", "f8"), ("open", "f8"), ("high", "f8"),
                                     ("low", "f8"), ("close", "f8"),
                                     ("tick_volume", "f8"), ("spread", "f8"),
                                     ("real_volume", "f8")])

    m.copy_rates_from_pos = copy_rates_from_pos
    m.symbol_info = lambda s: _Info()
    # Tick time is on the broker clock — that's what offset detection reads.
    m.symbol_info_tick = lambda s: _Tick(115000 - spread / 2, 115000 + spread / 2,
                                         __import__("time").time() + BROKER_OFFSET_H * 3600)
    m.account_info = lambda: _Acct(equity, balance)
    m.positions_get = lambda **kw: []
    m.history_deals_get = lambda *a, **kw: list(deals)
    m.last_error = lambda: (0, "none")
    m.symbol_select = lambda s, v: True

    def order_send(req):
        m._orders.append(req)
        return _Result(m.TRADE_RETCODE_DONE, 1000 + len(m._orders))
    m.order_send = order_send
    return m


class _Deal:
    def __init__(self, profit, commission=0.0, swap=0.0, entry=1):
        self.profit, self.commission, self.swap, self.entry = profit, commission, swap, entry


def load_connector(mock):
    """(Re)import the connector bound to a fresh mock."""
    sys.modules["MetaTrader5"] = mock
    for name in list(sys.modules):
        if name.startswith("connector.mt5_connector"):
            del sys.modules[name]
    import connector.mt5_connector as c
    c.mt5 = mock
    c.TIMEFRAME_MAP = {"M5": mock.TIMEFRAME_M5, "M15": mock.TIMEFRAME_M15}
    c._BROKER_OFFSET_HOURS = None
    c._LAST_BAR_SEEN.clear()
    return c


# ---------------------------------------------------------------------------
print("A. forming candle is dropped, times normalised to UTC")

mock = make_mock()
c = load_connector(mock)
df = c.get_candles("BTCUSDm", "M5", count=100)

check("returns exactly `count` closed bars", len(df) == 100)
newest = int(df["time"].iloc[-1])
check("newest bar is the last CLOSED bar, not the forming one",
      newest == mock._last_closed_utc)
check("forming bar is absent", newest + BAR not in set(df["time"].astype(int)))
check("times are true UTC (aligned to the bar grid)", newest % BAR == 0)
check("broker offset was detected as +3", c.get_broker_offset_hours() == 3.0)

# Without the fix the newest timestamp would be 3h ahead and one bar later.
check("uncorrected time would have been 3h off",
      (newest + BROKER_OFFSET_H * 3600) - newest == 10800)


# ---------------------------------------------------------------------------
print("B. new-bar gate prevents repeat entries on the same bar")

def run_polls(mock, c, polls, advance_bar_after=None):
    """Run run_cycle several times; optionally roll to a new bar partway."""
    import connector.config as cfg
    c.sync_trade_state = lambda: []
    # Must stub this: the real one appends to data/trades.json and
    # data/raw_trade_log.jsonl, so tests would pollute live trade history.
    c.append_trade_record = lambda t: None
    c.update_equity_snapshot = lambda: None
    c.write_strategy_state = lambda s: None
    c.write_candle_snapshot = lambda *a, **k: None
    c.commit_and_push = lambda *a, **k: True
    c.read_status = lambda: {"power": "on", "mode": "demo"}
    c.read_strategy_config = lambda: {"active_strategy": "always_long",
                                      "active_session": "24h"}
    # Deterministic always-fire strategy so we're testing the GATE, not TA.
    from strategy.signals import Signal
    c.STRATEGIES = {"always_long": lambda df: Signal(
        direction="long", votes={"t": "long"}, vote_count=1, reason="test",
        meta={"atr": 300.0, "atr_ok": True})}
    c.get_ollama_vote = lambda sig: "long"
    c.atr_filter_ok = lambda df, mult=1.0: True
    cfg.TIMEFRAMES = ["M5"]
    for i in range(polls):
        if advance_bar_after is not None and i == advance_bar_after:
            mock._last_closed_utc += BAR
        c.run_cycle()

mock = make_mock(equity=5000.0, balance=5000.0)
c = load_connector(mock)
run_polls(mock, c, polls=5)
check("5 polls of the SAME closed bar produce exactly 1 order", len(mock._orders) == 1)

mock = make_mock(equity=5000.0, balance=5000.0)
c = load_connector(mock)
run_polls(mock, c, polls=6, advance_bar_after=3)
check("a new bar re-enables trading (2 orders across 2 bars)", len(mock._orders) == 2)


# ---------------------------------------------------------------------------
print("C. daily loss breaker blocks new entries")

# Day started at 5000; realized -300 today, equity 4700 => -6% vs 3% limit.
mock = make_mock(equity=4700.0, balance=4700.0, deals=[_Deal(-300.0)])
c = load_connector(mock)
run_polls(mock, c, polls=3, advance_bar_after=1)
check("no orders placed while daily loss limit is breached", len(mock._orders) == 0)

# Small loss: trading continues.
mock = make_mock(equity=4950.0, balance=4950.0, deals=[_Deal(-50.0)])
c = load_connector(mock)
run_polls(mock, c, polls=2)
check("a 1% down day still trades", len(mock._orders) == 1)


# ---------------------------------------------------------------------------
print("D. place_trade refuses oversized risk and wide spreads")

# The real account: $78 equity. Min lot forces ~3.8% risk => refuse.
mock = make_mock(equity=78.0, balance=78.0)
c = load_connector(mock)
trade = c.place_trade("BTCUSDm", "long", "M5", {"reason": "t", "votes": {}, "meta": {"atr": 300.0}})
check("trade refused on $78 account (min lot exceeds risk ceiling)", trade is None)
check("no order was sent to the broker", len(mock._orders) == 0)

# Healthy account: trade goes through, and records true risk.
mock = make_mock(equity=5000.0, balance=5000.0)
c = load_connector(mock)
trade = c.place_trade("BTCUSDm", "long", "M5", {"reason": "t", "votes": {}, "meta": {"atr": 300.0}})
check("healthy account trade is placed", trade is not None)
check("trade records the actual risk taken", trade["risk_pct"] > 0)
check("trade records the spread paid", trade["spread_at_entry"] == 10.0)
check("stop is below entry for a long", trade["stop"] < trade["entry"])
check("sl/tp rounded to symbol digits",
      round(trade["stop"], 2) == trade["stop"] and round(trade["target"], 2) == trade["target"])

# Wide spread ($60 vs $300 ATR = 20% > 15% cap) => rejected.
mock = make_mock(equity=5000.0, balance=5000.0, spread=60.0)
c = load_connector(mock)
trade = c.place_trade("BTCUSDm", "long", "M5", {"reason": "t", "votes": {}, "meta": {"atr": 300.0}})
check("wide spread blocks the trade", trade is None)
check("no order sent on wide spread", len(mock._orders) == 0)

print(f"\nALL PASSED ({passed} assertions)")
