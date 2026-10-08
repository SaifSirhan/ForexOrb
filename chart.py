"""Price chart for the ForexOrb GUI.

Downloads the last 5 days of GC=F H1 candles and renders them as a close
line with the daily Asian session shaded and alert markers overlaid.

  build_chart(parent) -> the matplotlib canvas widget to embed
"""

import csv
import os
from datetime import datetime, time, timezone

import matplotlib

matplotlib.use("TkAgg")

import pandas as pd
import yfinance as yf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import config

PAIR = config.PAIRS[0] if config.PAIRS else "GC=F"
PERIOD = "5d"
INTERVAL = "1h"

BAND_COLOR = "#3a3a3a"
LINE_COLOR = "#d8d8d8"
UP_COLOR = "#2fbf71"
DOWN_COLOR = "#e5484d"


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


def fetch_candles():
    """Last 5 days of H1 candles on a UTC index, NaN closes dropped."""
    df = yf.Ticker(PAIR).history(period=PERIOD, interval=INTERVAL,
                                 auto_adjust=False)
    if df is None or df.empty:
        raise ValueError("yfinance returned no rows")

    df = _to_utc(df)
    df = df[df["Close"].notna()]
    df = df.sort_index()
    return df


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
                    "pair": (row.get("pair") or "").strip(),
                    "close": (row.get("close_price") or "").strip(),
                })
    except (OSError, csv.Error):
        return alerts

    return alerts


def _shade_asian_sessions(ax, df):
    """Light grey vertical band over each day's 00:00-05:00 UTC window."""
    for day in sorted({d.date() for d in df.index}):
        start = datetime.combine(day, time(config.ASIAN_START_HOUR),
                                 tzinfo=timezone.utc)
        end = datetime.combine(day, time(config.ASIAN_END_HOUR),
                               tzinfo=timezone.utc)
        ax.axvspan(start, end, color=BAND_COLOR, alpha=0.45, linewidth=0, zorder=0)


def _mark_alerts(ax, df, alerts):
    """Arrow at each alert whose timestamp matches a candle in range."""
    if not alerts:
        return

    known = set(df.index)
    label = config.PAIR_LABELS.get(PAIR, PAIR)

    for alert in alerts:
        when = alert["when"]
        if when not in known:
            continue

        price = float(df.loc[when, "Close"])
        if alert["direction"] == "BREAK_UP":
            colour, marker, offset, va = UP_COLOR, "^", 14, "bottom"
        else:
            colour, marker, offset, va = DOWN_COLOR, "v", -14, "top"

        ax.plot(when, price, marker=marker, color=colour, markersize=9,
                zorder=5, linestyle="none")
        ax.annotate(label, xy=(when, price), xytext=(0, offset),
                    textcoords="offset points", ha="center", va=va,
                    color=colour, fontsize=8, zorder=6)


def draw_chart(fig, df=None, alerts_path=None):
    """(Re)draw the chart onto fig from already-downloaded `df`.

    Tk and matplotlib are not thread-safe, so this must run on the main
    thread. `df` is produced by fetch_candles() in a worker thread.
    """
    path = alerts_path or config.ALERT_CSV
    if df is None:
        df = fetch_candles()
    alerts = read_alerts(path)

    fig.clear()
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1e1e1e")

    _shade_asian_sessions(ax, df)
    ax.plot(df.index, df["Close"], color=LINE_COLOR, linewidth=1.2, zorder=3)
    _mark_alerts(ax, df, alerts)

    ax.set_title(f"{PAIR} last 5 days (UTC)", color="#e6e6e6", fontsize=11)
    ax.grid(True, color="#4a4a4a", linewidth=0.5, alpha=0.6)
    ax.tick_params(colors="#b0b0b0", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#4a4a4a")
    ax.margins(x=0.01)
    fig.autofmt_xdate(rotation=20, ha="right")
    fig.tight_layout()
    return len(alerts)


def draw_error(fig, exc):
    """Render a short failure note in place of the chart. Main thread only."""
    fig.clear()
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1e1e1e")
    ax.text(0.5, 0.5, f"Chart unavailable\n{type(exc).__name__}: {exc}",
            ha="center", va="center", color="#e5484d",
            fontsize=10, transform=ax.transAxes, wrap=True)
    ax.set_axis_off()


def build_chart(parent):
    """Build a FigureCanvasTkAgg embedded in `parent`, ready to draw.

    Returns the Tk canvas widget with a `render(df)` attribute attached.
    Call render() from the main thread after downloading `df` in a worker.
    """
    fig = Figure(figsize=(8, 3.2), dpi=100, facecolor="#1e1e1e")
    canvas = FigureCanvasTkAgg(fig, master=parent)
    widget = canvas.get_tk_widget()

    def render(df):
        draw_chart(fig, df=df)
        canvas.draw()

    widget.render = render
    widget.figure = fig
    widget.canvas = canvas
    widget.show_error = lambda exc: (draw_error(fig, exc), canvas.draw())
    return widget
