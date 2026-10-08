"""Trading-only AI chat panel for the ForexOrb GUI.

A PySide6 widget docked on the right of the main window. Sends the user's
question plus a small slice of context (the last rows of the alert log, the
current chart timeframe and last close price) to DeepSeek and shows the
reply. Streaming is off: one request, one response.

Also supports chart vision: capture_and_analyze() grabs the live chart canvas
as a PNG and asks the vision model to describe it.

  ChatPanel(parent, context_provider) -> widget; call set_chart_view(view)
  to enable capture_and_analyze().
"""

import base64
import csv
import os

import requests
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QThread, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QVBoxLayout,
    QWidget,
)

import config

API_URL = "https://api.deepseek.com/v1/chat/completions"
# deepseek-flash is the vision-capable model and also serves text.
MODEL = "deepseek-flash"
REQUEST_TIMEOUT = 60

MAX_HISTORY_TURNS = 20
CONTEXT_ROWS = 20

SYSTEM_PROMPT = (
    "You are a trading assistant embedded in a personal gold (GC=F) alert "
    "system. You only discuss: forex and gold price action, technical "
    "analysis, risk management, position sizing, market sessions, and "
    "interpretation of the alert log the user shares with you. If asked "
    "anything outside those topics, reply: 'I only handle trading "
    "questions.' Do not give financial advice. Do not predict prices. Do "
    "not claim certainty. Be concise. When analyzing charts, describe what "
    "you see factually. Do not recommend buy/sell actions."
)

VISION_PROMPT = (
    "Analyze this chart. Identify patterns, trends, and key levels. "
    "Be specific."
)

NO_KEY_MESSAGE = (
    "DEEPSEEK_API_KEY is not set. Add it to your .env file and restart the "
    "app, then try again."
)
NOT_READY_MESSAGE = "Chart not ready \u2014 wait a moment and try again."

# JavaScript run inside the chart page to serialise the main canvas to PNG.
# QWidget.grab() returns a blank image for QtWebEngine content, so the image
# must be taken from the page's own canvas instead.
_CANVAS_JS = """
(function () {
  var canvases = document.querySelectorAll('canvas');
  if (!canvases.length) { return 'NOCANVAS'; }
  var target = null, best = 0;
  for (var i = 0; i < canvases.length; i++) {
    var c = canvases[i];
    if (c.width * c.height > best) { best = c.width * c.height; target = c; }
  }
  if (!target) { return 'NOCANVAS'; }
  try { return target.toDataURL('image/png'); }
  catch (e) { return 'ERR:' + e.message; }
})();
"""


def read_recent_rows(path, limit=CONTEXT_ROWS):
    """Return up to `limit` most recent alert rows as a compact text table."""
    if not os.path.exists(path):
        return "(no alerts logged yet)"

    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return "(no alerts logged yet)"
            rows = [row for row in reader if len(row) == len(header)]
    except (OSError, csv.Error) as exc:
        return f"(alert log unreadable: {exc})"

    if not rows:
        return "(no alerts logged yet)"

    recent = rows[-limit:]
    lines = [" | ".join(header)]
    lines.extend(" | ".join(row) for row in recent)
    return "\n".join(lines)


def build_context(context_provider):
    """Assemble the per-request context block."""
    try:
        timeframe, last_close = context_provider()
    except Exception:  # noqa: BLE001 - context must never break a send
        timeframe, last_close = "unknown", "unknown"

    table = read_recent_rows(config.ALERT_CSV)
    close = last_close if last_close is not None else "unknown"
    return (
        f"Current chart timeframe: {timeframe}\n"
        f"Last close price: {close}\n"
        f"Recent alert log (most recent last):\n{table}"
    )


def _png_from_data_url(data_url):
    """Decode a data:image/png;base64,... URL into raw PNG bytes.

    Uses QByteArray/QBuffer for the base64 decode step so the image path
    stays inside Qt, matching the rest of the capture pipeline.
    """
    if not data_url or "," not in data_url:
        return None
    b64 = data_url.split(",", 1)[1]
    buf = QBuffer(QByteArray(base64.b64decode(b64)))
    buf.open(QIODevice.ReadOnly)
    data = bytes(buf.readAll())
    buf.close()
    return data


def _is_blank_png(png_bytes):
    """True if the image is missing, trivial, or a single flat colour.

    A chart that has not painted yet serialises to a uniformly white canvas,
    so size alone is not enough: decode and sample actual pixels.
    """
    if not png_bytes or len(png_bytes) < 1000:
        return True

    image = QImage()
    if not image.loadFromData(QByteArray(png_bytes), "PNG"):
        return True

    width, height = image.width(), image.height()
    if width < 2 or height < 2:
        return True

    seen = set()
    step_x = max(1, width // 40)
    step_y = max(1, height // 20)
    for x in range(0, width, step_x):
        for y in range(0, height, step_y):
            seen.add(image.pixel(x, y))
            if len(seen) > 2:
                return False
    # Only a flat fill (or a single edge) was found.
    return True


class _ChatWorker(QObject):
    """Runs one blocking DeepSeek request off the GUI thread.

    Emits results as signals only. It must never touch a widget, the chat
    document, or any Qt object owned by the GUI thread; the panel's slots
    run back on the main thread via queued connections.
    """

    finished = Signal(str)
    failed = Signal(str)
    token_used = Signal(int, int, int)

    def __init__(self, messages):
        super().__init__()
        self._messages = messages

    def run(self):
        key = os.getenv("DEEPSEEK_API_KEY")
        if not key:
            self.failed.emit(NO_KEY_MESSAGE)
            return

        payload = {"model": MODEL, "messages": self._messages,
                   "stream": False}
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(API_URL, json=payload, headers=headers,
                                 timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            self.failed.emit(f"Request failed: {exc}")
            return

        if resp.status_code != 200:
            self.failed.emit(
                f"DeepSeek returned HTTP {resp.status_code}. "
                f"{resp.text[:300]}"
            )
            return

        try:
            data = resp.json()
            reply = data["choices"][0]["message"].get("content") or ""
            if not reply.strip():
                reply = "(no text returned)"
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self.failed.emit(f"Unexpected API response: {exc}")
            return

        if usage:
            self.token_used.emit(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
            )
        self.finished.emit(reply)


class ChatPanel(QWidget):
    """Scrollable history + input + Send, with DeepSeek text and vision."""

    def __init__(self, parent=None, context_provider=None):
        super().__init__(parent)
        self._context_provider = context_provider or (lambda: ("unknown", None))
        self._history = []
        self._busy = False
        self._thread = None
        self._worker = None
        self._chart_view = None
        self._pending_capture = None
        self._pending_kind = None
        self._pending_user = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("TRADING DESK")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self.history_view = QTextEdit()
        self.history_view.setObjectName("historyView")
        self.history_view.setReadOnly(True)
        layout.addWidget(self.history_view, stretch=1)

        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Ask about gold, risk, sessions\u2026")
        self.entry.returnPressed.connect(self.send_message)
        row.addWidget(self.entry, stretch=1)

        self.send_btn = QPushButton("SEND")
        self.send_btn.setObjectName("sendButton")
        self.send_btn.clicked.connect(self.send_message)
        row.addWidget(self.send_btn)
        layout.addLayout(row)

        self.analyze_btn = QPushButton("ANALYZE CHART")
        self.analyze_btn.setObjectName("analyzeButton")
        self.analyze_btn.clicked.connect(self.capture_and_analyze)
        layout.addWidget(self.analyze_btn)

    def set_context_provider(self, provider):
        self._context_provider = provider

    def set_chart_view(self, view):
        """Give the panel the chart's QWebEngineView so it can be captured."""
        self._chart_view = view

    # -- rendering ---------------------------------------------------------

    def _append(self, who, text):
        self.history_view.append(f"{who}: {text}")
        self._scroll_bottom()

    def _set_busy(self, busy):
        self._busy = busy
        self.send_btn.setEnabled(not busy)
        self.analyze_btn.setEnabled(not busy)
        self.entry.setEnabled(not busy)

    # -- text chat ---------------------------------------------------------

    def send_message(self):
        if self._busy:
            return
        text = self.entry.text().strip()
        if not text:
            return

        self.entry.clear()
        self._append("You", text)
        self._set_busy(True)
        self._append("Assistant", "...")
        self._pending_kind = "chat"
        self._pending_user = text

        context = build_context(self._context_provider)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": context},
        ]
        messages.extend(self._trimmed_history())
        messages.append({"role": "user", "content": text})

        self._dispatch(messages)

    # -- vision ------------------------------------------------------------

    def capture_and_analyze(self):
        """Grab the chart canvas and ask the vision model about it."""
        if self._busy:
            return
        if self._chart_view is None:
            self._append("Assistant", NOT_READY_MESSAGE)
            return

        self._set_busy(True)
        self._append("Assistant", "Capturing chart\u2026")
        self._pending_capture = True
        self._chart_view.page().runJavaScript(_CANVAS_JS, self._on_canvas)

    def _on_canvas(self, data_url):
        """Main-thread callback with the canvas data URL (or an error code)."""
        if not self._pending_capture:
            return
        self._pending_capture = False

        if not isinstance(data_url, str) or not data_url.startswith(
                "data:image/png;base64,"):
            self._replace_placeholder(NOT_READY_MESSAGE)
            self._set_busy(False)
            return

        png_bytes = _png_from_data_url(data_url)
        if _is_blank_png(png_bytes):
            self._replace_placeholder(NOT_READY_MESSAGE)
            self._set_busy(False)
            return

        b64 = base64.b64encode(png_bytes).decode("ascii")
        context = build_context(self._context_provider)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": context},
            {"role": "user", "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
            ]},
        ]

        self._append("You", "[chart screenshot]")
        self._pending_kind = "vision"
        self._pending_user = "[chart screenshot]"
        self._dispatch(messages)

    # -- request plumbing --------------------------------------------------

    def _dispatch(self, messages):
        """Start one worker thread. Results arrive only via signals."""
        self._thread = QThread(self)
        self._worker = _ChatWorker(messages)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_reply)
        self._worker.failed.connect(self._on_error)
        self._worker.token_used.connect(self._on_tokens)
        # Standard teardown: let the worker die with its thread, and let the
        # thread delete itself once its event loop has stopped.
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_reply(self, reply):
        """Main-thread slot: show the assistant reply and record history."""
        self._replace_placeholder(reply)
        if self._pending_kind == "chat":
            self._history.append({"role": "user",
                                  "content": self._pending_user})
        else:
            self._history.append({"role": "user",
                                  "content": "[chart screenshot]"})
        self._history.append({"role": "assistant", "content": reply})
        self._pending_kind = None
        self._pending_user = None
        self._finish()

    def _on_error(self, message):
        """Main-thread slot: show the error and release the panel."""
        self._replace_placeholder(message)
        self._pending_kind = None
        self._pending_user = None
        self._finish()

    def _on_tokens(self, prompt, completion, total):
        """Main-thread slot: cost visibility in the console."""
        print(f"[tokens] prompt={prompt} completion={completion} "
              f"total={total}", flush=True)

    def _trimmed_history(self):
        """Last MAX_HISTORY_TURNS exchanges, oldest dropped first."""
        limit = MAX_HISTORY_TURNS * 2
        if len(self._history) <= limit:
            return list(self._history)
        return list(self._history[-limit:])

    def _finish(self):
        """Stop the worker thread without terminate().

        Deletion is handled by the thread.finished -> deleteLater()
        connections made in _dispatch; here we only stop and drop refs.
        """
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._worker = None
        self._set_busy(False)

    def _replace_placeholder(self, text):
        """Replace the trailing placeholder line with the real text."""
        doc = self.history_view.toPlainText().rstrip()
        for marker in ("Assistant: ...", "Assistant: Capturing chart\u2026"):
            if doc.endswith(marker):
                trimmed = doc[: -len(marker)].rstrip("\n")
                self.history_view.setPlainText(trimmed)
                self.history_view.append(f"Assistant: {text}")
                self._scroll_bottom()
                return
        self._append("Assistant", text)

    def _scroll_bottom(self):
        bar = self.history_view.verticalScrollBar()
        bar.setValue(bar.maximum())
