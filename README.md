# APEX Terminal — MT5 BTC scalping agent

Zero-cost automated BTC scalper (5m/15m) trading a demo MT5 account, with a
glassmorphism dashboard, full trade journaling, and a two-tier diagnostic
review — both tiers run fully local via Ollama, no paid API required.

## How it fits together

```
Your PC/VPS (Windows, MT5 terminal running)
  connector/mt5_connector.py  ← always-on loop, polls control/status.json,
                                  executes trades, writes data/*.json,
                                  pushes commits back to this repo

GitHub repo (this one)
  strategy/     EMA crossover + SMC (BOS/CHoCH/order blocks/FVGs/sweeps) voting engine
  journal/      daily_ollama.py + weekly_review.py — both local, both free
  data/         trades.json, equity.json, journal.json, strategy_state.json — the "database"
  control/      status.json — dashboard writes here, connector reads here
  dashboard/    static site → GitHub Pages, reads data/*.json live
  .github/workflows/  scheduled jobs for journaling + dashboard deploy
```

## Setup

### 1. Create the repo
Push this folder to a new GitHub repo (public, so raw.githubusercontent.com
and GitHub Pages work without extra auth for reads).

Replace `siphen00` below with your actual GitHub username if different — do not leave angle brackets in the URL, they'll break the push.

```bash
git init
git add .
git commit -m "Initial scaffold"
git branch -M main
git remote add origin https://github.com/siphen00/mt5-trading-agent.git
git push -u origin main
```

### 2. Enable GitHub Pages
Repo Settings → Pages → Source: **GitHub Actions**. The `deploy-dashboard.yml`
workflow will publish `dashboard/` automatically on every push.

### 3. Set up the MT5 machine (your PC or a Windows VPS)
```bash
git clone https://github.com/siphen00/mt5-trading-agent.git
cd mt5-trading-agent
pip install -r requirements.txt
cp .env.example .env    # fill in your MT5 demo login, server, path
```
Install [Ollama](https://ollama.com) and pull the model:
```bash
ollama pull qwen2.5:1.5b
```
Run the connector:
```bash
python -m connector.mt5_connector
```
This is the part that needs live debugging against your actual broker's demo
account — symbol names, filling modes, and margin requirements vary by broker,
so expect to iterate here.

### 4. Open the dashboard
Once Pages is live (Settings → Pages shows the URL), open it and click
**Set repo / token**:
- Owner + repo name — lets it read `data/*.json` (public, no token needed)
- A GitHub **fine-grained personal access token**, scoped to only this repo,
  with **Contents: read and write** — only needed if you want the power
  on/off toggle to work from the dashboard. Stored in your browser's
  localStorage only, never committed or sent anywhere but GitHub's API.

## Live charts

The dashboard now has two chart panels:
- **Live market** — a free TradingView widget showing real-time BTC/USDT
  from Binance's public feed. This is *general market context*, not your
  broker's exact quotes — TradingView's free widget can't access private
  broker feeds like Exness's.
- **Execution chart** — your bot's own M5/M15 candles (exported by the
  connector every cycle to `data/candles_M5.json` / `data/candles_M15.json`),
  with entry/exit markers plotted exactly where trades fired. Toggle between
  5m and 15m with the buttons in the panel header.

**Known gap:** the connector currently only records trades as `status: "open"`
— there's no position-close monitor yet, so `exit`, `exit_time`, and `pnl`
never get filled in. That means win rate, closed-trade P&L, and the
execution chart's exit markers won't show real numbers until that's built.
Worth doing next — happy to add a loop that polls open MT5 positions and
updates the trade record when one closes.

## Multiple strategies, sessions, and the backtest lab

The bot now runs one of six selectable strategies, each a complete standalone
signal generator (not just extra votes in one combined engine):

- **EMA + SMC** — trend confluence (the original strategy)
- **VWAP Reversion** — mean-reversion fade at VWAP band extremes, best in ranging conditions
- **BB Squeeze** — volatility contraction → breakout
- **RSI Divergence** — reversal on price/RSI divergence
- **ORB · London** / **ORB · New York** — opening range breakout, tied to a specific session open

Ollama remains a universal confirm/veto layer regardless of which strategy is
active — a trade only fires if Ollama agrees with the active strategy's signal.

**Sessions** (Asia / London / New York / London-NY Overlap / 24-7) gate *when*
the bot is allowed to trade, independent of which strategy is picked.

**Everything is switched from the dashboard**, no config file editing:
- The **Strategy control** panel on the live dashboard lets you pick a
  strategy and session with one click each — writes to
  `control/strategy_config.json`, which the connector reads every cycle.
- The **Backtest lab** (`dashboard/backtest.html`, linked from the nav bar)
  runs a full JS reimplementation of all six strategies *in your browser*
  against historical candles, shows win rate/profit factor/drawdown/equity
  curve/trade log, and has a **"Push to demo"** button that writes the
  tested strategy+session straight to the same control file — no manual step.

**The backtest lab needs historical data exported first.** GitHub Pages is
static — there's no server to compute a backtest against live MT5 history —
so the connector machine needs to export a data snapshot for the browser to
test against:

```powershell
python -m connector.export_backtest_data
```

This pulls 5000 candles per timeframe (~17 days of M5, ~52 days of M15 for
24/7 crypto) and pushes them to `data/backtest_M5.json` / `backtest_M15.json`.
Re-run it whenever you want fresher data — weekly is plenty for most purposes.
It's deliberately separate from the live loop so it doesn't bloat the repo
or slow down 15-second cycles.

**Known limitation worth knowing:** the JS strategy logic in `backtest.html`
is a faithful *port* of the Python logic in `strategy/strategies.py`, not
the same code running twice. If you ever tune a strategy's parameters in
Python, the backtest lab won't reflect that change until the same edit is
made in the JS version — they can drift out of sync if only one side gets
updated.

## Demo → live checklist

Don't flip `mode` to `live` on a whim. Before doing it, you want at minimum:
- A meaningful number of demo trading days behind you (weeks, not days)
- A maximum drawdown you've actually seen and are comfortable repeating with real money
- At least one weekly review (`data/journal.json`, source `ollama-weekly`) that didn't flag a live structural issue
- A kill switch you've actually tested (power off mid-trade, confirm it doesn't touch open positions)

I'd suggest building that as an explicit checklist in the dashboard rather
than a casual toggle — happy to add that next.

## What's next

- Backtest `strategy/signals.py` against historical MT5 data before going live
- Tune `ATR_MIN_MULTIPLIER` and `VOTES_REQUIRED` based on real demo results
- Consider a Telegram alert on every trade (you've already got that stack
  from the job-aggregation bot)
