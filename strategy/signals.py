"""
Signal engine: EMA crossover + Smart Money Concepts (SMC) structure detection.
Ported from the Binance swing/scalper bots, adapted to MT5's OHLC dataframe shape
(MT5's copy_rates_from_pos returns numpy structured arrays with:
 time, open, high, low, close, tick_volume, spread, real_volume)

This module is pure logic — no MT5 calls here, no side effects. Feed it a
pandas DataFrame of candles, it returns a Signal. Keeping it pure makes it
backtestable and unit-testable without a live MT5 connection.
"""

from dataclasses import dataclass, field
from typing import Literal
import pandas as pd
import numpy as np

Direction = Literal["long", "short", "none"]


@dataclass
class Signal:
    direction: Direction
    votes: dict = field(default_factory=dict)   # {"ema": "long", "smc": "long", "ollama": "long"}
    vote_count: int = 0
    reason: str = ""                             # human-readable, feeds the journal
    meta: dict = field(default_factory=dict)      # raw values for logging (ema values, ATR, etc.)


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def ema_cross_signal(df: pd.DataFrame, fast: int = 9, slow: int = 21) -> tuple[Direction, dict]:
    """Classic EMA crossover, confirmed on the most recently closed candle."""
    fast_ema = ema(df["close"], fast)
    slow_ema = ema(df["close"], slow)

    prev_diff = fast_ema.iloc[-2] - slow_ema.iloc[-2]
    curr_diff = fast_ema.iloc[-1] - slow_ema.iloc[-1]

    direction: Direction = "none"
    if prev_diff <= 0 and curr_diff > 0:
        direction = "long"
    elif prev_diff >= 0 and curr_diff < 0:
        direction = "short"

    return direction, {
        "ema_fast": round(fast_ema.iloc[-1], 2),
        "ema_slow": round(slow_ema.iloc[-1], 2),
    }


def detect_bos_choch(df: pd.DataFrame, lookback: int = 20) -> tuple[str, dict]:
    """
    Break of Structure / Change of Character.
    Simplified swing-high/swing-low detection over `lookback` candles, then checks
    whether the latest close broke beyond the most recent swing point.
    """
    window = df.iloc[-lookback:]
    highs, lows = window["high"], window["low"]

    swing_high = highs.iloc[:-1].max()
    swing_low = lows.iloc[:-1].min()
    last_close = df["close"].iloc[-1]

    if last_close > swing_high:
        return "bos_bullish", {"swing_high": round(swing_high, 2)}
    if last_close < swing_low:
        return "bos_bearish", {"swing_low": round(swing_low, 2)}
    return "none", {}


def detect_order_block(df: pd.DataFrame, lookback: int = 15) -> tuple[str, dict]:
    """
    Very simplified order block: last opposite-color candle before a strong
    directional move. Flags if price is currently retesting that zone.
    """
    window = df.iloc[-lookback:]
    body = (window["close"] - window["open"]).abs()
    move_idx = body.idxmax()  # candle with the largest body = the "impulse"

    if move_idx not in window.index or window.index.get_loc(move_idx) == 0:
        return "none", {}

    impulse_bullish = window.loc[move_idx, "close"] > window.loc[move_idx, "open"]
    ob_candle_idx = window.index[window.index.get_loc(move_idx) - 1]
    ob_low = window.loc[ob_candle_idx, "low"]
    ob_high = window.loc[ob_candle_idx, "high"]

    last_low, last_high = df["low"].iloc[-1], df["high"].iloc[-1]
    retesting = ob_low <= last_high and ob_high >= last_low

    if retesting:
        return ("ob_bullish_retest" if impulse_bullish else "ob_bearish_retest"), {
            "ob_low": round(ob_low, 2), "ob_high": round(ob_high, 2)
        }
    return "none", {}


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> tuple[str, dict]:
    """Wick beyond a recent high/low that immediately reverses (stop hunt)."""
    window = df.iloc[-lookback:-1]
    last = df.iloc[-1]

    recent_high = window["high"].max()
    recent_low = window["low"].min()

    if last["high"] > recent_high and last["close"] < recent_high:
        return "sweep_high", {"level": round(recent_high, 2)}
    if last["low"] < recent_low and last["close"] > recent_low:
        return "sweep_low", {"level": round(recent_low, 2)}
    return "none", {}


def detect_fvg(df: pd.DataFrame) -> tuple[str, dict]:
    """Fair value gap: a 3-candle imbalance where candle 1's range doesn't
    overlap candle 3's range."""
    if len(df) < 3:
        return "none", {}
    c1, c3 = df.iloc[-3], df.iloc[-1]
    if c1["high"] < c3["low"]:
        return "fvg_bullish", {"gap_low": round(c1["high"], 2), "gap_high": round(c3["low"], 2)}
    if c1["low"] > c3["high"]:
        return "fvg_bearish", {"gap_low": round(c3["high"], 2), "gap_high": round(c1["low"], 2)}
    return "none", {}


def smc_signal(df: pd.DataFrame) -> tuple[Direction, dict]:
    """Combine BOS/CHoCH + order block + sweep + FVG into a single SMC vote."""
    bos, bos_meta = detect_bos_choch(df)
    ob, ob_meta = detect_order_block(df)
    sweep, sweep_meta = detect_liquidity_sweep(df)
    fvg, fvg_meta = detect_fvg(df)

    bullish_hits = sum([
        bos == "bos_bullish", ob == "ob_bullish_retest",
        sweep == "sweep_low", fvg == "fvg_bullish",
    ])
    bearish_hits = sum([
        bos == "bos_bearish", ob == "ob_bearish_retest",
        sweep == "sweep_high", fvg == "fvg_bearish",
    ])

    meta = {"bos": bos, "order_block": ob, "sweep": sweep, "fvg": fvg,
             **bos_meta, **ob_meta, **sweep_meta, **fvg_meta}

    if bullish_hits >= 2 and bullish_hits > bearish_hits:
        return "long", meta
    if bearish_hits >= 2 and bearish_hits > bullish_hits:
        return "short", meta
    return "none", meta


def atr_filter_ok(df: pd.DataFrame, min_multiplier: float = 1.0, period: int = 20) -> bool:
    """
    From the weekly Claude review: skip entries when ATR is below its own
    rolling average, i.e. avoid trading during low-volatility chop.
    """
    a = atr(df, 14)
    current = a.iloc[-1]
    average = a.rolling(period).mean().iloc[-1]
    if pd.isna(current) or pd.isna(average):
        return True  # not enough data yet, don't block
    return current >= average * min_multiplier


def build_signal(df: pd.DataFrame, fast: int = 9, slow: int = 21) -> Signal:
    """
    Runs EMA + SMC and returns a partial Signal (ollama vote is added
    separately by ollama_veto.py, since that call is async/network-bound
    and shouldn't live in this pure-logic module).
    """
    ema_dir, ema_meta = ema_cross_signal(df, fast, slow)
    smc_dir, smc_meta = smc_signal(df)

    votes = {"ema": ema_dir, "smc": smc_dir}
    long_votes = sum(1 for v in votes.values() if v == "long")
    short_votes = sum(1 for v in votes.values() if v == "short")

    direction: Direction = "none"
    if long_votes > short_votes:
        direction = "long"
    elif short_votes > long_votes:
        direction = "short"

    reason_parts = [f"EMA {ema_dir}" if ema_dir != "none" else None,
                     f"SMC {smc_dir} ({smc_meta.get('bos','none')}/{smc_meta.get('order_block','none')})" if smc_dir != "none" else None]
    reason = ", ".join(p for p in reason_parts if p) or "no confluence"

    return Signal(
        direction=direction,
        votes=votes,
        vote_count=max(long_votes, short_votes),
        reason=reason,
        meta={**ema_meta, **smc_meta, "atr_ok": atr_filter_ok(df)},
    )
