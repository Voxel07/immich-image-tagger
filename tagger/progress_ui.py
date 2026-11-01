"""Stage progress and a compact result summary for the desktop interface."""
import time
import tkinter as tk
from tkinter import ttk

from .workflow import Progress

STAGES = (("backup", "Back up"), ("matching", "Match & review"),
          ("updating", "Update locations"), ("verifying", "Verify changes"))


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def progress_text(event: Progress, elapsed: float) -> tuple[str, str]:
    if event.total is None:
        return "Preparing…", f"Elapsed {duration(elapsed)} · Waiting for album details"
    percent = 100 if event.total == 0 else min(100, event.completed / event.total * 100)
    count = f"{event.completed:,} / {event.total:,} processed · {percent:.0f}%"
    if event.completed >= event.total:
        eta = "Stage complete"
    elif event.completed and elapsed >= 1:
        eta = "About " + duration(elapsed / event.completed * (event.total - event.completed)) + " remaining"
    else:
        eta = "Estimating time remaining…"
    return count, f"Elapsed {duration(elapsed)} · {eta}"


class ResultSummary(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.counts = {}
        for index, (key, label, color) in enumerate((
                ("matched", "Matched", "#16724a"), ("skipped", "Skipped", "#526174"),
                ("unmatched", "No GPX match", "#a45a08"), ("errors", "Errors", "#b12e3c"))):
            self.columnconfigure(index, weight=1)
            card = ttk.Frame(self, padding=(14, 7), relief="solid", borderwidth=1)
            card.grid(row=0, column=index, sticky="ew", padx=(0, 8 if index < 3 else 0))
            self.counts[key] = tk.StringVar(value="0")
            ttk.Label(card, textvariable=self.counts[key], font=("Segoe UI", 21, "bold"), foreground=color).pack(side="left")
            ttk.Label(card, text=label, foreground=color).pack(side="left", padx=(10, 0))

    def update_counts(self, statistics: dict):
        for key, variable in self.counts.items():
            variable.set(f"{statistics.get(key, 0):,}")


class StageProgress(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=(12, 8), relief="solid", borderwidth=1)
        style = ttk.Style()
        style.configure("Stage.Horizontal.TProgressbar", thickness=14, background="#2673d9")
        self.chips = []
        stages = ttk.Frame(self)
        stages.pack(fill="x", pady=(0, 7))
        for index, (_, label) in enumerate(STAGES):
            stages.columnconfigure(index, weight=1)
            chip = ttk.Label(stages, text=f"{index + 1}. {label}", foreground="#738194")
            chip.grid(row=0, column=index, sticky="w")
            self.chips.append(chip)
        line = ttk.Frame(self)
        line.pack(fill="x")
        self.title = tk.StringVar(value="Ready to start")
        self.count = tk.StringVar(value="0%")
        ttk.Label(line, textvariable=self.title, font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(line, textvariable=self.count).pack(side="right")
        self.bar = ttk.Progressbar(self, maximum=100, mode="determinate", style="Stage.Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=6)
        bottom = ttk.Frame(self)
        bottom.pack(fill="x")
        self.current = tk.StringVar(value="Original backups are downloaded before any matching or updates.")
        self.timing = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.current, foreground="#526174", width=50).pack(side="left", fill="x", expand=True)
        ttk.Label(bottom, textvariable=self.timing, foreground="#526174").pack(side="right")
        self.event = None
        self.started = 0.0
        self.running = False
        self.after(500, self.tick)

    def begin(self):
        self.event = None
        self.running = True
        self.title.set("Preparing…")
        self.count.set("")
        self.current.set("Connecting and preparing this stage…")
        self.timing.set("")
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)

    def update_progress(self, event: Progress):
        previous = self.event
        if previous is None or previous.stage != event.stage:
            self.started = time.monotonic()
        self.event, self.running = event, True
        index = next(index for index, (key, _) in enumerate(STAGES) if key == event.stage)
        self.title.set(f"Step {index + 1} of 4 · {STAGES[index][1]}")
        for chip_index, chip in enumerate(self.chips):
            chip.configure(foreground="#16724a" if chip_index < index else "#165eb5" if chip_index == index else "#738194",
                           font=("Segoe UI", 9, "bold" if chip_index == index else "normal"))
        if event.total is not None:
            self.bar.stop()
            self.bar.configure(mode="determinate", value=100 if event.total == 0 else event.completed / event.total * 100)
        self.current.set(event.current[:65] if event.current else "Preparing this stage…")
        self.render_timing()

    def render_timing(self):
        if self.event:
            count, timing = progress_text(self.event, time.monotonic() - self.started)
            self.count.set(count)
            self.timing.set(timing)

    def finish(self, failed: bool = False):
        value, mode = self.bar["value"], str(self.bar["mode"])
        self.bar.stop()
        if mode == "determinate":
            self.bar["value"] = value
        self.render_timing()
        self.running = False
        if failed:
            self.title.set(self.title.get() + " · Stopped")
        elif self.event:
            self.current.set("Stage finished. Review the results before continuing.")

    def tick(self):
        if self.running:
            self.render_timing()
        self.after(500, self.tick)
