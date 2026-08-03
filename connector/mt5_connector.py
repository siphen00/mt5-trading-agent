"""
Main connector loop. Runs on your Windows machine/VPS where MT5 is installed.

This is the piece I can write but can't test from here — it needs a live MT5
terminal. Run it, watch the console output, and we'll debug against the real
errors together (this is exactly the kind of iterative loop Claude Code is
built for).

Responsibilities:
  1. Connect to the MT5 demo account
  2. Every POLL_INTERVAL_SEC: check control/status.json (power on/off, mode)
  3. If ON: pull latest M5/M15 candles, run the signal engine + Ollama veto,
     execute trades within risk limits
  4. Log every trade (with full reasoning) to data/trades.json and
     data/raw_trade_log.jsonl (the journal scripts consume the .jsonl)
  5. Sync equity + trade data back to GitHub so the dashboard updates
"""

import json
import time
import traceback
from datetime import datetime, timezone

import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # allows this file to be imported for testing on non-Windows machines

from connector import config
from connector.git_sync import commit_and_push
from strategy.signals import build_signal, atr_filter_ok
from strategy.ollama_veto import get_ollama_vote

TIMEFRAME_MAP = {
    "M5": getattr(mt5, "TIMEFRAME_M5", None) if mt5 else None,
    "M15": getattr(mt5, "TIMEFRAME_M15", None) if mt5 else None,
}


def connect() -> bool:
    if mt5 is None:
        raise RuntimeError(
            "MetaTrader5 package not available. This connector must run on "
            "Windows with the MetaTrader5 terminal installed. "
            "pip install MetaTrader5"
        )
    kwargs = {}
    if config.MT5_PATH:
        kwargs["path"] = config.MT5_PATH
    if not mt5.initialize(**kwargs):
        print(f"[connector] MT5 initialize() failed: {mt5.last_error()}")
        return False
    if config.MT5_LOGIN:
        authorized = mt5.login(config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER)
        if not authorized:
            print(f"[connector] MT5 login failed: {mt5.last_error()}")
            return False
    print(f"[connector] Connected to MT5. Account: {mt5.account_info()}")
    return True


def read_status() -> dict:
    """Read control/status.json — this is how the dashboard's power toggle reaches the connector."""
    try:
        with open(config.STATUS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"power": "off", "mode": "demo"}


def get_candles(symbol: str, timeframe_str: str, count: int = 200) -> pd.DataFrame:
    tf = TIMEFRAME_MAP[timeframe_str]

    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(
            f"Symbol '{symbol}' not found on this broker. Check the exact name in "
            f"MT5's Market Watch (right-click -> Symbols, search 'BTC') and update "
            f"SYMBOL in connector/config.py — brokers often use suffixes like "
            f"'BTCUSDm' or 'BTCUSD.m' rather than plain 'BTCUSD'."
        )
    if not info.visible:
        # Symbol exists but isn't active in Market Watch yet — copy_rates_from_pos
        # can silently fail until it's explicitly selected.
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Could not add '{symbol}' to Market Watch: {mt5.last_error()}")

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No candle data returned for {symbol} {timeframe_str}: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    return df


def calc_position_size(equity: float, entry: float, stop: float) -> float:
    """Risk a fixed % of equity per trade based on stop distance."""
    risk_amount = equity * (config.RISK_PER_TRADE_PCT / 100)
    stop_distance = abs(entry - stop)
    if stop_distance == 0:
        return 0.01  # fallback minimum lot, avoid divide-by-zero
    lots = risk_amount / stop_distance
    return round(max(lots, 0.01), 2)


def place_trade(symbol: str, direction: str, timeframe: str, signal_meta: dict) -> dict | None:
    """Places a market order with SL/TP and returns a trade record dict, or None on failure."""
    tick = mt5.symbol_info_tick(symbol)
    equity = mt5.account_info().equity

    atr_estimate = signal_meta.get("atr", equity * 0.002)  # fallback if not present
    if direction == "long":
        entry = tick.ask
        stop = entry - atr_estimate * 1.5
        target = entry + atr_estimate * 2.5
        order_type = mt5.ORDER_TYPE_BUY
    else:
        entry = tick.bid
        stop = entry + atr_estimate * 1.5
        target = entry - atr_estimate * 2.5
        order_type = mt5.ORDER_TYPE_SELL

    lots = calc_position_size(equity, entry, stop)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": entry,
        "sl": stop,
        "tp": target,
        "deviation": 20,
        "magic": 20260802,
        "comment": f"apex-{timeframe}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[connector] order_send failed: {result.retcode} {result.comment}")
        return None

    return {
        "ticket": result.order,
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "lots": lots,
        "reason": signal_meta.get("reason", ""),
        "votes": signal_meta.get("votes", {}),
        "meta": signal_meta.get("meta", {}),
        "status": "open",
    }


def append_trade_record(trade: dict):
    """Appends to data/trades.json (dashboard reads this) and the .jsonl log (journal scripts read this)."""
    try:
        with open(config.TRADES_FILE) as f:
            trades = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        trades = []
    trades.append(trade)
    with open(config.TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

    with open(config.JOURNAL_LOG_FILE, "a") as f:
        f.write(json.dumps(trade) + "\n")


def update_equity_snapshot():
    equity = mt5.account_info().equity
    try:
        with open(config.EQUITY_FILE) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append({"time": datetime.now(timezone.utc).isoformat(), "equity": equity})
    with open(config.EQUITY_FILE, "w") as f:
        json.dump(history[-2000:], f, indent=2)  # cap history length


def write_strategy_state(state: dict):
    """Latest signal snapshot per timeframe — feeds the dashboard's live strategy panel,
    independent of whether a trade actually fired."""
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(config.REPO_PATH + "/data/strategy_state.json", "w") as f:
        json.dump(state, f, indent=2)


def run_cycle():
    status = read_status()
    if status.get("power") != "on":
        return  # agent is paused from the dashboard — do nothing

    open_positions = mt5.positions_get(symbol=config.SYMBOL) or []
    state_snapshot = {}

    for tf in config.TIMEFRAMES:
        df = get_candles(config.SYMBOL, tf)
        atr_ok = atr_filter_ok(df, config.ATR_MIN_MULTIPLIER)
        signal = build_signal(df, config.EMA_FAST, config.EMA_SLOW)

        state_snapshot[tf] = {
            "direction": signal.direction,
            "votes": signal.votes,
            "reason": signal.reason,
            "atr_ok": atr_ok,
        }

        if not atr_ok or signal.direction == "none":
            continue
        if len(open_positions) >= config.MAX_CONCURRENT_TRADES:
            continue

        ollama_vote = get_ollama_vote(signal)
        signal.votes["ollama"] = ollama_vote
        state_snapshot[tf]["votes"] = signal.votes
        total_votes = sum(1 for v in signal.votes.values() if v == signal.direction)

        if total_votes < config.VOTES_REQUIRED:
            continue

        trade = place_trade(config.SYMBOL, signal.direction, tf, {
            "reason": signal.reason,
            "votes": signal.votes,
            "meta": signal.meta,
        })
        if trade:
            print(f"[connector] Trade opened: {trade}")
            append_trade_record(trade)
            open_positions = mt5.positions_get(symbol=config.SYMBOL) or []

    write_strategy_state(state_snapshot)
    update_equity_snapshot()
    commit_and_push(
        ["data/trades.json", "data/equity.json", "data/raw_trade_log.jsonl", "data/strategy_state.json"],
        f"Trade sync {datetime.now(timezone.utc).isoformat()}",
    )


def main():
    if not connect():
        return
    print(f"[connector] Starting main loop, polling every {config.POLL_INTERVAL_SEC}s")
    while True:
        try:
            run_cycle()
        except Exception:
            print("[connector] Error in run_cycle:")
            traceback.print_exc()
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
