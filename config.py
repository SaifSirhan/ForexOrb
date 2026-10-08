"""Configuration for the Stage 0 forex alert system."""

PAIRS = ["EURUSD=X", "GBPUSD=X", "GBPJPY=X"]

# Display names used in alerts, CSV rows and console output.
PAIR_LABELS = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "GBPJPY=X": "GBPJPY",
}

TIMEFRAME = "1h"
INTERVAL_MINUTES = 60

# Asian session window in UTC. The window is [ASIAN_START_HOUR, ASIAN_END_HOUR),
# i.e. candles timestamped 00:00 through 06:00 inclusive (07:00 belongs to London).
ASIAN_START_HOUR = 0
ASIAN_END_HOUR = 7

# No alerts are emitted before the Asian range is complete.
LONDON_OPEN_HOUR = 7

# --loop scheduling interval.
LOOP_MINUTES = 15

# Extra history to request so a full Asian session is always covered
# even with weekend gaps or a short previous trading day.
HISTORY_PERIOD = "5d"

LOG_DIR = "logs"
ALERT_CSV = "logs/alerts.csv"

ALERT_HEADER = [
    "timestamp_utc",
    "pair",
    "direction",
    "asian_high",
    "asian_low",
    "range_pips",
    "close_price",
]

# JPY crosses quote to 3 decimals, everything else to 5. Pip = 10th decimal
# digit for non-JPY pairs, 100th for JPY pairs.
PIP_DECIMALS = {"EURUSD=X": 4, "GBPUSD=X": 4, "GBPJPY=X": 2}
PRICE_DECIMALS = {"EURUSD=X": 5, "GBPUSD=X": 5, "GBPJPY=X": 3}

STATE_FILE = "logs/alert_state.json"
