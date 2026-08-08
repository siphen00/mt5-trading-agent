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
SYMBOL = "BTCUSDm"          # match your broker's exact symbol name, e.g. BTCUSD, BTCUSD.m
TIMEFRAMES = ["M5", "M15"]  # 5-minute and 15-minute scalping

# --- Risk management ---
RISK_PER_TRADE_PCT = 0.5     # % of equity risked per trade
MAX_CONCURRENT_TRADES = 2    # one per timeframe by default
MAX_DAILY_LOSS_PCT = 3.0     # kill switch: stop trading for the day past this.
                              # NOW ENFORCED in run_cycle via connector/risk.py —
                              # previously this value existed in config and was
                              # referenced nowhere else in the codebase.
ATR_MIN_MULTIPLIER = 1.0     # skip entries when ATR is below this vs its 20-period average
                              # (this is the "low volatility chop" filter from the weekly review)

# Hard ceiling on the risk a single trade may actually carry. The broker's
# minimum lot (0.01) can force far more risk than RISK_PER_TRADE_PCT asks for on
# a small account: at ~$78 equity with a ~$300 ATR stop, 0.5% intends ~$0.39 of
# risk but the minimum lot risks ~$3, i.e. ~4%. Trades exceeding this ceiling are
# REFUSED with a loud log line rather than silently taken oversized.
MAX_EFFECTIVE_RISK_PCT = 1.5

# Spread gate: skip entries when the spread is a large fraction of ATR, since
# the trade then has to beat the broker before it beats the market.
MAX_SPREAD_ATR_FRACTION = 0.15   # e.g. skip if spread > 15% of ATR
SPREAD_STOP_MULTIPLE = 3.0       # stop must be at least this many spreads wide

# Broker clock. MT5 reports candle/tick times in SERVER time, not UTC (Exness is
# typically UTC+2/+3). Leave as None to auto-detect on startup by comparing a
# live tick against real UTC; set a number to pin it (e.g. 3.0 for UTC+3).
BROKER_UTC_OFFSET_HOURS = None

# --- Strategy voting ---
VOTES_REQUIRED = 2           # deprecated — kept for reference; trade confirmation is now
                              # "does Ollama agree with whichever strategy is active" (see
                              # run_cycle in mt5_connector.py), not a fixed vote count, since
                              # different strategies have different internal vote structures
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
STRATEGY_CONFIG_FILE = os.path.join(REPO_PATH, "control", "strategy_config.json")
TRADES_FILE = os.path.join(REPO_PATH, "data", "trades.json")
OPEN_POSITIONS_FILE = os.path.join(REPO_PATH, "data", "open_positions.json")
BOT_MAGIC = 20260802  # tags every order this bot places — used to tell bot trades apart
                       # from anything you place manually in the MT5 terminal
EQUITY_FILE = os.path.join(REPO_PATH, "data", "equity.json")
JOURNAL_LOG_FILE = os.path.join(REPO_PATH, "data", "raw_trade_log.jsonl")  # append-only, feeds the journal scripts
