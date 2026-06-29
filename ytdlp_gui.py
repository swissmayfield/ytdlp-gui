"""
ytdlp_gui.py — a small, readable GUI front-end for yt-dlp.

What it does:
  - Queue up one or more URLs, pick a quality/format and a folder, hit Download.
  - "Fetch" shows a video's title/duration/channel and its real available formats
    so you can pick a specific one instead of guessing.
  - Optional subtitles, SponsorBlock segment removal, and a download archive
    (skip things you've already grabbed).
  - Optionally upload each finished file to a remote (cloud / SFTP / S3) via rclone.
  - An "Update yt-dlp" button keeps the downloader current when sites change.
  - Downloads run one at a time with a live progress bar and speed / ETA readout.
  - Your settings are remembered between runs.

Why it's built this way:
  - tkinter ships with Python, so there's nothing extra to install.
  - yt-dlp is called as a *subprocess* (the same thing you'd type in a terminal)
    instead of imported as a library — clean separation, easy to reason about.
  - Long operations run on a worker *thread* so the window never freezes. Worker
    threads can't touch tkinter directly (it isn't thread-safe), so they push
    messages into a thread-safe Queue that the GUI drains on a timer.

Run it:  py ytdlp_gui.py
"""

import os
import re
import sys
import json
import shlex
import threading
import subprocess
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ---------------------------------------------------------------------------
# Format presets: label shown in the dropdown -> list of yt-dlp arguments.
# -f is yt-dlp's "format selector". "bv*+ba/b" means "best video + best audio,
# or fall back to the best single file". The [height<=N] filters cap resolution.
# ---------------------------------------------------------------------------
FORMAT_PRESETS = {
    "Best quality (video + audio)": ["-f", "bv*+ba/b"],
    "1080p":                        ["-f", "bv*[height<=1080]+ba/b[height<=1080]"],
    "720p":                         ["-f", "bv*[height<=720]+ba/b[height<=720]"],
    "480p":                         ["-f", "bv*[height<=480]+ba/b[height<=480]"],
    "Audio only (MP3)":             ["-f", "ba/b", "-x", "--audio-format", "mp3"],
}

# Shown in the "specific format" box until you Fetch a video.
USE_PRESET = "(use preset above)"

# Patterns for the line yt-dlp prints while downloading, e.g.:
#   [download]  42.7% of   10.00MiB at    1.50MiB/s ETA 00:04
PERCENT_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
SPEED_RE   = re.compile(r"\bat\s+([\d.]+\s*[KMGT]?i?B/s)")
ETA_RE     = re.compile(r"\bETA\s+([\d:]+)")

# Per-user config + download archive location.
CONFIG_DIR   = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "ytdlp-gui"
)
CONFIG_PATH  = os.path.join(CONFIG_DIR, "config.json")
ARCHIVE_PATH = os.path.join(CONFIG_DIR, "archive.txt")


class YtDlpGui:
    def __init__(self, root):
        self.root = root
        self.proc = None                # the running subprocess, if any
        self.log_queue = queue.Queue()  # worker thread -> GUI messages
        self.jobs = []                  # URLs still to download this run
        self.results = []               # (url, exit_code) per finished item
        self.current_url = ""           # URL of the item currently downloading
        self.format_map = {}            # "display label" -> yt-dlp format_id
        self.cancelled = False
        self.busy = False               # True while any subprocess is running
        self.total_jobs = 0
        self.done_jobs = 0

        root.title("yt-dlp GUI")
        root.geometry("780x780")
        root.minsize(700, 680)

        # --- restore saved settings (or sensible defaults) -----------------
        cfg = self._load_config()
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")

        self.url_var          = tk.StringVar()
        self.info_var         = tk.StringVar(value="No video fetched yet.")
        self.dir_var          = tk.StringVar(value=cfg.get("dir", default_dir))
        self.format_var       = tk.StringVar(value=cfg.get("format", "Best quality (video + audio)"))
        self.specific_var     = tk.StringVar(value=USE_PRESET)
        self.playlist_var     = tk.BooleanVar(value=cfg.get("playlist", False))
        self.metadata_var     = tk.BooleanVar(value=cfg.get("metadata", True))
        self.subs_var         = tk.BooleanVar(value=cfg.get("subs", False))
        self.sublang_var      = tk.StringVar(value=cfg.get("sublang", "en"))
        self.sponsorblock_var = tk.BooleanVar(value=cfg.get("sponsorblock", False))
        self.archive_var      = tk.BooleanVar(value=cfg.get("archive", False))
        self.extra_var        = tk.StringVar(value=cfg.get("extra", ""))
        self.rclone_var       = tk.StringVar(value=cfg.get("rclone_remote", ""))
        self.rclone_move_var  = tk.BooleanVar(value=cfg.get("rclone_move", False))
        self.status_var       = tk.StringVar(value="Idle")

        if self.format_var.get() not in FORMAT_PRESETS:
            self.format_var.set("Best quality (video + audio)")

        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)

    # -- UI layout ----------------------------------------------------------
    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        r = 0

        # URL row + Fetch / Add buttons
        ttk.Label(frm, text="Video URL:").grid(row=r, column=0, sticky="w", **pad)
        url_entry = ttk.Entry(frm, textvariable=self.url_var)
        url_entry.grid(row=r, column=1, sticky="ew", **pad)
        url_entry.bind("<Return>", lambda _e: self._add_to_queue())
        url_entry.focus()
        urlbtns = ttk.Frame(frm)
        urlbtns.grid(row=r, column=2, **pad)
        self.fetch_btn = ttk.Button(urlbtns, text="Fetch", width=7, command=self._fetch_info)
        self.fetch_btn.pack(side="left")
        self.add_btn = ttk.Button(urlbtns, text="Add", width=7, command=self._add_to_queue)
        self.add_btn.pack(side="left", padx=(4, 0))
        r += 1

        # Fetched-info line
        ttk.Label(frm, textvariable=self.info_var, foreground="#0a7").grid(
            row=r, column=1, columnspan=2, sticky="w", padx=8)
        r += 1

        # Queue list + side buttons
        ttk.Label(frm, text="Queue:").grid(row=r, column=0, sticky="nw", **pad)
        qframe = ttk.Frame(frm)
        qframe.grid(row=r, column=1, sticky="ew", **pad)
        qframe.columnconfigure(0, weight=1)
        self.queue_list = tk.Listbox(qframe, height=4)
        self.queue_list.grid(row=0, column=0, sticky="ew")
        qscroll = ttk.Scrollbar(qframe, command=self.queue_list.yview)
        qscroll.grid(row=0, column=1, sticky="ns")
        self.queue_list["yscrollcommand"] = qscroll.set
        qbtns = ttk.Frame(frm)
        qbtns.grid(row=r, column=2, sticky="n", **pad)
        self.remove_btn = ttk.Button(qbtns, text="Remove", command=self._remove_selected)
        self.remove_btn.pack(fill="x")
        self.clear_btn = ttk.Button(qbtns, text="Clear", command=self._clear_queue)
        self.clear_btn.pack(fill="x", pady=(4, 0))
        r += 1

        # Format preset
        ttk.Label(frm, text="Format:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Combobox(frm, textvariable=self.format_var, values=list(FORMAT_PRESETS.keys()),
                     state="readonly").grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1

        # Specific format (populated after Fetch)
        ttk.Label(frm, text="Specific:").grid(row=r, column=0, sticky="w", **pad)
        self.specific_combo = ttk.Combobox(frm, textvariable=self.specific_var,
                                            values=[USE_PRESET], state="readonly")
        self.specific_combo.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1

        # Folder row
        ttk.Label(frm, text="Save to:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.dir_var).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self._choose_dir).grid(row=r, column=2, **pad)
        r += 1

        # Options group
        opts = ttk.LabelFrame(frm, text="Options", padding=6)
        opts.grid(row=r, column=0, columnspan=3, sticky="ew", **pad)
        row1 = ttk.Frame(opts); row1.pack(fill="x")
        ttk.Checkbutton(row1, text="Whole playlist", variable=self.playlist_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row1, text="Embed metadata + thumbnail", variable=self.metadata_var).pack(side="left")
        row2 = ttk.Frame(opts); row2.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(row2, text="Subtitles", variable=self.subs_var).pack(side="left")
        ttk.Label(row2, text="lang:").pack(side="left", padx=(4, 2))
        ttk.Entry(row2, textvariable=self.sublang_var, width=8).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row2, text="Remove sponsors", variable=self.sponsorblock_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row2, text="Skip already-downloaded", variable=self.archive_var).pack(side="left")
        r += 1

        # Extra yt-dlp arguments (advanced, optional). Anything typed here is
        # passed straight through to yt-dlp, so the GUI can reach any flag it
        # doesn't have a checkbox for (proxy, rate limit, clip sections, etc.).
        ttk.Label(frm, text="Extra args:").grid(row=r, column=0, sticky="w", **pad)
        extra_entry = ttk.Entry(frm, textvariable=self.extra_var)
        extra_entry.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1

        # Optional: upload each finished file to a remote with rclone. The remote
        # must already be set up via `rclone config` (e.g. "gdrive:Movies").
        ttk.Label(frm, text="Upload to:").grid(row=r, column=0, sticky="w", **pad)
        up = ttk.Frame(frm)
        up.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        up.columnconfigure(0, weight=1)
        ttk.Entry(up, textvariable=self.rclone_var).grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(up, text="delete local after upload",
                        variable=self.rclone_move_var).grid(row=0, column=1, padx=(8, 0))
        r += 1

        # Action buttons
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", **pad)
        self.download_btn = ttk.Button(btns, text="Download", command=self._start_download)
        self.download_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.update_btn = ttk.Button(btns, text="Update yt-dlp", command=self._update_ytdlp)
        self.update_btn.pack(side="right")
        r += 1

        # Progress + status
        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.grid(row=r, column=0, columnspan=3, sticky="ew", **pad)
        r += 1
        ttk.Label(frm, textvariable=self.status_var).grid(
            row=r, column=0, columnspan=3, sticky="w", **pad)
        r += 1

        # Log output
        ttk.Label(frm, text="Output:").grid(row=r, column=0, sticky="w", **pad)
        r += 1
        self.log = tk.Text(frm, height=11, wrap="word", state="disabled",
                           bg="#111", fg="#ddd", font=("Consolas", 9))
        self.log.grid(row=r, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(r, weight=1)
        scroll = ttk.Scrollbar(frm, command=self.log.yview)
        scroll.grid(row=r, column=3, sticky="ns")
        self.log["yscrollcommand"] = scroll.set

    # -- Queue management ---------------------------------------------------
    def _add_to_queue(self):
        url = self.url_var.get().strip()
        if url:
            self.queue_list.insert("end", url)
            self.url_var.set("")

    def _remove_selected(self):
        for i in reversed(self.queue_list.curselection()):
            self.queue_list.delete(i)

    def _clear_queue(self):
        self.queue_list.delete(0, "end")

    # -- Settings persistence ----------------------------------------------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        data = {
            "dir": self.dir_var.get(),
            "format": self.format_var.get(),
            "playlist": self.playlist_var.get(),
            "metadata": self.metadata_var.get(),
            "subs": self.subs_var.get(),
            "sublang": self.sublang_var.get(),
            "sponsorblock": self.sponsorblock_var.get(),
            "archive": self.archive_var.get(),
            "extra": self.extra_var.get(),
            "rclone_remote": self.rclone_var.get(),
            "rclone_move": self.rclone_move_var.get(),
        }
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass  # a failed settings write shouldn't interrupt the user

    # -- Fetch info ---------------------------------------------------------
    def _fetch_info(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Type a URL to fetch info for.")
            return
        if self.busy:
            return
        self._set_busy(True)
        self.info_var.set("Fetching…")
        self.status_var.set("Fetching video info…")
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url):
        """Worker thread: ask yt-dlp for the video's metadata as JSON."""
        cmd = [sys.executable, "-m", "yt_dlp", "--dump-json", "--no-playlist",
               "--no-warnings", url]
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", creationflags=flags)
            out = res.stdout.strip()
            if not out:
                raise RuntimeError(res.stderr.strip() or "no data returned")
            data = json.loads(out.splitlines()[0])
            self.log_queue.put(("info", data))
        except Exception as exc:  # noqa: BLE001
            self.log_queue.put(("infoerror", str(exc)))

    def _on_info(self, data):
        self._set_busy(False)
        title = data.get("title", "?")
        dur = self._fmt_duration(data.get("duration"))
        who = data.get("uploader") or data.get("channel") or ""
        self.info_var.set(f"{title}   •   {dur}   •   {who}".strip(" •"))

        # Build the "specific format" dropdown from the real available formats.
        self.format_map = {}
        display = [USE_PRESET]
        for f in data.get("formats", []):
            vcodec, acodec = f.get("vcodec"), f.get("acodec")
            if vcodec == "none" and acodec == "none":
                continue                      # skip storyboards / metadata-only
            if f.get("ext") == "mhtml":
                continue
            fid = f.get("format_id", "?")
            ext = f.get("ext", "")
            height = f.get("height")
            res = f.get("resolution") or (f"{height}p" if height else (f.get("format_note") or "audio"))
            size = self._fmt_size(f.get("filesize") or f.get("filesize_approx"))
            tag = " (video only)" if (acodec == "none" and vcodec != "none") else (
                  " (audio)" if vcodec == "none" else "")
            label = f"{fid} — {ext} {res} {size}{tag}".replace("  ", " ").strip()
            display.append(label)
            self.format_map[label] = fid
        self.specific_combo["values"] = display
        self.specific_var.set(USE_PRESET)
        self.status_var.set("Fetched. Pick a specific format or just Download.")

    def _on_info_error(self, msg):
        self._set_busy(False)
        self.info_var.set("Couldn't fetch info.")
        self.status_var.set("Fetch failed.")
        self._log_line(f"\n[fetch error] {msg}\n")

    # -- Update yt-dlp ------------------------------------------------------
    def _update_ytdlp(self):
        if self.busy:
            return
        self._set_busy(True)
        self.status_var.set("Updating yt-dlp…")
        self._log_line("\n=== Updating yt-dlp (pip install -U yt-dlp) ===\n")
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
        threading.Thread(target=self._stream_worker, args=(cmd, "update_done"),
                         daemon=True).start()

    # -- Actions ------------------------------------------------------------
    def _choose_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get())
        if chosen:
            self.dir_var.set(chosen)

    def _build_command(self, url):
        """Assemble the yt-dlp argument list from the current UI state."""
        cmd = [sys.executable, "-m", "yt_dlp", "--newline", "--no-color"]

        # A specific format (from Fetch) overrides the preset. We append best
        # audio with a fallback: "<id>+ba/<id>" merges audio if the chosen
        # stream is video-only, else just uses the stream as-is.
        spec = self.specific_var.get()
        if spec in self.format_map:
            fid = self.format_map[spec]
            cmd += ["-f", f"{fid}+ba/{fid}"]
        else:
            cmd += FORMAT_PRESETS[self.format_var.get()]

        if not self.playlist_var.get():
            cmd += ["--no-playlist"]
        if self.metadata_var.get():
            cmd += ["--embed-metadata", "--embed-thumbnail"]
        if self.subs_var.get():
            lang = self.sublang_var.get().strip() or "en"
            cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", lang, "--embed-subs"]
        if self.sponsorblock_var.get():
            cmd += ["--sponsorblock-remove", "default"]
        if self.archive_var.get():
            os.makedirs(CONFIG_DIR, exist_ok=True)
            cmd += ["--download-archive", ARCHIVE_PATH]

        # Pass any advanced flags straight through. shlex handles quoted values
        # like:  --download-sections "*10:00-10:30"
        extra = self.extra_var.get().strip()
        if extra:
            try:
                cmd += shlex.split(extra)
            except ValueError:
                cmd += extra.split()  # unbalanced quotes -> fall back to naive split

        # Optional rclone upload. yt-dlp's --exec runs after the file is finished
        # and moved to its final name; %(filepath)q is the shell-quoted path, so
        # this becomes e.g.  rclone copy "C:\...\video.mp4" "gdrive:Movies"
        remote = self.rclone_var.get().strip()
        if remote:
            verb = "move" if self.rclone_move_var.get() else "copy"
            cmd += ["--exec", f'after_move:rclone {verb} %(filepath)q "{remote}"']

        out_template = os.path.join(self.dir_var.get(), "%(title)s [%(id)s].%(ext)s")
        cmd += ["-o", out_template, url]
        return cmd

    def _start_download(self):
        if self.busy:
            return
        if self.queue_list.size() == 0 and self.url_var.get().strip():
            self._add_to_queue()
        if self.queue_list.size() == 0:
            messagebox.showwarning("Nothing queued", "Add at least one URL to the queue.")
            return
        if not os.path.isdir(self.dir_var.get()):
            messagebox.showwarning("Bad folder", "Pick a valid download folder.")
            return

        self._save_config()
        self.jobs = list(self.queue_list.get(0, "end"))
        self.total_jobs = len(self.jobs)
        self.done_jobs = 0
        self.cancelled = False
        self.results = []
        self._clear_log()
        self._set_busy(True, downloading=True)
        self._run_next()

    def _run_next(self):
        """Start the next queued URL, or finish if the queue is empty."""
        if self.cancelled or not self.jobs:
            self._all_done()
            return
        url = self.jobs.pop(0)
        self.current_url = url
        self.done_jobs += 1
        self.progress["value"] = 0
        self.status_var.set(f"Downloading {self.done_jobs} of {self.total_jobs}…")
        cmd = self._build_command(url)
        self._log_line(f"\n=== [{self.done_jobs}/{self.total_jobs}] {url} ===\n")
        threading.Thread(target=self._stream_worker, args=(cmd, "done"), daemon=True).start()

    def _stream_worker(self, cmd, done_kind):
        """Worker: run a command and forward each output line, then a 'done'."""
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=flags,
            )
            for line in self.proc.stdout:
                self.log_queue.put(("line", line))
            code = self.proc.wait()
            self.log_queue.put((done_kind, code))
        except Exception as exc:  # noqa: BLE001
            self.log_queue.put(("line", f"\nERROR: {exc}\n"))
            self.log_queue.put((done_kind, -1))
        finally:
            self.proc = None

    def _cancel(self):
        self.cancelled = True
        self.jobs = []
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._log_line("\n[cancelled]\n")

    # -- Queue / log plumbing ----------------------------------------------
    def _drain_queue(self):
        """Runs on the GUI thread ~10x/sec; applies worker messages safely."""
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "line":
                    self._log_line(payload)
                    self._update_status_from(payload)
                elif kind == "done":
                    self._on_job_finished(payload)
                elif kind == "update_done":
                    self._on_update_finished(payload)
                elif kind == "info":
                    self._on_info(payload)
                elif kind == "infoerror":
                    self._on_info_error(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _update_status_from(self, line):
        """Pull percent / speed / ETA out of a yt-dlp progress line."""
        pct = PERCENT_RE.search(line)
        if not pct:
            return
        self.progress["value"] = float(pct.group(1))
        speed = SPEED_RE.search(line)
        eta = ETA_RE.search(line)
        parts = [f"[{self.done_jobs}/{self.total_jobs}]  {pct.group(1)}%"]
        if speed:
            parts.append(speed.group(1).replace(" ", ""))
        if eta:
            parts.append(f"ETA {eta.group(1)}")
        self.status_var.set("   ".join(parts))

    def _on_job_finished(self, code):
        self.results.append((self.current_url, code))
        if code == 0:
            self._log_line("\n Done.\n")
        elif not self.cancelled:
            self._log_line(f"\n Item finished with exit code {code}.\n")
        self._run_next()

    def _on_update_finished(self, code):
        self._set_busy(False)
        self.status_var.set("yt-dlp updated." if code == 0 else "Update failed.")
        # Show the version we ended up with.
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ver = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                                 capture_output=True, text=True, creationflags=flags).stdout.strip()
            if ver:
                self._log_line(f"\nyt-dlp version is now {ver}\n")
        except Exception:  # noqa: BLE001
            pass

    def _all_done(self):
        self._set_busy(False)
        ok = sum(1 for _u, c in self.results if c == 0)
        failed = [(u, c) for u, c in self.results if c != 0]
        not_started = self.total_jobs - len(self.results)

        if self.cancelled:
            self.status_var.set(f"Cancelled — {ok} done, {not_started} not started.")
        else:
            self.progress["value"] = 100
            self.status_var.set(f"Finished — {ok}/{self.total_jobs} ok, {len(failed)} failed.")

        # Detailed summary in the log so batch results are easy to scan.
        self._log_line("\n" + "=" * 40 + "\n")
        self._log_line(f"Summary: {ok} succeeded, {len(failed)} failed"
                       + (f", {not_started} not started (cancelled)" if self.cancelled else "")
                       + ".\n")
        for url, code in failed:
            self._log_line(f"  FAILED (exit {code}): {url}\n")

    def _set_busy(self, busy, downloading=False):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.download_btn, self.fetch_btn, self.add_btn,
                  self.remove_btn, self.clear_btn, self.update_btn):
            w["state"] = state
        # Cancel only makes sense while a download queue is running.
        self.cancel_btn["state"] = "normal" if (busy and downloading) else "disabled"

    def _log_line(self, text):
        self.log["state"] = "normal"
        self.log.insert("end", text)
        self.log.see("end")
        self.log["state"] = "disabled"

    def _clear_log(self):
        self.log["state"] = "normal"
        self.log.delete("1.0", "end")
        self.log["state"] = "disabled"

    def _on_close(self):
        self._save_config()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.root.destroy()

    # -- Small formatting helpers ------------------------------------------
    @staticmethod
    def _fmt_duration(seconds):
        if not seconds:
            return "?:??"
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @staticmethod
    def _fmt_size(num_bytes):
        if not num_bytes:
            return ""
        size = float(num_bytes)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                return f"~{size:.0f}{unit}"
            size /= 1024


def main():
    root = tk.Tk()
    YtDlpGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
