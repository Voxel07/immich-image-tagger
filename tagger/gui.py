"""A local desktop interface; all network and file work runs off the UI thread."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .immich import Immich
from .metadata import ExifTool
from .map_preview import MapPreview
from .progress_ui import ResultSummary, StageProgress
from .workflow import Options, apply, backup, match_images, save_report, verify


class Window:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Immich GPX Tagger")
        self.root.geometry("1240x900")
        self.root.minsize(1040, 820)
        self.events = queue.Queue()
        self.busy = False
        self.plan = self.client = None
        self.files = []
        self.matches_dirty = True
        self.map_preview = None
        self.variables = {
            "album": tk.StringVar(), "key": tk.StringVar(value=os.environ.get("IMMICH_API_KEY", "")),
            "timezone": tk.StringVar(value="UTC"), "clock": tk.StringVar(value="0"),
            "tolerance": tk.StringVar(value="60"), "gap": tk.StringVar(value="300"),
            "output": tk.StringVar(value=str(Path("runs").resolve())),
            "exiftool": tk.StringVar(value="exiftool"), "overwrite": tk.BooleanVar(value=False),
        }
        for key, variable in self.variables.items():
            variable.trace_add("write", lambda *_, key=key: self.invalidate(key))
        self.status = tk.StringVar(value="Enter the album details, then download original backups in step 1.")
        self.statistics = tk.StringVar(value="1. Back up images   >   2. Match and review   >   3. Update Immich   >   4. Verify")
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("Subtitle.TLabel", foreground="#536172")
        style.configure("Treeview", rowheight=26)
        main = ttk.Frame(root, padding=24)
        main.pack(fill="both", expand=True)
        heading = ttk.Frame(main)
        heading.pack(fill="x")
        ttk.Label(heading, text="Immich GPX Tagger", style="Title.TLabel").pack(side="left")
        self.settings_toggle = ttk.Button(heading, text="Hide settings", command=self.toggle_settings)
        self.settings_toggle.pack(side="right")
        ttk.Label(main, text="Match photo times to your route, review the results, then update GPS on the existing images.", style="Subtitle.TLabel").pack(anchor="w", pady=(4, 16))
        self.settings = ttk.Frame(main)
        self.settings.pack(fill="x")
        self.settings.columnconfigure(1, weight=1)
        self._entry(self.settings, 0, "Immich album URL", "album")
        self._entry(self.settings, 1, "API key", "key", show="\u2022")
        tracks = ttk.Frame(self.settings)
        tracks.grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Button(tracks, text="Choose GPX files\u2026", command=self.choose_files).pack(side="left")
        self.track_label = ttk.Label(tracks, text="No files selected", style="Subtitle.TLabel")
        self.track_label.pack(side="left", padx=12)
        options = ttk.LabelFrame(self.settings, text="Time matching", padding=12)
        options.grid(row=3, column=0, columnspan=2, sticky="ew", pady=6)
        fields = [("Camera timezone", "timezone"), ("Clock correction (seconds)", "clock"),
                  ("Nearest point limit (seconds)", "tolerance"), ("Interpolation gap (seconds)", "gap")]
        for col, (label, key) in enumerate(fields):
            options.columnconfigure(col, weight=1)
            ttk.Label(options, text=label).grid(row=0, column=col, sticky="w", padx=(0, 12))
            ttk.Entry(options, textvariable=self.variables[key], width=23).grid(row=1, column=col, sticky="ew", padx=(0, 12), pady=(4, 0))
        ttk.Label(options, text="Timezone is used when EXIF has no offset, e.g. Europe/Berlin. Clock correction is added before matching.", style="Subtitle.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._entry(self.settings, 4, "ExifTool executable", "exiftool")
        self._entry(self.settings, 5, "Backups / reports folder", "output")
        ttk.Checkbutton(self.settings, text="Include photos that already have GPS when matching and updating Immich", variable=self.variables["overwrite"]).grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        actions = ttk.Frame(main)
        self.actions = actions
        actions.pack(fill="x", pady=(8, 12))
        self.backup_button = ttk.Button(actions, text="1. Download backups", command=self.download)
        self.backup_button.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.match_button = ttk.Button(actions, text="2. Match GPX / review", command=self.prepare, state="disabled")
        self.match_button.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.apply_button = ttk.Button(actions, text="3. Update + 4. Verify", command=self.update, state="disabled")
        self.apply_button.grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.verify_button = ttk.Button(actions, text="4. Verify again", command=self.reverify, state="disabled")
        self.verify_button.grid(row=0, column=3, sticky="w")
        self.exclude_button = ttk.Button(actions, text="Exclude selected", command=self.exclude, state="disabled")
        self.exclude_button.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.folder_button = ttk.Button(actions, text="Open run folder", command=self.open_folder, state="disabled")
        self.folder_button.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.map_button = ttk.Button(actions, text="Preview map ↗", command=self.open_map, state="disabled")
        self.map_button.grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(actions, text="Originals stay unchanged.", style="Subtitle.TLabel").grid(row=1, column=3, sticky="w", pady=(8, 0))
        self.result_summary = ResultSummary(main)
        self.result_summary.pack(fill="x", pady=(0, 8))
        ttk.Label(main, textvariable=self.statistics, wraplength=1080).pack(anchor="w", pady=(0, 8))
        table_frame = ttk.Frame(main)
        columns = ("file", "time", "location", "status", "detail")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=8)
        for key, label, width in [("file", "Photo", 180), ("time", "Matched time (UTC)", 190),
                                  ("location", "Latitude, longitude", 170), ("status", "Status", 80), ("detail", "Details", 300)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=60, stretch=key in {"file", "detail"})
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Double-1>", lambda _: self.open_map())
        # Reserve footer space before giving the table the remaining height.
        footer = ttk.Frame(main)
        footer.pack(side="bottom", fill="x")
        self.progress = StageProgress(footer)
        self.progress.pack(fill="x", pady=(10, 5))
        ttk.Label(footer, textvariable=self.status, wraplength=1080).pack(anchor="w")
        self.log = tk.Text(footer, height=3, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="x", pady=(8, 0))
        table_frame.pack(fill="both", expand=True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def toggle_settings(self):
        if self.settings.winfo_manager():
            self.settings.pack_forget()
            self.settings_toggle.configure(text="Show settings")
        else:
            self.settings.pack(fill="x", before=self.actions)
            self.settings_toggle.configure(text="Hide settings")

    def update_summary(self, statistics):
        self.result_summary.update_counts(statistics)
        self.statistics.set(f"{statistics.get('total_assets', 0):,} assets  ·  "
                            f"{statistics.get('backed_up', 0):,} backed up  ·  "
                            f"{statistics.get('ready', 0):,} ready to update  ·  "
                            f"{statistics.get('excluded', 0):,} excluded  ·  "
                            f"{statistics.get('verified', 0):,} verified")

    def update_map(self, focus=None):
        if self.map_preview:
            self.map_preview.update(self.plan, stale=self.matches_dirty, focus=focus)

    def open_map(self):
        if self.busy or not self.plan or self.matches_dirty:
            return
        try:
            if not self.map_preview:
                self.map_preview = MapPreview()
            selected = self.table.selection()
            self.update_map(focus=selected[0] if selected else None)
            webbrowser.open(self.map_preview.url, new=2)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Map preview unavailable", str(exc))

    def _entry(self, frame, row, label, key, **kwargs):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=5)
        ttk.Entry(frame, textvariable=self.variables[key], **kwargs).grid(row=row, column=1, sticky="ew", pady=5)

    def invalidate(self, key="matching"):
        if not self.busy and hasattr(self, "apply_button"):
            if key in {"album", "key", "output"}:
                self.plan = None
                self.result_summary.update_counts({})
                self.table.delete(*self.table.get_children())
                self.statistics.set("Album or backup settings changed. Start with 1. Download backups.")
            else:
                self.matches_dirty = True
                if self.plan and self.plan.stage in {"backed_up", "review"}:
                    self.statistics.set("Matching settings changed. Run step 2 again; your backups are kept.")
            self.update_map()
            self.set_busy(False)

    def choose_files(self):
        files = filedialog.askopenfilenames(title="Select GPX tracks", filetypes=[("GPX tracks", "*.gpx"), ("All files", "*.*")])
        if files:
            self.files = [Path(path) for path in files]
            self.track_label.configure(text=f"{len(files)} file(s): " + ", ".join(path.name for path in self.files)[:110])
            self.invalidate()

    def set_busy(self, busy):
        self.busy = busy
        def state(frame):
            for child in frame.winfo_children():
                if isinstance(child, (ttk.Entry, ttk.Button, ttk.Checkbutton)):
                    child.configure(state="disabled" if busy else "normal")
                state(child)
        state(self.settings)
        self.backup_button.configure(state="disabled" if busy else "normal")
        can_match = self.plan and self.plan.stage in {"backed_up", "review"} and any(p.backup_status == "backed_up" for p in self.plan.photos)
        can_apply = self.plan and self.plan.stage == "review" and self.plan.ready and not self.matches_dirty
        can_verify = self.plan and self.plan.stage in {"verification_pending", "verified", "verification_failed"} and any(p.update_attempted for p in self.plan.photos)
        self.match_button.configure(state="normal" if not busy and can_match else "disabled")
        self.apply_button.configure(state="normal" if not busy and can_apply else "disabled")
        self.exclude_button.configure(state="normal" if not busy and can_apply else "disabled")
        self.verify_button.configure(state="normal" if not busy and can_verify else "disabled")
        self.folder_button.configure(state="disabled" if busy or not self.plan else "normal")
        self.map_button.configure(state="normal" if not busy and self.plan and self.plan.stage in {"review", "verified", "verification_failed", "verification_pending"} and not self.matches_dirty else "disabled")

    def start_worker(self, work):
        self.set_busy(True)
        self.progress.begin()
        def run():
            try:
                result = work()
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def download(self):
        try:
            if self.client:
                self.client.close()
            self.client = Immich(self.variables["album"].get(), self.variables["key"].get())
            output = Path(self.variables["output"].get())
        except (ValueError, OSError) as exc:
            messagebox.showerror("Check connection settings", str(exc))
            return
        self.plan = None
        self.matches_dirty = True
        self.table.delete(*self.table.get_children())
        self.result_summary.update_counts({})
        self.update_map()
        self.status.set("1. Downloading all available image originals for backup\u2026")
        self.start_worker(lambda: backup(self.client, output, self.emit_log, self.emit_progress))

    def prepare(self):
        if not self.plan or self.plan.stage not in {"backed_up", "review"}:
            messagebox.showerror("Backups needed", "Complete step 1 before matching GPX tracks.")
            return
        if not self.files:
            messagebox.showerror("GPX files needed", "Choose one or more GPX files first.")
            return
        try:
            v = {key: value.get() for key, value in self.variables.items()}
            options = Options(v["timezone"].strip(), float(v["clock"]), float(v["tolerance"]),
                              float(v["gap"]), v["overwrite"], Path(v["output"]))
            options.validate()
            tool = ExifTool(v["exiftool"].strip())
        except (ValueError, OSError) as exc:
            messagebox.showerror("Check settings", str(exc))
            return
        self.matches_dirty = True
        self.status.set("2. Matching GPX timestamps to local backups; preparing the review\u2026")
        self.update_map()
        self.start_worker(lambda: match_images(self.plan, tool, self.files, options, self.emit_log, self.emit_progress))

    def emit_log(self, message):
        self.events.put(("log", message))

    def emit_progress(self, event):
        self.events.put(("progress", event))

    def update(self):
        if self.plan and self.plan.stage == "review" and self.plan.ready and not self.matches_dirty:
            self.status.set("3. Updating reviewed locations, then 4. Verifying changes\u2026")
            def work():
                apply(self.client, self.plan, self.emit_log, self.emit_progress)
                verify(self.client, self.plan, self.emit_log, self.emit_progress)
                return self.plan
            self.start_worker(work)

    def reverify(self):
        if self.plan:
            self.start_worker(lambda: (verify(self.client, self.plan, self.emit_log, self.emit_progress), self.plan)[1])

    def exclude(self):
        if self.plan and self.plan.stage == "review" and not self.matches_dirty:
            for item in self.table.selection():
                photo = self.plan.photos[int(item)]
                if photo.status == "ready":
                    photo.status, photo.detail = "excluded", "Excluded during review"
            save_report(self.plan)
            self.refresh_table()
            self.set_busy(False)

    def open_folder(self):
        if self.plan:
            if sys.platform == "win32":
                os.startfile(self.plan.folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.plan.folder)])

    def refresh_table(self):
        self.table.delete(*self.table.get_children())
        if self.plan:
            self.update_summary(self.plan.statistics)
            for index, photo in enumerate(self.plan.photos):
                location = f"{photo.match.point.latitude:.6f}, {photo.match.point.longitude:.6f}" if photo.match else ""
                self.table.insert("", "end", iid=str(index), values=(photo.filename, photo.capture_time, location, photo.status, photo.detail))
            self.update_map()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress.update_progress(value)
                    if value.statistics:
                        self.update_summary(value.statistics)
                elif kind == "log":
                    self.status.set(value)
                    self.log.configure(state="normal")
                    self.log.insert("end", value + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "done":
                    self.plan = value
                    if self.plan.stage == "review":
                        self.matches_dirty = False
                        if self.settings.winfo_manager():
                            self.toggle_settings()
                    self.refresh_table()
                    self.set_busy(False)
                    self.progress.finish()
                elif kind == "error":
                    self.refresh_table()
                    self.set_busy(False)
                    self.progress.finish(failed=True)
                    self.status.set(value)
                    messagebox.showerror("Could not complete the operation", value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo("Operation in progress", "Wait for the current operation to finish before closing the app.")
            return
        if self.client:
            self.client.close()
        if self.map_preview:
            self.map_preview.close()
        self.root.destroy()


def launch():
    root = tk.Tk()
    Window(root)
    root.mainloop()
