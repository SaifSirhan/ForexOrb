"""Configuration for the Stage 0 gold alert system."""

# Gold futures only. GC=F tracks spot closely and updates in real time;
# XAUUSD=X is unreliable and returns stale data on yfinance.
PAIRS = ["GC=F"]

# Display names used in alerts, CSV rows and console output.
PAIR_LABELS = {
    "GC=F": "GC=F",
}

TIMEFRAME = "1h"
INTERVAL_MINUTES = 60

# Asian session window in UTC. The window is [ASIAN_START_HOUR, ASIAN_END_HOUR),
# i.e. candles timestamped 00:00 through 04:00 inclusive (05:00 belongs to
# the pre-London gold session and is where breakout checks begin).
ASIAN_START_HOUR = 0
ASIAN_END_HOUR = 5

# No alerts are emitted before the Asian range is complete. The breakout
# window is [LONDON_OPEN_HOUR, SESSION_END_HOUR) in UTC: candles opening at
# or after 05:00, and strictly before the 21:00 daily rollover. Candles at
# 21:00+ belong to the next trading day's Asian range, not this one.
LONDON_OPEN_HOUR = 5
SESSION_END_HOUR = 21

# --loop scheduling interval.
LOOP_MINUTES = 15

# Extra history to request so a full Asian session is always covered
# even with weekend gaps or a short previous trading day.
HISTORY_PERIOD = "5d"

LOG_DIR = "logs"
ALERT_CSV = "logs/alerts.csv"

# Scheduled runs are headless, so every run also appends to this file.
# Rotated so a long-running loop cannot fill the disk.
LOG_FILE = "logs/forex_alert.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3

# range_pips now holds points (1 point = 0.10 USD) for gold.
ALERT_HEADER = [
    "timestamp_utc",
    "pair",
    "direction",
    "asian_high",
    "asian_low",
    "range_pips",
    "close_price",
]

# Gold is measured in points, not forex pips: 1 point = 0.10 USD, so a
# whole-dollar move is 10 points. Retained as a mapping so the pair loop
# stays reusable if other instruments are ever added back.
PIP_SIZE_BY_PAIR = {"GC=F": 0.10}
DEFAULT_PIP_SIZE = 0.0001

# Gold quotes to 2 decimals for display.
PRICE_DECIMALS = {"GC=F": 2}

STATE_FILE = "logs/alert_state.json"
