"""
Diagnostic tool: places ONE small test trade directly, bypassing the
strategy engine entirely. Use this to answer "is the bot not trading
because there are genuinely no signals, or because something in the
execution path is broken?"

Run this with the connector NOT running at the same time (both would try
to use the same MT5 connection). Safe to run on a demo account — it opens
a real position with a real SL/TP, so don't run this against a live account.

Usage:
    python -m connector.test_trade
"""

import sys
from connector import config
from connector.mt5_connector import connect, place_trade

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


def main():
    print("[test_trade] Connecting to MT5...")
    if not connect():
        print("[test_trade] FAILED to connect — this is your problem right here. "
              "Check .env credentials and MT5_PATH in connector/config.py.")
        sys.exit(1)

    info = mt5.symbol_info(config.SYMBOL)
    if info is None:
        print(f"[test_trade] FAILED — symbol '{config.SYMBOL}' not found on this broker. "
              f"Check the exact name in MT5's Market Watch.")
        sys.exit(1)
    print(f"[test_trade] Symbol OK: {config.SYMBOL}, visible={info.visible}, "
          f"trade_mode={info.trade_mode}")

    account = mt5.account_info()
    print(f"[test_trade] Account balance: {account.balance}, equity: {account.equity}, "
          f"trade_allowed: {account.trade_allowed}")
    if not account.trade_allowed:
        print("[test_trade] FAILED — trade_allowed is False on this account. "
              "Check Algo Trading is enabled in MT5 (top toolbar) and this account permits it.")
        sys.exit(1)

    tick = mt5.symbol_info_tick(config.SYMBOL)
    print(f"[test_trade] Current tick: bid={tick.bid}, ask={tick.ask}")

    print("[test_trade] Attempting a real test trade (long, minimum size)...")
    trade = place_trade(config.SYMBOL, "long", "TEST", {
        "reason": "manual diagnostic test trade",
        "votes": {},
        "meta": {"atr": (tick.ask - tick.bid) * 20 or 50},  # rough fallback distance
    })

    if trade is None:
        print("[test_trade] FAILED — order_send was rejected. Check the retcode/comment "
              "printed above by place_trade() for the exact broker error "
              "(common causes: wrong filling mode, invalid stops distance, "
              "min lot size, market closed).")
        sys.exit(1)

    print(f"[test_trade] SUCCESS — order placed: {trade}")
    print("[test_trade] Check your MT5 terminal's Trade tab to confirm the position is open. "
          "You'll want to manually close it since this script doesn't.")


if __name__ == "__main__":
    main()
