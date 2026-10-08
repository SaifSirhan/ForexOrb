"""Read-only log viewer for the ForexOrb alert system.

Reads logs/alerts.csv and displays the rows in a table. No editing, no
charts, no Telegram, no running of forex_alert.py.

  python gui.py
"""

import csv
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

import chart
import config

REFRESH_MS = 60_000
CHART_POLL_MS = 100

COLUMNS = config.ALERT_HEADER

# Colour the direction cell by value. ttk.Treeview has no per-cell styling,
# so colouring happens at the tag (row) level, driven by the direction value.
DIRECTION_COLORS = {
    "BREAK_UP": "#2fbf71",
    "BREAK_DOWN": "#e5484d",
}


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


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self._chart_busy = False
        self._chart_queue = queue.Queue()
        self.chart_widget = None
        self.title("ForexOrb \u2014 Alerts")
        self.geometry("880x900")
        self.minsize(640, 600)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        top.grid_columnconfigure(0, weight=1)

        self.status = ctk.CTkLabel(top, text="", anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")

        self.refresh_btn = ctk.CTkButton(top, text="Refresh", width=100,
                                         command=self.refresh)
        self.refresh_btn.grid(row=0, column=1, padx=(12, 0))

        self.chart_btn = ctk.CTkButton(top, text="Refresh chart", width=110,
                                       command=self.refresh_chart)
        self.chart_btn.grid(row=0, column=2, padx=(8, 0))

        table_frame = ctk.CTkFrame(self)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Alerts.Treeview",
                        background="#2b2b2b", fieldbackground="#2b2b2b",
                        foreground="#e6e6e6", borderwidth=0, rowheight=26)
        style.configure("Alerts.Treeview.Heading",
                        background="#3a3a3a", foreground="#e6e6e6",
                        borderwidth=0)
        style.map("Alerts.Treeview",
                  background=[("selected", "#1f6aa5")])

        self.tree = ttk.Treeview(table_frame, columns=COLUMNS, show="headings",
                                 style="Alerts.Treeview")
        for col in COLUMNS:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=120, anchor="center", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        scroll = ttk.Scrollbar(table_frame, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns", pady=2)

        for direction, colour in DIRECTION_COLORS.items():
            self.tree.tag_configure(direction, foreground=colour)

        self.empty_label = ctk.CTkLabel(table_frame, text="No alerts yet",
                                        font=ctk.CTkFont(size=16))
        self.empty_label.grid(row=0, column=0)

        self.chart_frame = ctk.CTkFrame(self)
        self.chart_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.chart_frame.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=2)

        self.chart_loading = ctk.CTkLabel(self.chart_frame,
                                          text="Loading chart\u2026",
                                          font=ctk.CTkFont(size=14))
        self.chart_loading.grid(row=0, column=0, pady=40)

        self.refresh()
        self.after(REFRESH_MS, self.auto_refresh)
        self.after(50, self.refresh_chart)

    def refresh_chart(self):
        """Download in a worker thread, then draw on the main thread."""
        if self._chart_busy:
            return
        self._chart_busy = True
        self.chart_btn.configure(state="disabled")
        self.chart_loading.configure(text="Loading chart\u2026")
        self.chart_loading.grid()
        if self.chart_widget is not None:
            self.chart_widget.grid_remove()
        threading.Thread(target=self._download_chart, daemon=True).start()
        self._poll_chart()

    def _download_chart(self):
        # Worker thread: blocking network only. It must not touch Tk at all
        # (not even after()), so results go through a queue instead.
        try:
            self._chart_queue.put(("ok", chart.fetch_candles()))
        except Exception as exc:  # noqa: BLE001 - report, never crash the GUI
            self._chart_queue.put(("error", exc))

    def _poll_chart(self):
        """Main-thread poller: applies the worker result once it lands."""
        try:
            kind, payload = self._chart_queue.get_nowait()
        except queue.Empty:
            self.after(CHART_POLL_MS, self._poll_chart)
            return

        if kind == "ok":
            if self.chart_widget is None:
                self.chart_widget = chart.build_chart(self.chart_frame)
            self.chart_widget.render(payload)
            self.chart_widget.grid(row=0, column=0, sticky="nsew")
            self.chart_loading.grid_remove()
        else:
            self.chart_loading.configure(
                text=f"Chart unavailable \u2014 {type(payload).__name__}: {payload}")
            self.chart_loading.grid()

        self.chart_btn.configure(state="normal")
        self._chart_busy = False

    def auto_refresh(self):
        self.refresh()
        self.after(REFRESH_MS, self.auto_refresh)

    def refresh(self):
        index_col = COLUMNS.index("direction") if "direction" in COLUMNS else -1
        rows, error = read_alerts(config.ALERT_CSV)

        self.tree.delete(*self.tree.get_children())
        for row in rows:
            tag = ()
            if index_col >= 0:
                direction = row[index_col]
                if direction in DIRECTION_COLORS:
                    tag = (direction,)
            self.tree.insert("", "end", values=row, tags=tag)

        if rows:
            self.empty_label.grid_remove()
            self.tree.grid()
            self.status.configure(
                text=f"{len(rows)} alert(s) \u00b7 {config.ALERT_CSV}")
        else:
            self.tree.grid_remove()
            self.empty_label.grid()
            if error:
                self.status.configure(text=f"Error reading {config.ALERT_CSV}: {error}")
            else:
                self.status.configure(text="No alerts yet")


def main():
    ctk.set_appearance_mode("dark")
    App().mainloop()


if __name__ == "__main__":
    main()
