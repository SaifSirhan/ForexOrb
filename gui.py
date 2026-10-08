"""Read-only log + chart viewer with a trading AI panel for ForexOrb.

Left column: alert log table on top, candlestick chart below.
Right column: trading-only DeepSeek chat panel.

Runs on PySide6; the chart is a lightweight-charts QtChart embedded via
QtWebEngine. yfinance downloads and DeepSeek calls both run off the GUI
thread, so the window never freezes.

  python gui.py
"""

import csv
import os
import sys

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import chart
import config
from chat_panel import ChatPanel

REFRESH_MS = 60_000

COLUMNS = config.ALERT_HEADER

# Trader palette: green = up/buy, red = down/sell.
BUY_GREEN = "#0a8f4d"
SELL_RED = "#c0223b"

# Marble palette: NYSE-style cream stone with grey veining and brass trim.
MARBLE_BASE = "#efe9df"
MARBLE_PANEL = "#f6f2ea"
MARBLE_VEIN = "#cfc6b8"
MARBLE_DARK = "#b9ae9c"
BRASS = "#a8862c"
BRASS_LIGHT = "#d4af37"

DIRECTION_COLORS = {
    "BREAK_UP": BUY_GREEN,
    "BREAK_DOWN": SELL_RED,
}

# A marble veining texture: soft diagonal streaks over cream stone, drawn as
# a border-image free gradient set so it scales with any widget size.
MARBLE_TEXTURE = (
    "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
    f"stop:0 {MARBLE_BASE}, stop:0.18 {MARBLE_PANEL}, stop:0.34 {MARBLE_VEIN}, "
    f"stop:0.44 {MARBLE_BASE}, stop:0.62 {MARBLE_PANEL}, stop:0.78 {MARBLE_VEIN}, "
    f"stop:0.9 {MARBLE_BASE}, stop:1 {MARBLE_PANEL})"
)

STYLESHEET = f"""
QMainWindow, QWidget {{
    background: {MARBLE_BASE};
    color: #2a2620;
    font-family: 'Georgia', 'Segoe UI', serif;
    font-size: 12px;
}}
#tickerBar {{
    background: {MARBLE_TEXTURE};
    border-bottom: 3px solid {BRASS};
}}
#tickerSymbol {{
    color: #1d3a5f;
    font-size: 21px;
    font-weight: bold;
    letter-spacing: 3px;
}}
#tickerSub {{
    color: #6b6353;
    font-size: 11px;
    letter-spacing: 2px;
}}
#statusLabel {{ color: #6b6353; }}
QPushButton {{
    background: {MARBLE_PANEL};
    color: #2a2620;
    border: 1px solid {MARBLE_DARK};
    padding: 6px 14px;
    letter-spacing: 1px;
}}
QPushButton:hover {{ border-color: {BRASS}; color: {BRASS}; }}
QPushButton:disabled {{ color: #a89f8d; border-color: {MARBLE_VEIN}; }}
#sendButton {{ border-color: {BUY_GREEN}; color: {BUY_GREEN}; }}
#analyzeButton {{ border-color: {BRASS}; color: {BRASS}; }}
#analyzeButton:hover {{ border-color: {BRASS_LIGHT}; color: {BRASS_LIGHT}; }}
QComboBox {{
    background: {MARBLE_PANEL};
    border: 1px solid {MARBLE_DARK};
    padding: 4px 10px;
}}
QComboBox QAbstractItemView {{
    background: {MARBLE_PANEL};
    selection-background-color: {BRASS_LIGHT};
}}
QTableWidget {{
    background: #fbf8f2;
    gridline-color: {MARBLE_VEIN};
    border: 1px solid {MARBLE_DARK};
}}
QHeaderView::section {{
    background: {MARBLE_TEXTURE};
    color: #4a4335;
    border: 0;
    border-right: 1px solid {MARBLE_VEIN};
    padding: 6px;
    letter-spacing: 1px;
}}
QTextEdit#historyView {{
    background: #fbf8f2;
    border: 1px solid {MARBLE_DARK};
    color: #2a2620;
}}
QLineEdit {{
    background: #fbf8f2;
    border: 1px solid {MARBLE_DARK};
    padding: 6px;
    color: #2a2620;
}}
QLineEdit:focus {{ border-color: {BRASS}; }}
#panelTitle {{
    color: {BRASS};
    font-weight: bold;
    letter-spacing: 3px;
}}
QSplitter::handle {{ background: {MARBLE_VEIN}; }}
"""


def read_alerts(path):
    """Return (rows, error). rows are lists of strings in header order.

    Malformed rows (wrong field count) are skipped. A missing file yields
    an empty list, not an error.
    """
    if not os.path.exists(path):
        return [], None

    rows = []
    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # discard header
            for raw in reader:
                if len(raw) != len(COLUMNS):
                    continue
                rows.append(raw)
    except (OSError, csv.Error) as exc:
        return rows, str(exc)

    return rows, None


class _DownloadWorker(QObject):
    """Fetches candles for `timeframe` off the GUI thread."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, timeframe):
        super().__init__()
        self._timeframe = timeframe

    def run(self):
        try:
            df = chart.fetch_candles(self._timeframe)
        except Exception as exc:  # noqa: BLE001 - report, never crash the GUI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(df)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ForexOrb \u2014 Gold Trading Desk")
        self.resize(1400, 900)

        self._chart_thread = None
        self._chart_worker = None
        self._chart_busy = False
        self._timeframe = chart.DEFAULT_TIMEFRAME
        self._last_close = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_ticker())

        body = QWidget()
        body_layout = QHBoxLayout(body)
        outer.addWidget(body, stretch=1)

        splitter = QSplitter(Qt.Horizontal)
        body_layout.addWidget(splitter)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.refresh()
        self.refresh_chart()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)

    # -- construction ------------------------------------------------------

    def _build_ticker(self):
        bar = QFrame()
        bar.setObjectName("tickerBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 8, 14, 8)

        symbol = QLabel(chart.PAIR)
        symbol.setObjectName("tickerSymbol")
        lay.addWidget(symbol)

        sub = QLabel("GOLD FUTURES \u00b7 COMEX \u00b7 UTC")
        sub.setObjectName("tickerSub")
        lay.addWidget(sub)
        lay.addStretch(1)
        return bar

    def _build_left(self):
        left = QWidget()
        layout = QVBoxLayout(left)

        controls = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("statusLabel")
        controls.addWidget(self.status, stretch=1)

        self.tf_box = QComboBox()
        self.tf_box.addItems(list(chart.TIMEFRAMES.keys()))
        self.tf_box.setCurrentText(self._timeframe)
        self.tf_box.currentTextChanged.connect(self._on_timeframe_changed)
        controls.addWidget(QLabel("TIMEFRAME"))
        controls.addWidget(self.tf_box)

        self.refresh_btn = QPushButton("REFRESH")
        self.refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_btn)

        self.chart_btn = QPushButton("REFRESH CHART")
        self.chart_btn.clicked.connect(self.refresh_chart)
        controls.addWidget(self.chart_btn)

        layout.addLayout(controls)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, stretch=1)

        self.chart_label = QLabel("Loading chart\u2026")
        layout.addWidget(self.chart_label)

        self.chart_holder = QWidget()
        self.chart_layout = QVBoxLayout(self.chart_holder)
        self.chart_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.chart_holder, stretch=2)

        self.qt_chart = chart.build_chart(self.chart_holder, self._timeframe)
        self.chart_view = self.qt_chart.get_webview()
        self.chart_view.setMinimumHeight(320)
        self.chart_layout.addWidget(self.chart_view)
        return left

    def _build_right(self):
        self.chat = ChatPanel(self, context_provider=self._chat_context)
        self.chat.set_chart_view(self.chart_view)
        return self.chat

    def _chat_context(self):
        return self._timeframe, self._last_close

    # -- alerts table ------------------------------------------------------

    def refresh(self):
        index_col = (COLUMNS.index("direction")
                     if "direction" in COLUMNS else -1)
        rows, error = read_alerts(config.ALERT_CSV)

        self.table.setRowCount(0)
        for row in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                if c == index_col and value in DIRECTION_COLORS:
                    item.setForeground(QColor(DIRECTION_COLORS[value]))
                self.table.setItem(r, c, item)

        if rows:
            self.status.setText(f"{len(rows)} alert(s) \u00b7 {config.ALERT_CSV}")
        elif error:
            self.status.setText(f"Error reading {config.ALERT_CSV}: {error}")
        else:
            self.status.setText("No alerts yet")

    # -- chart -------------------------------------------------------------

    def _on_timeframe_changed(self, value):
        self._timeframe = value
        self.refresh_chart()

    def refresh_chart(self):
        if self._chart_busy:
            return
        self._chart_busy = True
        self.chart_btn.setEnabled(False)
        self.tf_box.setEnabled(False)
        self.chart_label.setText(
            f"Loading {self._timeframe} chart\u2026")

        self._chart_thread = QThread(self)
        self._chart_worker = _DownloadWorker(self._timeframe)
        self._chart_worker.moveToThread(self._chart_thread)
        self._chart_thread.started.connect(self._chart_worker.run)
        self._chart_worker.finished.connect(self._on_chart_data)
        self._chart_worker.failed.connect(self._on_chart_error)
        self._chart_thread.start()

    def _stop_chart_thread(self):
        if self._chart_thread is not None:
            self._chart_thread.quit()
            self._chart_thread.wait(2000)
        self._chart_thread = None
        self._chart_worker = None
        self._chart_busy = False
        self.chart_btn.setEnabled(True)
        self.tf_box.setEnabled(True)

    def _on_chart_data(self, df):
        try:
            self._last_close = float(df["Close"].iloc[-1])
        except (KeyError, IndexError, ValueError):
            self._last_close = None
        chart.draw_chart(self.qt_chart, df=df)
        self.chart_label.setText(f"{chart.PAIR} \u2014 {self._timeframe} (UTC)")
        self._stop_chart_thread()

    def _on_chart_error(self, message):
        self.chart_label.setText(f"Chart unavailable \u2014 {message}")
        self._stop_chart_thread()

    # -- lifecycle ---------------------------------------------------------

    def closeEvent(self, event):
        if self._chart_thread is not None:
            self._chart_thread.quit()
            self._chart_thread.wait(2000)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
