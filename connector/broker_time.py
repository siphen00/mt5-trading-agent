"""
Broker timezone handling.

The bug this fixes
------------------
MT5 returns candle timestamps and tick times in the BROKER'S SERVER timezone,
not UTC. Exness servers typically run UTC+2 (winter) / UTC+3 (summer). The old
code did `pd.to_datetime(df["time"], unit="s", utc=True)` — i.e. it took server
seconds and *labelled* them UTC. Everything downstream that depends on the clock
was therefore shifted by the server offset:

  * strategy/sessions.py windows ("London" 07:00-16:00 UTC) selected the wrong
    hours of the day.
  * orb_signal()'s opening range started at the wrong candle.
  * The date used for "today" could roll over at the wrong moment.

Approach
--------
Detect the offset once per run by comparing a live server tick timestamp against
real UTC, then subtract it so every downstream consumer sees true UTC. Offsets
are rounded to the nearest half hour (some brokers use :30). The value can be
pinned in config if you'd rather not rely on detection.

Pure functions here so they're testable without a live terminal.
"""

from __future__ import annotations


def detect_offset_hours(server_epoch: float, true_utc_epoch: float) -> float:
    """
    Offset in hours between the broker's clock and real UTC.

    `server_epoch` is what MT5 hands back (e.g. symbol_info_tick().time), which
    is "seconds since epoch" computed against SERVER local time. `true_utc_epoch`
    is real UTC (time.time()). A UTC+3 server reads ~10800s ahead.

    Rounded to the nearest 0.5h so normal network/poll jitter (seconds) can't
    produce a bogus fractional offset.
    """
    raw_hours = (server_epoch - true_utc_epoch) / 3600.0
    return round(raw_hours * 2) / 2


def to_utc_epoch(server_epoch: float, offset_hours: float) -> float:
    """Convert one broker-clock timestamp to true UTC."""
    return server_epoch - offset_hours * 3600.0


def normalise_candle_times(times, offset_hours: float):
    """
    Convert a sequence/Series of broker-clock candle times to true UTC.
    Works with a pandas Series or a plain list.
    """
    shift = offset_hours * 3600.0
    try:
        return times - shift  # pandas Series / numpy array
    except TypeError:
        return [t - shift for t in times]


def bar_seconds(timeframe_str: str) -> int:
    """Bar length in seconds, used for the new-bar gate."""
    table = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
    try:
        return table[timeframe_str]
    except KeyError:
        raise ValueError(f"unknown timeframe {timeframe_str!r}") from None
