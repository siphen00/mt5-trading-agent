"""
Local LLM confirm/veto layer, using Ollama (qwen2.5:1.5b by default — matches
what you're already running for the Binance bots). This is the third vote
in the 2-of-3 engine: EMA cross, SMC structure, Ollama sanity-check.

Runs fully local, zero cost. Only the weekly deep-dive review (journal/weekly_claude.py)
touches the paid Claude API.
"""

import json
import requests
from strategy.signals import Signal
from connector.config import OLLAMA_HOST, OLLAMA_MODEL


PROMPT_TEMPLATE = """You are a risk-aware trading assistant reviewing a candidate BTC trade.

Candidate direction: {direction}
EMA signal: {ema_vote}
SMC structure signal: {smc_vote}
SMC details: {smc_meta}
ATR filter passed: {atr_ok}

Respond with ONLY one word: CONFIRM if this looks like a reasonable setup,
or VETO if the confluence looks weak or contradictory. No explanation."""


def get_ollama_vote(signal: Signal, timeout: int = 8) -> str:
    """
    Returns 'long', 'short', or 'none'.
    On any failure (Ollama not running, timeout, bad response) this fails
    SAFE by returning 'none' — a missing vote should never itself cause a
    trade to fire, only prevent one.
    """
    if signal.direction == "none":
        return "none"

    prompt = PROMPT_TEMPLATE.format(
        direction=signal.direction,
        ema_vote=signal.votes.get("ema"),
        smc_vote=signal.votes.get("smc"),
        smc_meta=json.dumps({k: v for k, v in signal.meta.items() if k not in ("atr_ok",)}),
        atr_ok=signal.meta.get("atr_ok"),
    )

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip().upper()

        if "CONFIRM" in text:
            return signal.direction
        return "none"  # VETO or unparseable -> no vote, not a hard block

    except Exception as e:
        print(f"[ollama_veto] Ollama unreachable, skipping veto layer: {e}")
        return "none"
