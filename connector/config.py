"""
Central config for the MT5 connector.
Edit these values for your demo account and risk preferences.
Nothing here is secret — actual login credentials come from environment
variables (see .env.example) so they never get committed to git.
"""

import os

# --- MT5 account (loaded from environment, see .env.example) ---
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
MT5_PATH = os.getenv("MT5_PATH", "")  # path to terminal64.exe, optional

# --- Instrument & timeframes ---
SYMBOL = "BTCUSD"          # match your broker's exact symbol name, e.g. BTCUSD, BTCUSD.m
TIMEFRAMES = ["M5", "M15"]  # 5-minute and 15-minute scalping

# --- Risk management ---
RISK_PER_TRADE_PCT = 0.5     # % of equity risked per trade
MAX_CONCURRENT_TRADES = 2    # one per timeframe by default
MAX_DAILY_LOSS_PCT = 3.0     # kill switch: stop trading for the day past this
ATR_MIN_MULTIPLIER = 1.0     # skip entries when ATR is below this vs its 20-period average
                              # (this is the "low volatility chop" filter from the weekly review)

# --- Strategy voting ---
VOTES_REQUIRED = 2           # 2-of-3 voting engine: EMA cross, SMC structure, Ollama veto
EMA_FAST = 9
EMA_SLOW = 21

# --- Ollama (local LLM veto/confirm layer) ---
OLLAMA_MODEL = "qwen2.5:1.5b"
OLLAMA_HOST = "http://localhost:11434"

# --- Repo sync (connector pushes trade data back to GitHub) ---
REPO_PATH = os.getenv("REPO_PATH", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GIT_PUSH_RETRY_ATTEMPTS = 5   # matches the fetch/rebase retry pattern from the Binance bots
GIT_PUSH_RETRY_DELAY_SEC = 3

# --- Control loop ---
POLL_INTERVAL_SEC = 15        # how often the connector checks control/status.json and looks for signals
STATUS_FILE = os.path.join(REPO_PATH, "control", "status.json")
TRADES_FILE = os.path.join(REPO_PATH, "data", "trades.json")
EQUITY_FILE = os.path.join(REPO_PATH, "data", "equity.json")
JOURNAL_LOG_FILE = os.path.join(REPO_PATH, "data", "raw_trade_log.jsonl")  # append-only, feeds the journal scripts
