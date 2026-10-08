# Forex Alerts - Stage 0

Asian range breakout alerts (London open) for EURUSD, GBPUSD, GBPJPY. Data + alerts + logging only.

## Setup

1. Install deps (no .venv, system interpreter):
   `C:\Users\USER\.pyenv\pyenv-win\versions\3.11.9\python.exe -m pip install yfinance pandas python-dotenv apscheduler requests`
2. Create `.env` from the template and fill in real values:
   `Copy-Item .env.example .env` then edit it.

## Run

- Single check (testing): `C:\Users\USER\.pyenv\pyenv-win\versions\3.11.9\python.exe forex_alert.py --once`
- Loop mode (every 15 min, blocks): `C:\Users\USER\.pyenv\pyenv-win\versions\3.11.9\python.exe forex_alert.py --loop`

## How it works

Asian range = high/low of candles opening 00:00-06:59 UTC. From 07:00 UTC on, each closed H1 candle is compared against that range: close above the high alerts BREAK_UP, close below the low alerts BREAK_DOWN, otherwise nothing. At most one alert per pair, per direction, per day.

## Output

- Alerts go to Telegram, and are appended to `logs/alerts.csv`.
- `logs/alert_state.json` tracks which alerts were already sent.
- A pair that fails to download is logged and skipped; the rest still run.
