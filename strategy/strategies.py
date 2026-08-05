"""
Strategy library — multiple selectable scalping/day-trading strategies.

Every strategy function takes a candle DataFrame and returns a Signal
(from strategy.signals), so the connector can plug any of them into the
same execution/voting/journaling pipeline interchangeably. Which one runs
is chosen by the dashboard (writes control/strategy_config.json), not
hardcoded — see connector/mt5_connector.py for how it's read and applied.

These are standard, widely-documented technical approaches, not proprietary
edges — treat them as starting points to backtest and tune, not guaranteed
profitable systems. Test each one on the backtest page before trusting it
with even demo capital for real.
"""

import pandas as pd
import numpy as np
from strategy.signals import Signal, Direction, atr, rsi, atr_filter_ok, build_signal as ema_smc_signal


def _volume_spike(df: pd.DataFrame, window: int = 20, mult: float = 1.3) -> bool:
    vol_avg = df["volume"].rolling(window).mean().iloc[-1]
    vol_now = df["volume"].iloc[-1]
    return bool(pd.notna(vol_avg) and vol_now > vol_avg * mult)


def vwap_reversion_signal(df: pd.DataFrame, window: int = 50, band_mult: float = 2.0) -> Signal:
    """
    Mean-reversion scalp: price stretches away from a rolling VWAP and
    snaps back inside the band. Suits range-bound/choppy conditions —
    tends to underperform in strong trends, since it fades momentum.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical * df["volume"]).rolling(window).sum() / df["volume"].rolling(window).sum()
    dev = (typical - vwap).rolling(window).std()
    upper = vwap + dev * band_mult
    lower = vwap - dev * band_mult

    close = df["close"].iloc[-1]
    vwap_now = vwap.iloc[-1]

    band_vote: Direction = "none"
    if pd.notna(lower.iloc[-2]) and df["low"].iloc[-2] < lower.iloc[-2] and close > lower.iloc[-1]:
        band_vote = "long"
    elif pd.notna(upper.iloc[-2]) and df["high"].iloc[-2] > upper.iloc[-2] and close < upper.iloc[-1]:
        band_vote = "short"

    votes = {"vwap_band": band_vote, "volume": band_vote if _volume_spike(df) else "none"}
    reason = f"VWAP band reversion ({band_vote})" if band_vote != "none" else "no confluence"
    current_atr = atr(df, 14).iloc[-1]

    return Signal(
        direction=band_vote, votes=votes,
        vote_count=sum(1 for v in votes.values() if v == band_vote),
        reason=reason,
        meta={"vwap": round(float(vwap_now), 2) if pd.notna(vwap_now) else None,
              "atr": float(current_atr) if pd.notna(current_atr) else None,
              "atr_ok": atr_filter_ok(df)},
    )


def bb_squeeze_signal(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0, lookback: int = 60) -> Signal:
    """
    Volatility breakout: waits for a Bollinger Band squeeze (width in the
    bottom 20% of its recent range) then trades the breakout candle out of
    that squeeze. Better suited to day-trading style setups than
    constant scalping — it fires rarely by design.
    """
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = mid + std * std_mult
    lower = mid - std * std_mult
    width = (upper - lower) / mid

    current_width = width.iloc[-1]
    recent_width = width.iloc[-lookback:]
    squeeze = bool(pd.notna(current_width) and current_width <= recent_width.quantile(0.2))

    close = df["close"].iloc[-1]
    body = abs(df["close"].iloc[-1] - df["open"].iloc[-1])
    avg_body = (df["close"] - df["open"]).abs().rolling(20).mean().iloc[-1]
    strong_body = bool(pd.notna(avg_body) and body > avg_body * 1.2)

    breakout: Direction = "none"
    if squeeze and strong_body:
        if close > upper.iloc[-1]:
            breakout = "long"
        elif close < lower.iloc[-1]:
            breakout = "short"

    votes = {"bb_breakout": breakout, "volume": breakout if _volume_spike(df) else "none"}
    reason = f"BB squeeze breakout ({breakout})" if breakout != "none" else "no squeeze/breakout"
    current_atr = atr(df, 14).iloc[-1]

    return Signal(
        direction=breakout, votes=votes,
        vote_count=sum(1 for v in votes.values() if v == breakout),
        reason=reason,
        meta={"bb_width": round(float(current_width), 5) if pd.notna(current_width) else None,
              "squeeze": squeeze,
              "atr": float(current_atr) if pd.notna(current_atr) else None,
              "atr_ok": atr_filter_ok(df)},
    )


def rsi_divergence_signal(df: pd.DataFrame, period: int = 14, lookback: int = 20) -> Signal:
    """
    Reversal scalp: RSI making a higher low while price makes a lower low
    (bullish divergence), or the mirror for bearish. A classic reversal
    tell — best used against overextended moves, not as a trend-follower.
    """
    r = rsi(df["close"], period)
    window = df.iloc[-lookback:]
    r_window = r.iloc[-lookback:]

    divergence: Direction = "none"
    lows_sorted = window["low"].nsmallest(2)
    if len(lows_sorted) == 2 and lows_sorted.index[0] != lows_sorted.index[1]:
        i1, i2 = sorted([lows_sorted.index[0], lows_sorted.index[1]])
        if window.loc[i2, "low"] < window.loc[i1, "low"] and r_window.loc[i2] > r_window.loc[i1]:
            divergence = "long"

    if divergence == "none":
        highs_sorted = window["high"].nlargest(2)
        if len(highs_sorted) == 2 and highs_sorted.index[0] != highs_sorted.index[1]:
            i1, i2 = sorted([highs_sorted.index[0], highs_sorted.index[1]])
            if window.loc[i2, "high"] > window.loc[i1, "high"] and r_window.loc[i2] < r_window.loc[i1]:
                divergence = "short"

    rsi_now = r.iloc[-1]
    rsi_extreme: Direction = "none"
    if pd.notna(rsi_now):
        if rsi_now < 35:
            rsi_extreme = "long"
        elif rsi_now > 65:
            rsi_extreme = "short"

    votes = {"rsi_divergence": divergence, "rsi_level": rsi_extreme if rsi_extreme == divergence else "none"}
    reason = (f"RSI divergence ({divergence}, RSI={round(rsi_now,1)})" if divergence != "none" and pd.notna(rsi_now)
              else "no divergence")
    current_atr = atr(df, 14).iloc[-1]

    return Signal(
        direction=divergence, votes=votes,
        vote_count=sum(1 for v in votes.values() if v == divergence),
        reason=reason,
        meta={"rsi": round(float(rsi_now), 2) if pd.notna(rsi_now) else None,
              "atr": float(current_atr) if pd.notna(current_atr) else None,
              "atr_ok": atr_filter_ok(df)},
    )


def orb_signal(df: pd.DataFrame, session_start_hour: int = 7, range_candles: int = 3) -> Signal:
    """
    Opening Range Breakout — a day-trading classic. Defines a price range
    from the first few candles after a session opens, then trades a
    breakout beyond that range. Most meaningful when paired with the
    matching session filter (e.g. this + the London session) — running it
    with no session filter means "opening range" is measured from whatever
    UTC hour you set here, regardless of which session is actually active.
    """
    df = df.copy()
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    today = df["dt"].iloc[-1].date()
    session_start = pd.Timestamp(today, tz="UTC") + pd.Timedelta(hours=session_start_hour)
    todays_session = df[df["dt"] >= session_start]

    if len(todays_session) <= range_candles:
        return Signal(direction="none", votes={"orb": "none"}, vote_count=0,
                       reason="waiting for opening range to form",
                       meta={"atr_ok": atr_filter_ok(df)})

    opening_range = todays_session.iloc[:range_candles]
    range_high, range_low = opening_range["high"].max(), opening_range["low"].min()
    latest = todays_session.iloc[-1]

    # Only fire on the candle that FIRST crosses the range — otherwise every
    # candle that remains above/below it re-fires the same "breakout" for
    # the rest of the session, which inflates trade count and isn't how ORB
    # is meant to work (one entry per breakout, not a standing bias).
    prev_close = (todays_session.iloc[-2]["close"] if len(todays_session) >= range_candles + 2
                  else opening_range.iloc[-1]["close"])

    breakout: Direction = "none"
    if latest["close"] > range_high and prev_close <= range_high:
        breakout = "long"
    elif latest["close"] < range_low and prev_close >= range_low:
        breakout = "short"

    votes = {"orb": breakout, "volume": breakout if _volume_spike(df) else "none"}
    reason = (f"ORB breakout ({breakout}), range {round(range_low,1)}-{round(range_high,1)}"
              if breakout != "none" else "inside opening range")
    current_atr = atr(df, 14).iloc[-1]

    return Signal(
        direction=breakout, votes=votes,
        vote_count=sum(1 for v in votes.values() if v == breakout),
        reason=reason,
        meta={"range_high": round(float(range_high), 2), "range_low": round(float(range_low), 2),
              "atr": float(current_atr) if pd.notna(current_atr) else None,
              "atr_ok": atr_filter_ok(df)},
    )


# Registry the connector and dashboard both key off of. IDs here must match
# the strategy IDs the dashboard writes into control/strategy_config.json.
STRATEGIES = {
    "ema_smc": lambda df: ema_smc_signal(df, 9, 21),
    "vwap_reversion": vwap_reversion_signal,
    "bb_squeeze": bb_squeeze_signal,
    "rsi_divergence": rsi_divergence_signal,
    "orb_london": lambda df: orb_signal(df, session_start_hour=7),
    "orb_new_york": lambda df: orb_signal(df, session_start_hour=12),
}

DEFAULT_STRATEGY = "ema_smc"
