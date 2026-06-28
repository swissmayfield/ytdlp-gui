# yt-dlp GUI

A small desktop front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp), built with
Python's built-in `tkinter`. Paste a URL, pick a quality and a folder, and it runs
yt-dlp in the background while streaming live progress into the window.

![one-file Tkinter app]

## Features
- **Fetch** — pull a video's title, duration, channel, and its *real* available
  formats before downloading, then pick a specific format instead of guessing
- **URL queue** — line up multiple URLs and download them one after another
- Quality presets (best, 1080p, 720p, 480p, audio-only MP3)
- **Subtitles** — download/embed subs in a chosen language (incl. auto-generated)
- **SponsorBlock** — auto-remove sponsor segments (`--sponsorblock-remove`)
- **Download archive** — skip items you've already grabbed (great for re-syncing)
- **Update yt-dlp** button — keeps the downloader current when sites change
- **Extra args** box — pass any yt-dlp flag the GUI doesn't expose (proxy, rate
  limit, clip sections, …); quoted values are parsed correctly
- **Results summary** — after a batch, a `✓ succeeded / ✗ failed` recap that
  lists the URLs that failed
- Choose the output folder; optional whole-playlist + metadata/thumbnail embed
- Live log output, a real progress bar, and a **speed / ETA** readout
- **Remembers** your settings between runs (saved to a JSON config)
- Cancel button to stop the current download and the rest of the queue
- Non-blocking UI (long operations run on a worker thread)

### Extra args examples
Type these into the **Extra args** box:
- `--limit-rate 2M` — cap download speed
- `--download-sections "*10:00-10:30"` — grab just a clip
- `--cookies-from-browser chrome` — use your logged-in browser session
- `-N 4` — download fragments in parallel (faster)

Settings and the download archive live in `%APPDATA%\ytdlp-gui\` (Windows) or
`~/.config/ytdlp-gui/` (macOS/Linux).

> Fetched info is shown as text. Rendering the actual thumbnail image would need
> the optional [Pillow](https://pypi.org/project/pillow/) library — left out to
> keep this stdlib-only.

## Requirements
- Python 3.9+ (uses only the standard library — `tkinter` is included)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp): `py -m pip install -U yt-dlp`
- [`ffmpeg`](https://ffmpeg.org/) on PATH (needed for merging video+audio and MP3 export)

## Run
```
py ytdlp_gui.py
```
or double-click `run.bat` on Windows.

## How it works
- The GUI builds a yt-dlp command from your selections and runs it as a subprocess
  (`python -m yt_dlp ...`) — the same thing you'd type in a terminal.
- yt-dlp's output is read line-by-line on a background **thread**, pushed through a
  thread-safe **queue**, and drained onto the GUI by a `root.after()` timer. tkinter
  isn't thread-safe, so the worker never touches widgets directly.
- The progress bar is driven by parsing the `[download]  NN.N%` lines yt-dlp prints.

## Legal note
yt-dlp does **not** bypass DRM. Use this only for content you're allowed to download —
your own uploads, Creative Commons / public-domain video, or sites whose terms permit it.

## Ideas for next versions
- A "paste from clipboard" button + auto-detect a URL on the clipboard at startup
- Per-item status in the queue (queued / downloading / done / failed)
- Light/dark theme toggle
- Package as a standalone `.exe` with PyInstaller
