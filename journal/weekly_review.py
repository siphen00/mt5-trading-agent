"""
Runs weekly (via .github/workflows/weekly-review.yml, or locally) for a
deeper diagnostic across the week's trades and daily journal notes.

Fully local via Ollama — no paid API, no API key needed. Uses a lower
temperature than the daily summary and (optionally) a bigger model, since
it only runs once a week and can afford to think a bit longer.

If your machine can handle a bigger model than qwen2.5:1.5b, set
OLLAMA_WEEKLY_MODEL to something like qwen2.5:7b for a noticeably sharper
weekly review — the daily one stays on the small model since it runs every
night and speed matters more there.
"""

import json
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADES_FILE = REPO_ROOT / "data" / "trades.json"
JOURNAL_FILE = REPO_ROOT / "data" / "journal.json"

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_WEEKLY_MODEL = os.getenv("OLLAMA_WEEKLY_MODEL", "qwen2.5:1.5b")

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
    return [e for e in entries if e.get("source") == "ollama-daily"
            and datetime.fromisoformat(e["date"]).date() >= cutoff]


def generate_review(trades: list[dict], daily_notes: list[dict]) -> str:
    if not trades:
        return "No trades this week — nothing to review yet."

    prompt = PROMPT_TEMPLATE.format(
        trades=json.dumps(trades, indent=2),
        daily_notes=json.dumps([n["summary"] for n in daily_notes], indent=2),
    )

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_WEEKLY_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.3}},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[weekly review generation failed: {e}] Trade count this week: {len(trades)}"


def append_journal_entry(summary: str, trade_count: int):
    try:
        with open(JOURNAL_FILE) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []

    entries.append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "source": "ollama-weekly",
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
