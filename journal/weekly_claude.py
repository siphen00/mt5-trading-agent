"""
Runs weekly (via .github/workflows/weekly-review.yml, using the
ANTHROPIC_API_KEY repo secret) for a deeper diagnostic across the week's
trades and daily Ollama journals — pattern recognition that a 1.5B local
model isn't well suited for.

This is the only part of the system that costs money, and it's roughly
one API call per week — a handful of cents at most, depending on trade volume.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADES_FILE = REPO_ROOT / "data" / "trades.json"
JOURNAL_FILE = REPO_ROOT / "data" / "journal.json"

PROMPT_TEMPLATE = """You are a trading performance analyst reviewing one week of an
automated BTC scalping agent's activity. The agent trades 5m and 15m timeframes
using an EMA crossover + Smart Money Concepts (BOS/CHoCH, order blocks, FVGs,
liquidity sweeps) voting system, with a local LLM veto layer.

This week's trades:
{trades}

This week's daily diagnostic notes:
{daily_notes}

Write a diagnostic review (6-10 sentences) that:
- Identifies any pattern in what's winning vs losing (by timeframe, signal type, session/time of day)
- Flags anything that looks like it's drifting or degrading week over week
- Gives 1-3 concrete, testable suggestions for tuning the strategy or filters

Be specific and grounded in the data given — do not speculate beyond it."""


def load_week_trades() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    return [t for t in trades if datetime.fromisoformat(t["time"]) >= cutoff]


def load_week_daily_notes() -> list[dict]:
    if not JOURNAL_FILE.exists():
        return []
    with open(JOURNAL_FILE) as f:
        entries = json.load(f)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=7)
    return [e for e in entries if e.get("source") == "ollama"
            and datetime.fromisoformat(e["date"]).date() >= cutoff]


def generate_review(trades: list[dict], daily_notes: list[dict]) -> str:
    if not trades:
        return "No trades this week — nothing to review yet."

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = PROMPT_TEMPLATE.format(
        trades=json.dumps(trades, indent=2),
        daily_notes=json.dumps([n["summary"] for n in daily_notes], indent=2),
    )
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def append_journal_entry(summary: str, trade_count: int):
    try:
        with open(JOURNAL_FILE) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []

    entries.append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "source": "claude",
        "trade_count": trade_count,
        "summary": summary,
    })
    with open(JOURNAL_FILE, "w") as f:
        json.dump(entries[-180:], f, indent=2)


if __name__ == "__main__":
    trades = load_week_trades()
    daily_notes = load_week_daily_notes()
    review = generate_review(trades, daily_notes)
    append_journal_entry(review, len(trades))
    print(review)
