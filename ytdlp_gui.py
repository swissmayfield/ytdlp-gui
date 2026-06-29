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

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def ytdlp_base():
    """Command prefix for invoking yt-dlp.

    From source we run the module with the current interpreter. In a packaged
    PyInstaller build, sys.executable is the GUI .exe (not a Python), so we use a
    bundled yt-dlp.exe if present, otherwise one found on PATH.
    """
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "yt-dlp.exe")
        if os.path.exists(bundled):
            return [bundled]
        found = shutil.which("yt-dlp")
        return [found] if found else ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


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

# A safe rclone remote looks like "name:path" with no shell metacharacters. It is
# interpolated into yt-dlp's --exec string (which yt-dlp runs via the shell), so
# anything that could break out of the surrounding quotes is rejected.
RCLONE_REMOTE_RE = re.compile(r"^[A-Za-z0-9_-]+:[A-Za-z0-9 _./@~-]*$")

# Per-user config + download archive location.
CONFIG_DIR   = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "ytdlp-gui"
)
CONFIG_PATH  = os.path.join(CONFIG_DIR, "config.json")
ARCHIVE_PATH = os.path.join(CONFIG_DIR, "archive.txt")

# Light/dark colour palettes. Applied on top of ttk's "clam" theme, which (unlike
# the native Windows themes) honours custom colours for buttons, entries, etc.
THEMES = {
    "light": {
        "bg": "#f0f0f0", "surface": "#ffffff", "fg": "#1a1a1a",
        "accent": "#2d7d46", "active": "#dcdcdc",
        "log_bg": "#fbfbfb", "log_fg": "#1a1a1a", "info": "#0a7d57",
    },
    "dark": {
        "bg": "#1e1e1e", "surface": "#2b2b2b", "fg": "#e6e6e6",
        "accent": "#3d7eaa", "active": "#3a3a3a",
        "log_bg": "#111111", "log_fg": "#dddddd", "info": "#4ec9b0",
    },
}

APP_VERSION = "1.0"
GITHUB_URL = "https://github.com/swissmayfield/ytdlp-gui"

HELP_HOWTO = """\
1. Paste a video URL at the top.
2. (Optional) Click Fetch to load the title and the formats available for that
   video, then pick one under "Specific" (Advanced view).
3. Click Add to queue it, or just click Download to grab the single URL.
4. Choose a Format and a Save-to folder, then click Download.

Use the View menu to switch between Simple and Advanced, or toggle Dark mode.
If a site stops working, try Tools > Update yt-dlp.

Note: this does not bypass DRM. Use it only for content you're allowed
to download - your own uploads, Creative Commons / public-domain video,
or sites whose terms permit it.
"""

# Reference of useful yt-dlp flags, grouped by purpose. Shown in the in-app
# glossary; clicking an entry drops the flag into the Extra args box. Values are
# sample defaults the user can edit. Options the form already controls (output
# path, format, subtitles, --no-playlist) are intentionally left out.
EXTRA_ARGS_GLOSSARY = [
    ("Speed & network", [
        ("-N 4", "Download video fragments in parallel (often much faster)."),
        ("--limit-rate 2M", "Cap download speed (2M = 2 MB/s, 500K = 500 KB/s)."),
        ("--retries 10", "Retry a failed download up to N times."),
        ("--proxy http://host:port", "Route the download through a proxy server."),
    ]),
    ("Clips & sections", [
        ('--download-sections "*10:00-10:30"', "Download only that time range of the video."),
        ("--force-keyframes-at-cuts", "Cleaner cuts for --download-sections (re-encodes at the cut)."),
    ]),
    ("Access & login", [
        ("--cookies-from-browser chrome", "Use your logged-in browser session (age-gated / members-only)."),
        ("--cookies cookies.txt", "Use an exported cookies.txt file instead."),
        ("--geo-bypass", "Attempt to bypass simple geographic restrictions."),
    ]),
    ("Output & metadata", [
        ("--write-thumbnail", "Also save the thumbnail as a separate image file."),
        ("--write-description", "Save the video description to a .description file."),
        ("--write-info-json", "Save all metadata to a .info.json file."),
        ("--embed-chapters", "Embed chapter markers into the file."),
        ("--restrict-filenames", "Use safe ASCII-only characters in filenames."),
    ]),
    ("Format & container", [
        ('-S "res:1080"', "Prefer formats up to 1080p (sort by resolution)."),
        ("--remux-video mp4", "Repackage into MP4 without re-encoding (fast)."),
        ("--recode-video mp4", "Re-encode into MP4 for compatibility (slow)."),
        ("--merge-output-format mkv", "Use MKV as the container for merged video+audio."),
    ]),
    ("Playlists", [
        ("--playlist-items 1-5,8", "Download only these items from a playlist."),
        ("--max-downloads 10", "Stop after N downloads."),
        ("--playlist-reverse", "Download a playlist in reverse order."),
        ("--dateafter 20240101", "Only download videos uploaded on/after this date."),
    ]),
    ("SponsorBlock", [
        ("--sponsorblock-remove all", "Remove all SponsorBlock categories, not just the defaults."),
        ("--sponsorblock-mark all", "Mark segments as chapters instead of removing them."),
    ]),
]

HELP_ABOUT = f"""\
yt-dlp GUI   v{APP_VERSION}

A small, dependency-free desktop front-end for yt-dlp, built with Python's
standard-library tkinter.

{GITHUB_URL}

yt-dlp does not bypass DRM; use this only for content you're allowed
to download.
"""


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
        self.dark_var         = tk.BooleanVar(value=cfg.get("dark", False))
        self.view_var         = tk.StringVar(value=cfg.get("view", "simple"))
        self.status_var       = tk.StringVar(value="Idle")

        if self.format_var.get() not in FORMAT_PRESETS:
            self.format_var.set("Best quality (video + audio)")

        self._build_widgets()
        self._apply_theme()
        self._apply_view()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)

    # -- UI layout ----------------------------------------------------------
    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}
        self.style = ttk.Style(self.root)
        self._build_menu()
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        self.advanced_widgets = []   # rows hidden in Simple view
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
        self.info_label = ttk.Label(frm, textvariable=self.info_var)
        self.info_label.grid(row=r, column=1, columnspan=2, sticky="w", padx=8)
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

        # Specific format (populated after Fetch) -- advanced only
        spec_lbl = ttk.Label(frm, text="Specific:")
        spec_lbl.grid(row=r, column=0, sticky="w", **pad)
        self.specific_combo = ttk.Combobox(frm, textvariable=self.specific_var,
                                            values=[USE_PRESET], state="readonly")
        self.specific_combo.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        self.advanced_widgets += [spec_lbl, self.specific_combo]
        r += 1

        # Folder row
        ttk.Label(frm, text="Save to:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.dir_var).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self._choose_dir).grid(row=r, column=2, **pad)
        r += 1

        # Basic options -- always visible
        opts = ttk.LabelFrame(frm, text="Options", padding=6)
        opts.grid(row=r, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(opts, text="Whole playlist", variable=self.playlist_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(opts, text="Embed metadata + thumbnail", variable=self.metadata_var).pack(side="left")
        r += 1

        # Advanced options -- hidden in Simple view
        adv = ttk.LabelFrame(frm, text="Advanced options", padding=6)
        adv.grid(row=r, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(adv, text="Subtitles", variable=self.subs_var).pack(side="left")
        ttk.Label(adv, text="lang:").pack(side="left", padx=(4, 2))
        ttk.Entry(adv, textvariable=self.sublang_var, width=8).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(adv, text="Remove sponsors", variable=self.sponsorblock_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(adv, text="Skip already-downloaded", variable=self.archive_var).pack(side="left")
        self.advanced_widgets.append(adv)
        r += 1

        # Extra yt-dlp arguments (advanced, optional). Anything typed here is
        # passed straight through to yt-dlp, so the GUI can reach any flag it
        # doesn't have a checkbox for (proxy, rate limit, clip sections, etc.).
        extra_lbl = ttk.Label(frm, text="Extra args:")
        extra_lbl.grid(row=r, column=0, sticky="w", **pad)
        extra_entry = ttk.Entry(frm, textvariable=self.extra_var)
        extra_entry.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        self.advanced_widgets += [extra_lbl, extra_entry]
        r += 1

        # Optional: upload each finished file to a remote with rclone. The remote
        # must already be set up via `rclone config` (e.g. "gdrive:Movies").
        up_lbl = ttk.Label(frm, text="Upload to:")
        up_lbl.grid(row=r, column=0, sticky="w", **pad)
        up = ttk.Frame(frm)
        up.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        up.columnconfigure(0, weight=1)
        ttk.Entry(up, textvariable=self.rclone_var).grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(up, text="delete local after upload",
                        variable=self.rclone_move_var).grid(row=0, column=1, padx=(8, 0))
        self.advanced_widgets += [up_lbl, up]
        r += 1

        # Action buttons (Update yt-dlp and Dark mode now live in the menu bar)
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", **pad)
        self.download_btn = ttk.Button(btns, text="Download", command=self._start_download)
        self.download_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
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

    def _apply_theme(self):
        """Recolour every widget for the current light/dark choice."""
        pal = THEMES["dark" if self.dark_var.get() else "light"]
        s = self.style
        s.theme_use("clam")  # clam honours custom colours; the native themes don't
        self.root.configure(bg=pal["bg"])

        s.configure(".", background=pal["bg"], foreground=pal["fg"],
                    fieldbackground=pal["surface"], bordercolor=pal["active"],
                    lightcolor=pal["bg"], darkcolor=pal["bg"])
        s.configure("TFrame", background=pal["bg"])
        s.configure("TLabel", background=pal["bg"], foreground=pal["fg"])
        s.configure("TLabelframe", background=pal["bg"])
        s.configure("TLabelframe.Label", background=pal["bg"], foreground=pal["fg"])
        s.configure("TCheckbutton", background=pal["bg"], foreground=pal["fg"])
        s.map("TCheckbutton", background=[("active", pal["bg"])])
        s.configure("TButton", background=pal["surface"], foreground=pal["fg"])
        s.map("TButton",
              background=[("active", pal["active"]), ("disabled", pal["bg"])],
              foreground=[("disabled", pal["active"])])
        s.configure("TEntry", fieldbackground=pal["surface"], foreground=pal["fg"],
                    insertcolor=pal["fg"])
        s.configure("TCombobox", fieldbackground=pal["surface"], foreground=pal["fg"],
                    arrowcolor=pal["fg"])
        s.map("TCombobox", fieldbackground=[("readonly", pal["surface"])],
              foreground=[("readonly", pal["fg"])])
        s.configure("Horizontal.TProgressbar", background=pal["accent"],
                    troughcolor=pal["surface"])
        s.configure("TScrollbar", background=pal["surface"], troughcolor=pal["bg"])

        # The combobox dropdown is a separate Tk listbox styled via the option DB.
        self.root.option_add("*TCombobox*Listbox.background", pal["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", pal["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", pal["accent"])

        # Raw tk widgets aren't covered by ttk styles, so set them directly.
        self.log.configure(bg=pal["log_bg"], fg=pal["log_fg"], insertbackground=pal["log_fg"])
        self.queue_list.configure(bg=pal["surface"], fg=pal["fg"],
                                  selectbackground=pal["accent"], selectforeground=pal["fg"])
        self.info_label.configure(foreground=pal["info"])

    # -- Menu bar / views ---------------------------------------------------
    def _build_menu(self):
        """Native menu bar: View / Tools / Help."""
        menubar = tk.Menu(self.root)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_radiobutton(label="Simple", variable=self.view_var,
                                  value="simple", command=self._apply_view)
        view_menu.add_radiobutton(label="Advanced", variable=self.view_var,
                                  value="advanced", command=self._apply_view)
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Dark mode", variable=self.dark_var,
                                  command=self._apply_theme)
        menubar.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Update yt-dlp", command=self._update_ytdlp)
        tools_menu.add_command(label="Open download folder", command=self._open_download_folder)
        tools_menu.add_command(label="Open settings folder", command=self._open_config_folder)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="How to use", command=self._help_howto)
        help_menu.add_command(label="Extra-args glossary", command=self._show_glossary_dialog)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._help_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def _apply_view(self):
        """Show or hide the advanced rows for the current Simple/Advanced choice."""
        advanced = self.view_var.get() == "advanced"
        for w in self.advanced_widgets:
            w.grid() if advanced else w.grid_remove()

    def _open_download_folder(self):
        path = self.dir_var.get()
        if not os.path.isdir(path):
            messagebox.showwarning("Folder not found",
                                   "The download folder doesn't exist yet.")
            return
        self._open_path(path)

    def _open_config_folder(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self._open_path(CONFIG_DIR)

    @staticmethod
    def _open_path(path):
        # Only ever open an existing *directory*. On Windows os.startfile would
        # otherwise launch/execute whatever file the path points at.
        if not os.path.isdir(path):
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - opening a local folder in Explorer
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError:
            pass

    def _help_howto(self):
        self._show_help_dialog("How to use", HELP_HOWTO)

    def _help_about(self):
        self._show_help_dialog("About", HELP_ABOUT)

    def _show_help_dialog(self, title, body):
        """Small themed, read-only text popup used for the Help menu items."""
        pal = THEMES["dark" if self.dark_var.get() else "light"]
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=pal["bg"])
        win.transient(self.root)
        txt = tk.Text(win, wrap="word", width=66, height=16, relief="flat",
                      bg=pal["surface"], fg=pal["fg"], insertbackground=pal["fg"],
                      padx=10, pady=10, font=("Segoe UI", 9))
        txt.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _show_glossary_dialog(self):
        """Scrollable, clickable reference of useful yt-dlp flags."""
        pal = THEMES["dark" if self.dark_var.get() else "light"]
        win = tk.Toplevel(self.root)
        win.title("Extra-args glossary")
        win.configure(bg=pal["bg"])
        win.geometry("660x580")
        win.transient(self.root)

        ttk.Label(win, wraplength=620, foreground=pal["info"],
                  text="Click any flag to add it to the Extra args box (switches to "
                       "Advanced view). Flags are appended to yt-dlp's command, so avoid "
                       "options the form already sets (Format, Save to, Subtitles).").pack(
            fill="x", padx=10, pady=(10, 4))

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        txt = tk.Text(body, wrap="word", relief="flat", bg=pal["surface"], fg=pal["fg"],
                      padx=10, pady=8, font=("Segoe UI", 9), cursor="arrow")
        scr = ttk.Scrollbar(body, command=txt.yview)
        txt["yscrollcommand"] = scr.set
        scr.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        txt.tag_configure("cat", font=("Segoe UI", 10, "bold"),
                          foreground=pal["info"], spacing1=10, spacing3=4)
        txt.tag_configure("flag", foreground=pal["accent"],
                          font=("Consolas", 9, "bold"), underline=True)
        idx = 0
        for category, items in EXTRA_ARGS_GLOSSARY:
            txt.insert("end", category + "\n", "cat")
            for flag, desc in items:
                tag = f"flag{idx}"
                idx += 1
                txt.insert("end", "   ")
                txt.insert("end", flag, ("flag", tag))
                txt.insert("end", "\n      " + desc + "\n")
                txt.tag_bind(tag, "<Button-1>", lambda _e, fl=flag: self._insert_extra(fl))
                txt.tag_bind(tag, "<Enter>", lambda _e: txt.config(cursor="hand2"))
                txt.tag_bind(tag, "<Leave>", lambda _e: txt.config(cursor="arrow"))
        txt.configure(state="disabled")

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _insert_extra(self, flag):
        """Append a glossary flag to the Extra args field and reveal it."""
        cur = self.extra_var.get().strip()
        self.extra_var.set((cur + " " + flag).strip())
        if self.view_var.get() != "advanced":
            self.view_var.set("advanced")
            self._apply_view()
        self.status_var.set("Added to Extra args:  " + flag)

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
            "dark": self.dark_var.get(),
            "view": self.view_var.get(),
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
        cmd = [*ytdlp_base(), "--dump-json", "--no-playlist", "--no-warnings", url]
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
        if getattr(sys, "frozen", False):
            messagebox.showinfo(
                "Update yt-dlp",
                "This packaged build bundles yt-dlp. To update, download a newer "
                "release of the app from GitHub.")
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
        cmd = [*ytdlp_base(), "--newline", "--no-color"]

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
        # Only add the rclone upload step if the remote is well-formed. Because
        # yt-dlp runs --exec through the shell, a malformed value — e.g. one
        # injected via a tampered config — is dropped here rather than executed.
        remote = self.rclone_var.get().strip()
        if remote and RCLONE_REMOTE_RE.match(remote):
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
        remote = self.rclone_var.get().strip()
        if remote and not RCLONE_REMOTE_RE.match(remote):
            messagebox.showwarning(
                "Invalid upload remote",
                'The "Upload to" value must look like "name:path", using only '
                "letters, numbers, spaces and _ - . / @ ~. Configure remotes "
                "with: rclone config")
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
            ver = subprocess.run([*ytdlp_base(), "--version"],
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
                  self.remove_btn, self.clear_btn):
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
