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
from datetime import datetime, timezone, timedelta

import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # allows this file to be imported for testing on non-Windows machines

from connector import config
from connector import risk as risk_mod
from connector import broker_time
from connector.git_sync import commit_and_push
from strategy.signals import atr_filter_ok
from strategy.strategies import STRATEGIES, DEFAULT_STRATEGY
from strategy.sessions import in_session, DEFAULT_SESSION
from strategy.ollama_veto import get_ollama_vote

TIMEFRAME_MAP = {
    "M1": getattr(mt5, "TIMEFRAME_M1", None) if mt5 else None,
    "M3": getattr(mt5, "TIMEFRAME_M3", None) if mt5 else None,
    "M5": getattr(mt5, "TIMEFRAME_M5", None) if mt5 else None,
    "M15": getattr(mt5, "TIMEFRAME_M15", None) if mt5 else None,
}


# Broker clock offset vs true UTC, detected once per run (see connector/broker_time.py).
_BROKER_OFFSET_HOURS: float | None = None

# Last CLOSED bar we actually evaluated, per timeframe. Guards against
# re-evaluating the same bar on every 15s poll (a 5-minute bar would otherwise
# be acted on ~20 times). In-memory on purpose: after a restart we simply
# evaluate the current closed bar once, with no stale flag to get stuck.
_LAST_BAR_SEEN: dict[str, int] = {}


def get_broker_offset_hours() -> float:
    """Detect (once) how far the broker's clock sits from true UTC."""
    global _BROKER_OFFSET_HOURS
    if config.BROKER_UTC_OFFSET_HOURS is not None:
        return float(config.BROKER_UTC_OFFSET_HOURS)
    if _BROKER_OFFSET_HOURS is None:
        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            print("[connector] WARNING: no tick for broker-time detection, assuming UTC+0")
            _BROKER_OFFSET_HOURS = 0.0
        else:
            _BROKER_OFFSET_HOURS = broker_time.detect_offset_hours(tick.time, time.time())
            print(f"[connector] Broker clock detected at UTC{_BROKER_OFFSET_HOURS:+g} — "
                  f"candle times normalised to true UTC for session/ORB logic")
    return _BROKER_OFFSET_HOURS


def get_symbol_spec(symbol: str) -> risk_mod.SymbolSpec:
    """Adapt MT5's symbol_info into the pure SymbolSpec used by connector/risk.py."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol '{symbol}' not found when reading contract specs")
    return risk_mod.SymbolSpec(
        tick_size=info.trade_tick_size or info.point,
        tick_value=info.trade_tick_value,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
        digits=info.digits,
        point=info.point,
        stops_level=info.trade_stops_level,
        freeze_level=info.trade_freeze_level,
    )


def realized_pnl_today(offset_hours: float) -> float:
    """
    Sum of today's closed P&L (profit + commission + swap) since 00:00 UTC.
    Used by the daily-loss breaker. history_deals_get expects broker-clock
    datetimes, so the UTC day boundary is converted back into broker time.
    """
    now_utc = datetime.now(timezone.utc)
    day_start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert the UTC window into the broker's clock for the history query.
    to_broker = timedelta(hours=offset_hours)
    deals = mt5.history_deals_get(day_start_utc + to_broker, now_utc + to_broker) or []
    return sum(d.profit + d.commission + d.swap for d in deals)


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


def read_strategy_config() -> dict:
    """Read control/strategy_config.json — the dashboard's strategy/session selector writes here."""
    try:
        with open(config.STRATEGY_CONFIG_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    strategy_id = data.get("active_strategy", DEFAULT_STRATEGY)
    session_id = data.get("active_session", DEFAULT_SESSION)
    if strategy_id not in STRATEGIES:
        print(f"[connector] Unknown strategy '{strategy_id}' in control file, falling back to {DEFAULT_STRATEGY}")
        strategy_id = DEFAULT_STRATEGY
    return {"active_strategy": strategy_id, "active_session": session_id}


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

    # Fetch one extra bar because position 0 is the CURRENTLY FORMING candle.
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count + 1)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No candle data returned for {symbol} {timeframe_str}: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)

    # Drop the incomplete bar. Every strategy reads .iloc[-1]; with the forming
    # candle present, its close kept changing on each 15s poll, so signals could
    # appear and then un-appear (repainting) and the same bar could be traded
    # repeatedly. Strategies now only ever see CLOSED candles.
    if len(df) > 1:
        df = df.iloc[:-1].reset_index(drop=True)

    # MT5 timestamps are in broker server time. Normalise to true UTC so
    # sessions.py windows and orb_signal's opening range land on the right hours.
    df["time"] = broker_time.normalise_candle_times(df["time"], get_broker_offset_hours())
    return df


# Position sizing now lives in connector/risk.py (calc_position_size there),
# which converts stop distance to money using the symbol's tick value/tick size
# and refuses trades where the broker's minimum lot would blow past the risk
# target. The old version here assumed 1 lot moved $1 per $1 of price and used
# max(lots, 0.01), which silently turned a 0.5% trade into roughly 4% on a
# small account.


def place_trade(symbol: str, direction: str, timeframe: str, signal_meta: dict) -> dict | None:
    """Places a market order with SL/TP and returns a trade record dict, or None on failure."""
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    equity = mt5.account_info().equity
    spec = get_symbol_spec(symbol)

    # signal_meta is shaped {"reason":..., "votes":..., "meta": {...}} by every
    # caller — the real ATR value (when available) lives in meta["atr"], not
    # at the top level. Previously this looked in the wrong place and silently
    # fell back to a tiny equity-based guess every single time.
    atr_estimate = (signal_meta.get("meta") or {}).get("atr") or (equity * 0.002)

    # --- Spread gate ------------------------------------------------------
    spread_check = risk_mod.check_spread(tick.bid, tick.ask, atr_estimate,
                                         config.MAX_SPREAD_ATR_FRACTION)
    if not spread_check.ok:
        print(f"[connector] Trade skipped — {spread_check.reason}")
        return None

    # Stop must clear the broker's minimum distance (the 10016 "Invalid stops"
    # cause) AND be wide enough that the spread isn't a big share of the risk.
    floor_distance = risk_mod.min_stop_distance(spec, spread_check.spread,
                                                config.SPREAD_STOP_MULTIPLE)
    stop_distance = max(atr_estimate * 1.5, floor_distance)
    target_distance = max(atr_estimate * 2.5, floor_distance * 1.67)

    if direction == "long":
        entry = tick.ask
        stop = entry - stop_distance
        target = entry + target_distance
        order_type = mt5.ORDER_TYPE_BUY
    else:
        entry = tick.bid
        stop = entry + stop_distance
        target = entry - target_distance
        order_type = mt5.ORDER_TYPE_SELL

    # Round everything to the symbol's actual tick size — brokers reject prices
    # that don't align to their digits/point, which was the other half of 10016.
    entry = round(entry, info.digits)
    stop = round(stop, info.digits)
    target = round(target, info.digits)

    # --- Position sizing (contract-aware, with a hard risk ceiling) --------
    sizing = risk_mod.calc_position_size(
        equity=equity, entry=entry, stop=stop, spec=spec,
        risk_pct=config.RISK_PER_TRADE_PCT,
        max_effective_risk_pct=config.MAX_EFFECTIVE_RISK_PCT,
    )
    if not sizing.ok:
        print(f"[connector] TRADE REFUSED — {sizing.reason}")
        return None
    if sizing.floored_by_min_lot:
        print(f"[connector] NOTE: minimum lot raised risk to {sizing.effective_risk_pct:.2f}% "
              f"(${sizing.effective_risk:.2f}) vs target {config.RISK_PER_TRADE_PCT:.2f}% "
              f"(${sizing.intended_risk:.2f}) — within the {config.MAX_EFFECTIVE_RISK_PCT:.2f}% ceiling")
    lots = sizing.lots

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": entry,
        "sl": stop,
        "tp": target,
        "deviation": 20,
        "magic": config.BOT_MAGIC,
        "comment": f"apex-{timeframe}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[connector] order_send failed: {result.retcode} {result.comment}")
        print(f"[connector]   request was: entry={entry} sl={stop} tp={target} lots={lots} "
              f"min_stop_distance={min_stop_distance} digits={info.digits} "
              f"stops_level={info.trade_stops_level} volume_min={info.volume_min} volume_step={info.volume_step}")
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
        # Recorded so you can audit later what was ACTUALLY risked per trade
        # rather than assuming it matched RISK_PER_TRADE_PCT.
        "risk_amount": round(sizing.effective_risk, 2),
        "risk_pct": round(sizing.effective_risk_pct, 3),
        "risk_floored_by_min_lot": sizing.floored_by_min_lot,
        "spread_at_entry": round(spread_check.spread, 2),
        "reason": signal_meta.get("reason", ""),
        "votes": signal_meta.get("votes", {}),
        "meta": signal_meta.get("meta", {}),
        "status": "open",
        "source": "bot",
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


# MT5's DEAL_REASON codes tell us exactly how a position was closed — this is
# what lets the dashboard show "closed by bot (take profit)" vs "closed
# manually" instead of just guessing.
CLOSE_REASON_LABELS = {
    0: "manual",              # DEAL_REASON_CLIENT — closed from the desktop terminal
    1: "manual",              # DEAL_REASON_MOBILE
    2: "manual",              # DEAL_REASON_WEB
    3: "bot (script)",        # DEAL_REASON_EXPERT — order_send() from Python (this connector or test_trade.py)
    4: "bot (stop loss)",     # DEAL_REASON_SL
    5: "bot (take profit)",   # DEAL_REASON_TP
    6: "broker (stop out)",   # DEAL_REASON_SO — margin call
    7: "broker (rollover)",
    8: "broker (margin)",
    9: "broker (split)",
}


def _lookup_close(ticket: int) -> dict | None:
    """Looks up how and when a position actually closed, from MT5's deal history."""
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None
    exit_deals = [d for d in deals if d.entry in (1, 3)]  # DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY
    if not exit_deals:
        return None
    last_exit = exit_deals[-1]
    total_pnl = sum(d.profit + d.commission + d.swap for d in exit_deals)
    return {
        "exit": last_exit.price,
        "exit_time": datetime.fromtimestamp(last_exit.time, tz=timezone.utc).isoformat(),
        "pnl": round(total_pnl, 2),
        "closed_by": CLOSE_REASON_LABELS.get(last_exit.reason, f"unknown ({last_exit.reason})"),
    }


def _import_untracked_history(trades: list, known_tickets: set, lookback_days: int = 3) -> bool:
    """Picks up trades that opened AND closed entirely between our polling cycles, or
    before this session started — e.g. a manual trade you opened and closed quickly.
    Without this, a trade like that would never appear anywhere, since it's not in our
    ledger and it's no longer an open MT5 position either."""
    date_from = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    deals = mt5.history_deals_get(date_from, datetime.now(timezone.utc), group=config.SYMBOL) or []
    by_position = {}
    for d in deals:
        by_position.setdefault(d.position_id, []).append(d)

    changed = False
    for position_id, group in by_position.items():
        if position_id in known_tickets:
            continue
        entry_deals = [d for d in group if d.entry == 0]  # DEAL_ENTRY_IN
        exit_deals = [d for d in group if d.entry in (1, 3)]
        if not entry_deals or not exit_deals:
            continue  # incomplete pair — still open, or history gap; leave for other paths to handle
        entry, last_exit = entry_deals[0], exit_deals[-1]
        total_pnl = sum(d.profit + d.commission + d.swap for d in exit_deals)
        trades.append({
            "ticket": position_id, "symbol": config.SYMBOL, "timeframe": "MANUAL",
            "direction": "long" if entry.type == 0 else "short",
            "entry": entry.price, "exit": last_exit.price, "lots": entry.volume,
            "reason": "opened + closed outside the bot", "votes": {}, "meta": {},
            "status": "closed",
            "source": "bot" if entry.magic == config.BOT_MAGIC else "manual",
            "closed_by": CLOSE_REASON_LABELS.get(last_exit.reason, f"unknown ({last_exit.reason})"),
            "pnl": round(total_pnl, 2),
            "time": datetime.fromtimestamp(entry.time, tz=timezone.utc).isoformat(),
            "exit_time": datetime.fromtimestamp(last_exit.time, tz=timezone.utc).isoformat(),
        })
        known_tickets.add(position_id)
        changed = True
    return changed


def sync_trade_state() -> list:
    """
    Reconciles our internal trades.json against what MT5 actually shows —
    this is the fix for trades staying stuck as "open" forever once closed
    outside the bot's own logic (manually, or via SL/TP which MT5 executes
    at the broker level without the bot doing anything itself):

    1. Writes data/open_positions.json — every position MT5 currently shows
       as open, tagged "bot" or "manual" by its magic number.
    2. Any trade we recorded as "open" that MT5 no longer shows open gets
       its real exit price/time/pnl/close-reason filled in from history.
    3. Any position MT5 knows about that we never recorded (e.g. a manual
       trade) gets added, so it still shows up on the dashboard.
    4. Any trade that opened AND closed between polling cycles gets pulled
       in from recent history too.

    Returns the current open-positions snapshot (used for the bot's own
    concurrent-trade limit, counting only its own trades — not yours).
    """
    positions = mt5.positions_get(symbol=config.SYMBOL) or []
    open_tickets = {p.ticket for p in positions}

    open_snapshot = [{
        "ticket": p.ticket,
        "direction": "long" if p.type == mt5.POSITION_TYPE_BUY else "short",
        "volume": p.volume,
        "entry": p.price_open,
        "current_price": p.price_current,
        "sl": p.sl, "tp": p.tp,
        "profit": round(p.profit, 2),
        "source": "bot" if p.magic == config.BOT_MAGIC else "manual",
        "opened_at": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
    } for p in positions]

    with open(config.OPEN_POSITIONS_FILE, "w") as f:
        json.dump(open_snapshot, f, indent=2)

    try:
        with open(config.TRADES_FILE) as f:
            trades = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        trades = []

    known_tickets = {t.get("ticket") for t in trades if t.get("ticket") is not None}
    changed = False

    for t in trades:
        if t.get("status") == "open" and t.get("ticket") not in open_tickets:
            close_info = _lookup_close(t["ticket"])
            if close_info:
                t.update(close_info)
                t["status"] = "closed"
                changed = True

    for p in positions:
        if p.ticket not in known_tickets:
            trades.append({
                "ticket": p.ticket, "symbol": p.symbol, "timeframe": "MANUAL",
                "direction": "long" if p.type == mt5.POSITION_TYPE_BUY else "short",
                "entry": p.price_open, "stop": p.sl, "target": p.tp, "lots": p.volume,
                "reason": "opened manually in MT5", "votes": {}, "meta": {},
                "status": "open", "source": "manual",
                "time": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            })
            known_tickets.add(p.ticket)
            changed = True

    if _import_untracked_history(trades, known_tickets):
        changed = True

    if changed:
        with open(config.TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)

    return open_snapshot


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


def write_candle_snapshot(timeframe: str, df: pd.DataFrame, keep: int = 150):
    """
    Exports recent OHLC candles so the dashboard can render an execution chart
    (your actual bot's candles, with trade markers), separate from the
    TradingView widget which shows a public market feed instead of your
    broker's private quotes.
    """
    recent = df.tail(keep)
    candles = [
        {
            "time": int(row["time"]),  # MT5 gives unix seconds already
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for _, row in recent.iterrows()
    ]
    with open(f"{config.REPO_PATH}/data/candles_{timeframe}.json", "w") as f:
        json.dump(candles, f)


def run_cycle():
    status = read_status()
    if status.get("power") != "on":
        print(f"[connector] {datetime.now(timezone.utc).strftime('%H:%M:%S')} — power is OFF, idling")
        return  # agent is paused from the dashboard — do nothing

    strategy_cfg = read_strategy_config()
    active_strategy_id = strategy_cfg["active_strategy"]
    active_session_id = strategy_cfg["active_session"]
    strategy_fn = STRATEGIES[active_strategy_id]
    session_active = in_session(active_session_id)

    open_snapshot = sync_trade_state()
    bot_open_count = sum(1 for p in open_snapshot if p["source"] == "bot")

    # --- Daily loss breaker -------------------------------------------------
    # Recomputed from broker truth every cycle (stateless, so it can't get stuck
    # halted across a restart). Blocks NEW entries only — existing positions keep
    # their SL/TP and are left alone.
    acct = mt5.account_info()
    daily = risk_mod.evaluate_daily_loss(
        equity_now=acct.equity, balance_now=acct.balance,
        realized_today=realized_pnl_today(get_broker_offset_hours()),
        max_daily_loss_pct=config.MAX_DAILY_LOSS_PCT,
    )
    if daily.halted:
        print(f"[connector] DAILY LOSS LIMIT HIT — {daily.reason}")

    state_snapshot = {"active_strategy": active_strategy_id, "active_session": active_session_id,
                       "session_active": session_active,
                       "daily_loss_pct": round(daily.loss_pct, 2),
                       "daily_loss_halted": daily.halted,
                       "open_positions_bot": bot_open_count, "open_positions_manual": len(open_snapshot) - bot_open_count}

    for tf in config.TIMEFRAMES:
        df = get_candles(config.SYMBOL, tf)
        write_candle_snapshot(tf, df)
        atr_ok = atr_filter_ok(df, config.ATR_MIN_MULTIPLIER)
        signal = strategy_fn(df)

        # New-bar gate: only ACT once per closed bar. The connector polls every
        # 15s, so without this a single M5 bar would be evaluated ~20 times and
        # could be entered repeatedly. Evaluation/logging still happens every
        # cycle so the dashboard stays live; only trading is gated.
        last_bar_time = int(df["time"].iloc[-1])
        is_new_bar = _LAST_BAR_SEEN.get(tf) != last_bar_time

        state_snapshot[tf] = {
            "direction": signal.direction,
            "votes": signal.votes,
            "reason": signal.reason,
            "atr_ok": atr_ok,
            "bar_time": datetime.fromtimestamp(last_bar_time, tz=timezone.utc).isoformat(),
            "new_bar": is_new_bar,
        }

        if daily.halted:
            continue  # kill switch active — no new entries today
        if not session_active:
            continue  # outside the selected trading session — evaluate and log, but don't trade
        if not is_new_bar:
            continue  # already acted on this closed bar
        if not atr_ok or signal.direction == "none":
            continue
        if bot_open_count >= config.MAX_CONCURRENT_TRADES:
            continue

        # Mark the bar consumed at the point we commit to acting on it, so a
        # downstream veto/refusal doesn't cause a retry on the very next poll.
        _LAST_BAR_SEEN[tf] = last_bar_time

        # Ollama is a universal confirm/veto layer regardless of which
        # strategy is active — each strategy already encapsulates its own
        # internal confluence, so this is the one external sanity check
        # rather than a vote-counting threshold.
        ollama_vote = get_ollama_vote(signal)
        signal.votes["ollama"] = ollama_vote
        state_snapshot[tf]["votes"] = signal.votes

        if ollama_vote != signal.direction:
            continue

        trade = place_trade(config.SYMBOL, signal.direction, tf, {
            "reason": f"[{active_strategy_id}] {signal.reason}",
            "votes": signal.votes,
            "meta": signal.meta,
        })
        if trade:
            print(f"[connector] Trade opened: {trade}")
            append_trade_record(trade)
            bot_open_count += 1

    write_strategy_state(state_snapshot)
    update_equity_snapshot()
    pushed = commit_and_push(
        ["data/trades.json", "data/equity.json", "data/raw_trade_log.jsonl", "data/strategy_state.json",
         "data/open_positions.json", *[f"data/candles_{tf}.json" for tf in config.TIMEFRAMES]],
        f"Trade sync {datetime.now(timezone.utc).isoformat()}",
    )
    session_note = f"{active_session_id}{'✓' if session_active else '(closed)'}"
    summary = " | ".join(f"{tf}: {state_snapshot[tf]['direction']} ({state_snapshot[tf]['reason']})"
                          for tf in config.TIMEFRAMES if tf in state_snapshot)
    sync_status = "synced" if pushed else "SYNC FAILED — see git_sync errors above"
    risk_note = f"day {daily.loss_pct:+.2f}%{' HALTED' if daily.halted else ''}"
    print(f"[connector] {datetime.now(timezone.utc).strftime('%H:%M:%S')} — [{active_strategy_id}/{session_note}] "
          f"{summary} — {risk_note} — {sync_status}")


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
