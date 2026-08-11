"""
Daily bias agent — higher-timeframe read on BTC with a written rationale.

What it actually does
---------------------
1. SWING STRUCTURE (the primary signal). Finds pivot highs/lows on D1 and H4 by
   fractal comparison, then classifies the last two swings:
       higher highs + higher lows  -> bullish
       lower highs  + lower lows   -> bearish
       anything mixed              -> ranging
2. CONFLUENCE. Checks price vs the 20/50/200 SMA stack, and RSI(14) regime.
3. Combines them into a weighted score -> bias + confidence, and writes a plain
   rationale naming the actual numbers that produced it.

On the rationale text
---------------------
It's generated deterministically from the computed facts, NOT written by an LLM.
That's a deliberate choice: the rationale's whole job is to state *why* the bias
is what it is, so it must be exactly faithful to the numbers. A small local model
paraphrasing them would add fluency and a risk of quiet contradiction. Ollama can
still be layered on afterwards for phrasing (see polish_with_ollama), but the
facts and the bias never depend on it.

Everything here is a pure function over candle lists, so it's testable without
MT5 or a network.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

Candle = Dict[str, float]   # {time, open, high, low, close, volume}


# ---------------------------------------------------------------------------
# Indicators (stdlib only — mirrors the JS engine's definitions)
# ---------------------------------------------------------------------------

def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= period:
            run -= values[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0.0) / period
        loss += max(-d, 0.0) / period
    rs = float("inf") if loss == 0 else gain / loss
    out[period] = 100 - 100 / (1 + rs)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = (gain * (period - 1) + max(d, 0.0)) / period
        loss = (loss * (period - 1) + max(-d, 0.0)) / period
        rs = float("inf") if loss == 0 else gain / loss
        out[i] = 100 - 100 / (1 + rs)
    return out


# ---------------------------------------------------------------------------
# Swing structure
# ---------------------------------------------------------------------------

@dataclass
class Swing:
    kind: str      # "high" | "low"
    price: float
    index: int
    time: int


def find_swings(candles: List[Candle], left: int = 2, right: int = 2) -> List[Swing]:
    """
    Fractal pivots: a swing high is a bar whose high exceeds `left` bars before
    and `right` bars after. `right` bars of lag is the price of not repainting —
    a pivot isn't confirmed until enough bars have closed after it.
    """
    swings: List[Swing] = []
    n = len(candles)
    for i in range(left, n - right):
        hi, lo = candles[i]["high"], candles[i]["low"]
        if all(hi > candles[j]["high"] for j in range(i - left, i)) and \
           all(hi > candles[j]["high"] for j in range(i + 1, i + right + 1)):
            swings.append(Swing("high", hi, i, int(candles[i]["time"])))
        if all(lo < candles[j]["low"] for j in range(i - left, i)) and \
           all(lo < candles[j]["low"] for j in range(i + 1, i + right + 1)):
            swings.append(Swing("low", lo, i, int(candles[i]["time"])))
    swings.sort(key=lambda s: s.index)
    return swings


def structure_from_swings(swings: List[Swing]) -> Tuple[str, str]:
    """
    Classify the last two confirmed highs and lows.
    Returns (bias, human description).
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "ranging", "not enough confirmed swings to read structure"

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        return "bullish", (f"higher high ({highs[-2].price:,.0f} → {highs[-1].price:,.0f}) "
                           f"and higher low ({lows[-2].price:,.0f} → {lows[-1].price:,.0f})")
    if lh and ll:
        return "bearish", (f"lower high ({highs[-2].price:,.0f} → {highs[-1].price:,.0f}) "
                           f"and lower low ({lows[-2].price:,.0f} → {lows[-1].price:,.0f})")
    if hh and ll:
        return "ranging", "expanding range — higher high but also lower low"
    return "ranging", (f"mixed structure — last high {'up' if hh else 'down'}, "
                       f"last low {'up' if hl else 'down'}")


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------

def ma_confluence(closes: List[float]) -> Tuple[str, str, Dict[str, Optional[float]]]:
    """Price vs the 20/50/200 SMA stack."""
    m20, m50, m200 = sma(closes, 20)[-1], sma(closes, 50)[-1], sma(closes, 200)[-1]
    price = closes[-1]
    vals = {"sma20": m20, "sma50": m50, "sma200": m200, "price": price}
    have = [m for m in (m20, m50, m200) if m is not None]
    if not have:
        return "ranging", "no moving averages available yet", vals

    above = sum(1 for m in have if price > m)
    below = len(have) - above
    stacked_up = m20 is not None and m50 is not None and m200 is not None and m20 > m50 > m200
    stacked_dn = m20 is not None and m50 is not None and m200 is not None and m20 < m50 < m200

    if stacked_up and above == len(have):
        return "bullish", f"price {price:,.0f} above a bullish 20/50/200 stack", vals
    if stacked_dn and below == len(have):
        return "bearish", f"price {price:,.0f} below a bearish 20/50/200 stack", vals
    if above == len(have):
        return "bullish", f"price {price:,.0f} above all {len(have)} moving averages", vals
    if below == len(have):
        return "bearish", f"price {price:,.0f} below all {len(have)} moving averages", vals
    return "ranging", f"price {price:,.0f} tangled in the moving averages ({above} above, {below} below)", vals


def rsi_state(closes: List[float], period: int = 14) -> Tuple[str, str, Optional[float]]:
    r = rsi(closes, period)[-1]
    if r is None:
        return "ranging", "RSI unavailable (not enough history)", None
    if r >= 70:
        return "bearish", f"RSI {r:.0f} is overbought — stretched, prone to a pullback", r
    if r <= 30:
        return "bullish", f"RSI {r:.0f} is oversold — stretched, prone to a bounce", r
    if r >= 55:
        return "bullish", f"RSI {r:.0f} sits in bullish territory", r
    if r <= 45:
        return "bearish", f"RSI {r:.0f} sits in bearish territory", r
    return "ranging", f"RSI {r:.0f} is neutral", r


# ---------------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------------

WEIGHTS = {"d1_structure": 3.0, "h4_structure": 2.0, "ma": 2.0, "rsi": 1.0}
_SIGN = {"bullish": 1.0, "bearish": -1.0, "ranging": 0.0}


@dataclass
class BiasResult:
    bias: str
    confidence: int
    score: float
    rationale: str
    factors: List[Dict[str, Any]] = field(default_factory=list)
    levels: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_bias(d1: List[Candle], h4: List[Candle]) -> BiasResult:
    """
    Combine D1 + H4 structure with MA and RSI confluence on D1.
    Score runs -8..+8; confidence is |score| normalised to 0-100.
    """
    if len(d1) < 30:
        return BiasResult("ranging", 0, 0.0,
                          "Not enough daily history to form a bias.",
                          [], {}, datetime.now(timezone.utc).isoformat())

    d1_closes = [c["close"] for c in d1]

    d1_bias, d1_desc = structure_from_swings(find_swings(d1))
    h4_bias, h4_desc = (structure_from_swings(find_swings(h4)) if len(h4) >= 30
                        else ("ranging", "not enough 4-hour history"))
    ma_bias, ma_desc, ma_vals = ma_confluence(d1_closes)
    r_bias, r_desc, r_val = rsi_state(d1_closes)

    factors = [
        {"name": "Daily structure", "bias": d1_bias, "detail": d1_desc, "weight": WEIGHTS["d1_structure"]},
        {"name": "4-hour structure", "bias": h4_bias, "detail": h4_desc, "weight": WEIGHTS["h4_structure"]},
        {"name": "Moving averages", "bias": ma_bias, "detail": ma_desc, "weight": WEIGHTS["ma"]},
        {"name": "RSI", "bias": r_bias, "detail": r_desc, "weight": WEIGHTS["rsi"]},
    ]
    score = sum(_SIGN[f["bias"]] * f["weight"] for f in factors)
    max_score = sum(WEIGHTS.values())

    if score >= 3:
        bias = "bullish"
    elif score <= -3:
        bias = "bearish"
    else:
        bias = "ranging"
    confidence = int(round(min(abs(score) / max_score, 1.0) * 100))

    return BiasResult(
        bias=bias, confidence=confidence, score=round(score, 2),
        rationale=write_rationale(bias, confidence, factors, ma_vals, r_val, d1),
        factors=factors,
        levels=key_levels(d1, ma_vals),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def key_levels(d1: List[Candle], ma_vals: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """Nearest confirmed swing above/below price, plus the MA values."""
    swings = find_swings(d1)
    price = d1[-1]["close"]
    highs = [s.price for s in swings if s.kind == "high" and s.price > price]
    lows = [s.price for s in swings if s.kind == "low" and s.price < price]
    return {
        "price": round(price, 2),
        "resistance": round(min(highs), 2) if highs else None,
        "support": round(max(lows), 2) if lows else None,
        "sma20": round(ma_vals["sma20"], 2) if ma_vals.get("sma20") else None,
        "sma50": round(ma_vals["sma50"], 2) if ma_vals.get("sma50") else None,
        "sma200": round(ma_vals["sma200"], 2) if ma_vals.get("sma200") else None,
        "day_change_pct": round((d1[-1]["close"] / d1[-2]["close"] - 1) * 100, 2) if len(d1) >= 2 else None,
    }


def write_rationale(bias: str, confidence: int, factors: List[Dict[str, Any]],
                    ma_vals: Dict[str, Optional[float]], rsi_val: Optional[float],
                    d1: List[Candle]) -> str:
    """Plain-English explanation built strictly from the computed factors."""
    agree = [f for f in factors if f["bias"] == bias and bias != "ranging"]
    against = [f for f in factors if f["bias"] != bias and f["bias"] != "ranging"]

    price = d1[-1]["close"]
    chg = (d1[-1]["close"] / d1[-2]["close"] - 1) * 100 if len(d1) >= 2 else 0.0

    if bias == "bullish":
        head = f"Bias is BULLISH ({confidence}% confidence). BTC at {price:,.0f}, {chg:+.2f}% on the day."
    elif bias == "bearish":
        head = f"Bias is BEARISH ({confidence}% confidence). BTC at {price:,.0f}, {chg:+.2f}% on the day."
    else:
        head = (f"Bias is RANGING ({confidence}% confidence) — signals conflict. "
                f"BTC at {price:,.0f}, {chg:+.2f}% on the day.")

    parts = [head]
    if agree:
        parts.append("Supporting it: " + "; ".join(f["detail"] for f in agree) + ".")
    if against:
        parts.append("Cutting against it: " + "; ".join(f"{f['name'].lower()} is {f['bias']} — {f['detail']}"
                                                       for f in against) + ".")
    if bias == "ranging":
        parts.append("With structure and confluence disagreeing, treat breakouts sceptically "
                     "and expect mean reversion until one side resolves.")
    elif confidence < 50:
        parts.append("Confidence is low, so this is a lean rather than a conviction call.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Optional Ollama phrasing pass (facts never depend on it)
# ---------------------------------------------------------------------------

def polish_with_ollama(result: BiasResult, host: str = "http://localhost:11434",
                       model: str = "qwen2.5:1.5b", timeout: int = 30) -> BiasResult:
    """
    Rewrite the rationale more naturally WITHOUT changing the bias, confidence,
    or any number. If the model is unreachable or contradicts the computed bias,
    the deterministic text is kept.
    """
    import urllib.request

    prompt = (
        "Rewrite this market note in 2-3 natural sentences for a trader. "
        "Do NOT change the direction, the confidence, or any number. "
        "Do not add predictions or advice.\n\n" + result.rationale
    )
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                             "options": {"temperature": 0.2}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = json.loads(resp.read().decode()).get("response", "").strip()
    except Exception:                          # noqa: BLE001
        return result

    # Reject a rewrite that flips the direction — the guard that makes this safe.
    opposite = {"bullish": "bearish", "bearish": "bullish"}.get(result.bias)
    low = text.lower()
    if not text or (opposite and opposite in low and result.bias not in low):
        return result
    result.rationale = text
    return result


def run_once(d1: List[Candle], h4: List[Candle], output_path: str = "data/daily_bias.json",
             use_ollama: bool = False) -> Dict[str, Any]:
    result = build_bias(d1, h4)
    if use_ollama:
        result = polish_with_ollama(result)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    payload = result.to_dict()
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[bias] {result.bias.upper()} @ {result.confidence}% (score {result.score})")
    return payload
