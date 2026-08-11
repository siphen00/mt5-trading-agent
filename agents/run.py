"""
Agent runner — schedules the news and daily-bias agents, then commits results.

Run on the connector machine (it needs MT5 for higher-timeframe candles):

    python -m agents.run              # loop forever
    python -m agents.run --once       # single pass, useful for testing
    python -m agents.run --no-bias    # news only (no MT5 needed)

Deliberately a SEPARATE PROCESS from the trading connector. News feeds are slow
and flaky; a hung HTTP request must never stall or crash the trading loop. The
two only meet in git.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timezone

from agents import news_agent, bias_agent

NEWS_INTERVAL_SEC = 300      # 5 min — fast enough for breaking headlines
BIAS_INTERVAL_SEC = 1800     # 30 min — HTF bias moves slowly; no point hammering it

NEWS_PATH = "data/news.json"
BIAS_PATH = "data/daily_bias.json"


def get_htf_candles():
    """Pull D1 + H4 candles from MT5. Returns (d1, h4) or (None, None)."""
    try:
        from connector import config
        from connector.mt5_connector import connect, get_candles
    except ImportError as e:
        print(f"[agents] connector unavailable ({e}); skipping bias")
        return None, None
    try:
        if not connect():
            print("[agents] MT5 connect failed; skipping bias")
            return None, None
        d1 = get_candles(config.SYMBOL, "D1", count=400).to_dict("records")
        h4 = get_candles(config.SYMBOL, "H4", count=400).to_dict("records")
        return d1, h4
    except Exception as e:                     # noqa: BLE001
        print(f"[agents] candle fetch failed: {e}")
        return None, None


def commit(paths):
    try:
        from connector.git_sync import commit_and_push
    except ImportError:
        print("[agents] git_sync unavailable; files written locally only")
        return False
    return commit_and_push(paths, f"Agents update {datetime.now(timezone.utc).isoformat()}")


def cycle(do_news=True, do_bias=True, use_ollama=False):
    written = []
    if do_news:
        try:
            news_agent.run_once(NEWS_PATH)
            written.append(NEWS_PATH)
        except Exception:                      # noqa: BLE001
            print("[agents] news agent failed:"); traceback.print_exc()
    if do_bias:
        try:
            d1, h4 = get_htf_candles()
            if d1 and h4:
                bias_agent.run_once(d1, h4, BIAS_PATH, use_ollama=use_ollama)
                written.append(BIAS_PATH)
        except Exception:                      # noqa: BLE001
            print("[agents] bias agent failed:"); traceback.print_exc()
    if written:
        commit(written)
    return written


def main():
    p = argparse.ArgumentParser(description="APEX news + daily bias agents")
    p.add_argument("--once", action="store_true", help="run a single pass and exit")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--no-bias", action="store_true")
    p.add_argument("--ollama", action="store_true", help="let Ollama rephrase the bias rationale")
    args = p.parse_args()

    do_news, do_bias = not args.no_news, not args.no_bias
    if args.once:
        cycle(do_news, do_bias, args.ollama)
        return

    print(f"[agents] starting — news every {NEWS_INTERVAL_SEC}s, bias every {BIAS_INTERVAL_SEC}s")
    last_news = last_bias = 0.0
    while True:
        try:
            now = time.time()
            run_news = do_news and (now - last_news >= NEWS_INTERVAL_SEC)
            run_bias = do_bias and (now - last_bias >= BIAS_INTERVAL_SEC)
            if run_news or run_bias:
                cycle(run_news, run_bias, args.ollama)
                if run_news: last_news = now
                if run_bias: last_bias = now
            time.sleep(15)
        except KeyboardInterrupt:
            print("\n[agents] stopped"); sys.exit(0)
        except Exception:                      # noqa: BLE001 - never die on one bad cycle
            traceback.print_exc(); time.sleep(30)


if __name__ == "__main__":
    main()
