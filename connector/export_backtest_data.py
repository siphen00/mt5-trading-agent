"""
Exports a deep slice of historical candles for the dashboard's backtest lab.
This is deliberately NOT part of the live connector loop — pulling
thousands of bars and committing them every 15 seconds would bloat the repo
and slow down the live cycle for no benefit. Run this manually whenever you
want fresher backtest data (weekly is plenty for most purposes):

    python -m connector.export_backtest_data

Exports data/backtest_M5.json and data/backtest_M15.json, then commits and
pushes them. The backtest page (dashboard/backtest.html) reads these files
and runs the actual backtest calculations in your browser.
"""

import json
import sys
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

from connector import config
from connector.mt5_connector import connect, get_candles
from connector.git_sync import commit_and_push

# How much history to pull per timeframe. 5000 M5 candles ≈ ~17 days of
# 24/7 crypto trading; 5000 M15 candles ≈ ~52 days. Enough to see a real
# range of conditions without the export file getting unwieldy.
CANDLE_COUNT = 5000


def export_timeframe(tf: str):
    print(f"[export_backtest_data] Pulling {CANDLE_COUNT} {tf} candles for {config.SYMBOL}...")
    df = get_candles(config.SYMBOL, tf, count=CANDLE_COUNT)
    candles = [
        {"time": int(row["time"]), "open": float(row["open"]), "high": float(row["high"]),
         "low": float(row["low"]), "close": float(row["close"]), "volume": float(row["volume"])}
        for _, row in df.iterrows()
    ]
    path = f"{config.REPO_PATH}/data/backtest_{tf}.json"
    with open(path, "w") as f:
        json.dump(candles, f)
    print(f"[export_backtest_data] Wrote {len(candles)} candles to {path}")


def main():
    if not connect():
        print("[export_backtest_data] Could not connect to MT5.")
        sys.exit(1)

    for tf in config.TIMEFRAMES:
        export_timeframe(tf)

    pushed = commit_and_push(
        [f"data/backtest_{tf}.json" for tf in config.TIMEFRAMES],
        f"Backtest data export {datetime.now(timezone.utc).isoformat()}",
    )
    print("[export_backtest_data] Synced to GitHub." if pushed else
          "[export_backtest_data] Local export succeeded but push failed — check git_sync errors above.")


if __name__ == "__main__":
    main()
