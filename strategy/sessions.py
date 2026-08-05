"""
Trading session windows, in UTC. Chosen by the dashboard, applied by the
connector so it only evaluates/trades during the selected window.

These are approximate, commonly-used session hours (not exchange-official
boundaries) — good enough for filtering, not for precision timing.
"""

from datetime import datetime, timezone

SESSIONS = {
    "24h": {"label": "24 Hours (no filter)", "start_hour": 0, "end_hour": 24},
    "asia": {"label": "Asia / Tokyo", "start_hour": 0, "end_hour": 9},
    "london": {"label": "London", "start_hour": 7, "end_hour": 16},
    "new_york": {"label": "New York", "start_hour": 12, "end_hour": 21},
    "london_ny_overlap": {"label": "London/NY Overlap", "start_hour": 12, "end_hour": 16},
}

DEFAULT_SESSION = "24h"


def in_session(session_id: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    session = SESSIONS.get(session_id, SESSIONS[DEFAULT_SESSION])
    start, end = session["start_hour"], session["end_hour"]
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # handles a window that wraps midnight
