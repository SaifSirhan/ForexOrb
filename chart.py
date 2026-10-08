"""Candlestick chart for the ForexOrb GUI.

Downloads GC=F candles for the selected timeframe and renders them as a
lightweight-charts candlestick series (QtWebEngine backend), with the daily
Asian session drawn as a shaded price box per day and alert markers overlaid.

  TIMEFRAMES       -> ordered timeframe keys shown in the dropdown
  fetch_candles(tf) -> DataFrame ready for Chart.set(), on a UTC index
  build_chart(parent, chart, tf) -> draws onto an existing QtChart
"""

import csv
import os
from datetime import datetime, time, timezone

import pandas as pd
import yfinance as yf
from lightweight_charts.widgets import QtChart

import config

PAIR = config.PAIRS[0] if config.PAIRS else "GC=F"

# Timeframe key -> (interval, period). The 4h key is fetched as 1h and
# resampled locally; yfinance has no native 4h interval for futures.
TIMEFRAMES = {
    "5m": ("5m", "5d"),
    "15m": ("15m", "5d"),
    "1h": ("1h", "1mo"),
    "4h": ("1h", "1mo"),
    "1D": ("1d", "6mo"),
}
DEFAULT_TIMEFRAME = "1h"

UP_COLOR = "#2fbf71"
DOWN_COLOR = "#e5484d"
BAND_LINE = "#6a6a6a"
BAND_FILL = "rgba(120, 120, 120, 0.18)"


def _to_utc(df):
    """Return df with a tz-aware UTC index.

    yfinance returns intraday data in the exchange timezone (America/
    New_York for GC=F). tz_convert raises on a naive index, so localise
    first, mirroring the guard in forex_alert.fetch_h1.
    """
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def _resample_4h(df):
    """Aggregate 1h candles into 4h bars, labelled at the window open.

    label/closed='left' puts the bar at the start of the 4h window, which
    is how the Asian 00:00 bucket lines up with the 00:00-05:00 range.
    """
    out = df.resample("4h", label="left", closed="left").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
    })
    return out.dropna(subset=["Close"])


def fetch_candles(timeframe=DEFAULT_TIMEFRAME):
    """Download candles for `timeframe` on a UTC index, NaN closes dropped.

    Returns a DataFrame indexed by tz-aware UTC timestamps with Open/High/
    Low/Close columns. Blocking network call: run in a worker thread.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe!r}")

    interval, period = TIMEFRAMES[timeframe]
    df = yf.Ticker(PAIR).history(period=period, interval=interval,
                                 auto_adjust=False)
    if df is None or df.empty:
        raise ValueError("yfinance returned no rows")

    df = _to_utc(df)
    df = df[["Open", "High", "Low", "Close"]]
    df = df[df["Close"].notna()]

    if timeframe == "4h":
        df = _resample_4h(df)

    df = df.sort_index()
    if df.empty:
        raise ValueError("no candles left after cleaning")
    return df


def to_chart_frame(df):
    """Convert an OHLC frame into the lowercase frame Chart.set() expects.

    lightweight_charts lowercases columns and reads the datetime from a
    `time` column or from a `date`/`time` index, then converts to epoch
    seconds and quantises to the inferred bar interval.

    The time column is forced to nanosecond resolution: the library divides
    the int64 epoch by 10**9, which only yields seconds for datetime64[ns].
    A datetime64[s] index (as yfinance gives here) would collapse every bar
    to epoch second 1 and the chart would render a single stacked bar.
    """
    out = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
    }).copy()
    out.index.name = "time"
    out = out.reset_index()
    out["time"] = pd.to_datetime(out["time"]).astype("datetime64[ns, UTC]")
    return out


def read_alerts(path):
    """Return alert rows as dicts. Missing file yields an empty list.

    A malformed timestamp or row is skipped; the chart must still render.
    """
    if not os.path.exists(path):
        return []

    alerts = []
    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                raw = (row.get("timestamp_utc") or "").strip()
                direction = (row.get("direction") or "").strip()
                if not raw or direction not in ("BREAK_UP", "BREAK_DOWN"):
                    continue
                try:
                    stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                alerts.append({
                    "when": stamp.replace(tzinfo=timezone.utc),
                    "direction": direction,
                })
    except (OSError, csv.Error):
        return alerts

    return alerts


def _asian_boxes(chart, df):
    """Shade each day's 00:00-05:00 UTC range as a Box primitive.

    The high/low of the candles inside the window bound the box vertically;
    days with no candles in the window are skipped.
    """
    start_h = config.ASIAN_START_HOUR
    end_h = config.ASIAN_END_HOUR

    for day in sorted({d.date() for d in df.index}):
        win_start = datetime.combine(day, time(start_h), tzinfo=timezone.utc)
        win_end = datetime.combine(day, time(end_h), tzinfo=timezone.utc)
        window = df[(df.index >= win_start) & (df.index < win_end)]
        if window.empty:
            continue
        high = float(window["High"].max())
        low = float(window["Low"].min())
        # The box spans [win_start, win_end]; the last candle in the window
        # is the right edge so the primitive lands on real bar times.
        right = window.index[-1].to_pydatetime()
        chart.box(win_start, high, right, low,
                  color=BAND_LINE, fill_color=BAND_FILL,
                  width=1, style="solid", round=False)


def _mark_alerts(chart, df, alerts):
    """Arrow marker at each alert whose timestamp matches a candle."""
    if not alerts:
        return

    known = set(df.index)
    label = config.PAIR_LABELS.get(PAIR, PAIR)
    last = df.index[-1]

    for alert in alerts:
        when = alert["when"]
        if when not in known:
            continue
        # A marker cannot be placed beyond the last bar.
        if when > last:
            continue

        if alert["direction"] == "BREAK_UP":
            chart.marker(time=when, position="below", shape="arrow_up",
                         color=UP_COLOR, text=label)
        else:
            chart.marker(time=when, position="above", shape="arrow_down",
                         color=DOWN_COLOR, text=label)


def draw_chart(chart, df=None, alerts_path=None):
    """(Re)draw onto an existing QtChart from already-downloaded `df`.

    Qt/WebEngine is not thread-safe, so this must run on the main thread.
    `df` is produced by fetch_candles() in a worker thread.
    """
    path = alerts_path or config.ALERT_CSV
    if df is None:
        df = fetch_candles()
    alerts = read_alerts(path)

    chart.set(to_chart_frame(df))
    _asian_boxes(chart, df)
    _mark_alerts(chart, df, alerts)
    chart.fit()
    return len(alerts)


def build_chart(parent, timeframe=DEFAULT_TIMEFRAME):
    """Create a QtChart embedded in `parent` (a Qt widget).

    Returns the QtChart. Data is supplied later via draw_chart() on the
    main thread once a worker has downloaded it.
    """
    return QtChart(parent, inner_width=1, inner_height=1)
