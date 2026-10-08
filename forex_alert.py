"""Stage 0 forex alert system: London Open Range Breakout.

Data + alerts + logging only. No AI, no trading, no prediction.

  python forex_alert.py --once   single check, for testing
  python forex_alert.py --loop   check every LOOP_MINUTES, blocks forever
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

import config

log = logging.getLogger("forex_alert")

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 15


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def load_secrets() -> tuple:
    """Read the bot token and chat id from .env. Returns (token, chat_id)."""
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.error(
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID missing. "
            "Copy .env.example to .env and fill in the real values."
        )
    elif token == "your_bot_token_here":
        log.warning("TELEGRAM_BOT_TOKEN still holds the .env.example placeholder.")

    return token, chat_id


# --------------------------------------------------------------------------
# market data
# --------------------------------------------------------------------------

def fetch_h1(pair: str) -> pd.DataFrame:
    """Return the H1 candles for `pair` on a timezone-aware UTC DatetimeIndex.

    The last row is dropped because yfinance returns the still-forming candle.
    Raises on network failure or on an unusable/empty response.
    """
    df = yf.Ticker(pair).history(
        period=config.HISTORY_PERIOD,
        interval=config.TIMEFRAME,
        auto_adjust=False,
    )

    if df is None or df.empty:
        raise ValueError("yfinance returned no rows")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("response index is not a DatetimeIndex")

    # yfinance hands back a tz-aware index for intraday data, but it is not
    # guaranteed to be UTC. Convert before any timestamp comparison.
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df.sort_index()

    # Drop partial data: the final candle is still forming.
    if len(df) > 1:
        df = df.iloc[:-1]

    if df.empty:
        raise ValueError("no closed candles available")

    return df


def closed_candles(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """Keep only candles that have already closed at `as_of` (UTC)."""
    close_time = df.index + pd.Timedelta(minutes=config.INTERVAL_MINUTES)
    return df[close_time <= as_of]


def asian_range(df: pd.DataFrame, day: datetime):
    """High/low of candles opening in [00:00, 07:00) UTC on `day`'s date.

    Returns (high, low, count) or (None, None, 0) when the window is empty.
    """
    start = datetime(day.year, day.month, day.day, config.ASIAN_START_HOUR,
                     tzinfo=timezone.utc)
    end = datetime(day.year, day.month, day.day, config.ASIAN_END_HOUR,
                   tzinfo=timezone.utc)

    # df.index is tz-aware UTC (guaranteed by fetch_h1), so slicing is safe.
    window = df.loc[(df.index >= start) & (df.index < end)]
    if window.empty:
        return None, None, 0

    return float(window["High"].max()), float(window["Low"].min()), len(window)


def range_in_pips(pair: str, high: float, low: float) -> int:
    decimals = config.PIP_DECIMALS.get(pair, 4)
    return int(round((high - low) * (10 ** decimals)))


# --------------------------------------------------------------------------
# state (one alert per pair / direction / day)
# --------------------------------------------------------------------------

def _state_key(pair: str, day: datetime, direction: str) -> str:
    return f"{pair}|{day.date().isoformat()}|{direction}"


def load_state() -> dict:
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read state file %s: %s", config.STATE_FILE, exc)
        return {}


def save_state(state: dict) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    try:
        with open(config.STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
    except OSError as exc:
        log.error("Could not write state file %s: %s", config.STATE_FILE, exc)


# --------------------------------------------------------------------------
# output: telegram + csv
# --------------------------------------------------------------------------

def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Send one message. Never raises: a failed alert must not kill the loop."""
    if not token or not chat_id:
        log.warning("Telegram not configured, dropping alert: %s", text)
        return False

    try:
        resp = requests.post(
            TELEGRAM_URL.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)
        return False

    if resp.status_code != 200:
        log.error("Telegram rejected the alert (%s): %s",
                  resp.status_code, resp.text[:200])
        return False

    return True


def append_csv(pair: str, direction: str, high: float, low: float,
               pips: int, close: float, when: datetime) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    new_file = not os.path.exists(config.ALERT_CSV)

    try:
        with open(config.ALERT_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(config.ALERT_HEADER)
            writer.writerow([
                when.strftime("%Y-%m-%d %H:%M:%S"),
                pair,
                direction,
                f"{high:.5f}",
                f"{low:.5f}",
                pips,
                f"{close:.5f}",
            ])
    except OSError as exc:
        log.error("Could not append to %s: %s", config.ALERT_CSV, exc)


def format_alert(pair: str, direction: str, high: float, low: float,
                 pips: int, close: float, candle_time: datetime) -> str:
    label = config.PAIR_LABELS.get(pair, pair)
    px = config.PRICE_DECIMALS.get(pair, 5)
    stamp = candle_time.strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{label} {direction} | Asian range {high:.{px}f}\u2013{low:.{px}f} "
        f"({pips} pips) | Close {close:.{px}f} | {stamp}"
    )


# --------------------------------------------------------------------------
# markets
# --------------------------------------------------------------------------

def all_markets_closed(now: datetime) -> bool:
    """True when FX is closed and no new candle exists.

    Market: Sunday 21:00 UTC -> Friday 21:00 UTC. Also treats the daily
    21:00-22:00 UTC rollover as closed.
    """
    weekday = now.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:
        return True
    if weekday == 6 and now.hour < 21:
        return True
    if weekday == 4 and now.hour >= 21:
        return True
    if now.hour == 21:
        return True
    return False


# --------------------------------------------------------------------------
# main check
# --------------------------------------------------------------------------

def check_pair(pair: str, token: str, chat_id: str, state: dict,
               now: datetime) -> None:
    """Evaluate one pair. Any failure is logged and swallowed."""
    label = config.PAIR_LABELS.get(pair, pair)

    try:
        df = fetch_h1(pair)
        df = closed_candles(df, now)
        if df.empty:
            log.info("%-7s skip - no closed candles yet", label)
            return

        last = df.iloc[-1]
        candle_time = df.index[-1]

        # The candle must have opened at or after the London open on its own day.
        if candle_time.hour < config.LONDON_OPEN_HOUR:
            log.info("%-7s skip - last close %s is pre-London",
                     label, candle_time.strftime("%H:%M"))
            return

        high, low, count = asian_range(df, candle_time)
        if count == 0:
            log.info("%-7s skip - no Asian session data for %s",
                     label, candle_time.date())
            return

        close = float(last["Close"])
        pips = range_in_pips(pair, high, low)
        px = config.PRICE_DECIMALS.get(pair, 5)

        if close > high:
            direction = "BREAK_UP"
        elif close < low:
            direction = "BREAK_DOWN"
        else:
            log.info("%-7s no break - close %.{0}f inside range %.{0}f-%.{0}f".format(px)
                     % (close, low, high) + f" ({pips} pips)")
            return

        key = _state_key(pair, candle_time, direction)
        if key in state:
            log.info("%-7s no break - %s already alerted at %s",
                     label, direction, state[key])
            return

        message = format_alert(pair, direction, high, low, pips, close, candle_time)
        if send_telegram(token, chat_id, message):
            append_csv(pair, direction, high, low, pips, close, candle_time)
            state[key] = now.strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
            log.info("%-7s ALERT %s (%d pips) - %s", label, direction, pips, message)
        else:
            # Not recorded, so the next run retries the same break.
            log.info("%-7s ALERT %s NOT delivered - will retry next run",
                     label, direction)

    except Exception as exc:  # noqa: BLE001 - one bad pair must not stop the rest
        log.error("%-7s error: %s: %s", label, type(exc).__name__, exc)


def run_check() -> None:
    now = datetime.now(timezone.utc)
    if all_markets_closed(now):
        log.info("market closed at %s UTC - nothing to check",
                 now.strftime("%Y-%m-%d %H:%M:%S"))
        return

    token, chat_id = load_secrets()
    state = load_state()

    log.info("check start %s UTC (token %s, chat %s)",
             now.strftime("%Y-%m-%d %H:%M:%S"),
             "set" if token else "MISSING",
             "set" if chat_id else "MISSING")
    if not token or not chat_id:
        log.warning("Running without Telegram: breaks will be detected and "
                    "printed but not sent, and not written to the CSV.")

    for pair in config.PAIRS:
        check_pair(pair, token, chat_id, state, now)

    log.info("check done")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage 0 forex London-open range breakout alerts.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="run a single check")
    group.add_argument("--loop", action="store_true",
                       help=f"check every {config.LOOP_MINUTES} min forever")
    args = parser.parse_args()

    setup_logging()

    if args.once:
        run_check()
        return 0

    log.info("loop mode: checking now, then every %d min (Ctrl+C to stop)",
             config.LOOP_MINUTES)
    try:
        run_check()
    except Exception as exc:  # noqa: BLE001
        log.error("initial check failed: %s: %s", type(exc).__name__, exc)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_check,
        "interval",
        minutes=config.LOOP_MINUTES,
        next_run_time=datetime.now(timezone.utc) + timedelta(
            minutes=config.LOOP_MINUTES),
        id="forex_check",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
