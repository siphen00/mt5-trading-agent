"""
Runs nightly (via .github/workflows/daily-journal.yml or a local cron/Task
Scheduler job) to produce a plain-language diagnostic of the day's trading —
this is the "why did it trade like that" layer you can read every morning.

Cost: $0 — runs entirely on Ollama.
"""

import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_LOG = REPO_ROOT / "data" / "raw_trade_log.jsonl"
JOURNAL_OUT = REPO_ROOT / "data" / "journal.json"
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:1.5b"

PROMPT_TEMPLATE = """You are reviewing today's automated BTC trading activity to help
the trader understand and improve the strategy. Be specific and concise (4-6 sentences).

Today's trades (JSON):
{trades}

Write a short diagnostic summary covering:
- Overall result (count, win/loss, net P&L)
- Which signal combination(s) worked or didn't
- Any pattern worth flagging (e.g. a filter being ignored, a session that underperformed)

Do not repeat the raw numbers verbatim, synthesize them into an assessment."""


def load_todays_trades() -> list[dict]:
    if not JOURNAL_LOG.exists():
        return []
    today = datetime.now(timezone.utc).date()
    trades = []
    with open(JOURNAL_LOG) as f:
        for line in f:
            try:
                t = json.loads(line)
                t_date = datetime.fromisoformat(t["time"]).date()
                if t_date == today:
                    trades.append(t)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return trades


def summarize(trades: list[dict]) -> str:
    if not trades:
        return "No trades today — either the agent was powered off or no signals met the 2-of-3 vote threshold."

    prompt = PROMPT_TEMPLATE.format(trades=json.dumps(trades, indent=2))
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[journal generation failed: {e}] Raw trade count: {len(trades)}"


def append_journal_entry(summary: str, trade_count: int):
    try:
        with open(JOURNAL_OUT) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []

    entries.append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "source": "ollama-daily",
        "trade_count": trade_count,
        "summary": summary,
    })
    with open(JOURNAL_OUT, "w") as f:
        json.dump(entries[-180:], f, indent=2)  # keep ~6 months


if __name__ == "__main__":
    trades = load_todays_trades()
    summary = summarize(trades)
    append_journal_entry(summary, len(trades))
    print(summary)
