#!/usr/bin/env sh
# Launch the GUI on Linux / macOS:  ./run.sh
# (Windows users: use run.bat instead.)
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    exec python3 ytdlp_gui.py
fi
exec python ytdlp_gui.py
