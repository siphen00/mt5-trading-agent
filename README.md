# APEX Terminal — MT5 BTC scalping agent

Zero-cost automated BTC scalper (5m/15m) trading a demo MT5 account, with a
glassmorphism dashboard, full trade journaling, and a two-tier diagnostic
review (local Ollama daily, Claude API weekly).

## How it fits together

```
Your PC/VPS (Windows, MT5 terminal running)
  connector/mt5_connector.py  ← always-on loop, polls control/status.json,
                                  executes trades, writes data/*.json,
                                  pushes commits back to this repo

GitHub repo (this one)
  strategy/     EMA crossover + SMC (BOS/CHoCH/order blocks/FVGs/sweeps) voting engine
  journal/      daily_ollama.py (free, local) + weekly_claude.py (paid, weekly)
  data/         trades.json, equity.json, journal.json, strategy_state.json — the "database"
  control/      status.json — dashboard writes here, connector reads here
  dashboard/    static site → GitHub Pages, reads data/*.json live
  .github/workflows/  scheduled jobs for journaling + dashboard deploy
```

## Setup

### 1. Create the repo
Push this folder to a new GitHub repo (public, so raw.githubusercontent.com
and GitHub Pages work without extra auth for reads).

```bash
git init
git add .
git commit -m "Initial scaffold"
git branch -M main
git remote add origin https://github.com/<you>/mt5-trading-agent.git
git push -u origin main
```

### 2. Enable GitHub Pages
Repo Settings → Pages → Source: **GitHub Actions**. The `deploy-dashboard.yml`
workflow will publish `dashboard/` automatically on every push.

### 3. Add repo secrets
Repo Settings → Secrets and variables → Actions:
- `ANTHROPIC_API_KEY` — for the weekly Claude review only

### 4. Set up the MT5 machine (your PC or a Windows VPS)
```bash
git clone https://github.com/<you>/mt5-trading-agent.git
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

### 5. Open the dashboard
Once Pages is live (Settings → Pages shows the URL), open it and click
**Set repo / token**:
- Owner + repo name — lets it read `data/*.json` (public, no token needed)
- A GitHub **fine-grained personal access token**, scoped to only this repo,
  with **Contents: read and write** — only needed if you want the power
  on/off toggle to work from the dashboard. Stored in your browser's
  localStorage only, never committed or sent anywhere but GitHub's API.

## Demo → live checklist

Don't flip `mode` to `live` on a whim. Before doing it, you want at minimum:
- A meaningful number of demo trading days behind you (weeks, not days)
- A maximum drawdown you've actually seen and are comfortable repeating with real money
- At least one weekly Claude review that didn't flag a live structural issue
- A kill switch you've actually tested (power off mid-trade, confirm it doesn't touch open positions)

I'd suggest building that as an explicit checklist in the dashboard rather
than a casual toggle — happy to add that next.

## What's next

- Backtest `strategy/signals.py` against historical MT5 data before going live
- Tune `ATR_MIN_MULTIPLIER` and `VOTES_REQUIRED` based on real demo results
- Consider a Telegram alert on every trade (you've already got that stack
  from the job-aggregation bot)
