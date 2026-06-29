#!/usr/bin/env python3
"""Build the standalone ytdlp-gui executable for the current OS.

Downloads the pinned yt-dlp binary for this platform (yt-dlp.exe on Windows,
yt-dlp_macos on macOS, yt-dlp on Linux), verifies its SHA-256 against the value
published by the yt-dlp project, bundles it, and runs PyInstaller. Run on each OS
you want a binary for — PyInstaller can't cross-compile.

    py build.py        # Windows
    python3 build.py   # macOS / Linux

Pinning + verifying the hash means a corrupted download, a wrong file, or a
man-in-the-middle swap can't silently end up inside the released binary. To move
to a newer yt-dlp, bump YTDLP_VERSION and the hashes together (each is a line in
that release's SHA2-256SUMS).
"""

import hashlib
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "build_assets")

YTDLP_VERSION = "2026.06.09"

# Per platform: (release asset name, expected SHA-256, local name to bundle as).
# The local name must match what ytdlp_base() looks for at runtime:
# "yt-dlp.exe" on Windows, "yt-dlp" elsewhere.
_ASSETS = {
    "win32":  ("yt-dlp.exe",   "3a48cb955d55c8821b60ccbdbbc6f61bc958f2f3d3b7ad5eaf3d83a543293a27", "yt-dlp.exe"),
    "darwin": ("yt-dlp_macos", "b82c3626952e6c14eaf654cc565866775ffd0b9ffb7021628ac59b42c2f4f244", "yt-dlp"),
}
# Linux / other: the generic "yt-dlp" binary.
_LINUX = ("yt-dlp", "e5d57466682cfa9d61e9cf7c8a4f09b00f4a62af37d3bbdc4bcffdf63615feac", "yt-dlp")
ASSET_NAME, YTDLP_SHA256, LOCAL_NAME = _ASSETS.get(sys.platform, _LINUX)
YTDLP_URL = f"https://github.com/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/{ASSET_NAME}"
YTDLP_BIN = os.path.join(ASSETS, LOCAL_NAME)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_ytdlp():
    """Download the yt-dlp binary if missing/wrong, then verify its hash."""
    os.makedirs(ASSETS, exist_ok=True)
    if not os.path.exists(YTDLP_BIN) or sha256(YTDLP_BIN) != YTDLP_SHA256:
        print(f"Downloading {ASSET_NAME} {YTDLP_VERSION} ...")
        # URL is a constant HTTPS GitHub release link; the result is SHA-256
        # verified just below, so a tampered download is caught.
        urllib.request.urlretrieve(YTDLP_URL, YTDLP_BIN)  # nosec B310
    digest = sha256(YTDLP_BIN)
    if digest != YTDLP_SHA256:
        raise SystemExit(
            f"SHA-256 mismatch for {ASSET_NAME} — refusing to build with an "
            f"unverified binary.\n  expected {YTDLP_SHA256}\n  got      {digest}")
    print(f"Verified {ASSET_NAME} ({digest})")


def build():
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed",
        "--onefile", "--name", "ytdlp-gui",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        "--add-binary", f"{YTDLP_BIN}{sep}.",
        os.path.join(HERE, "ytdlp_gui.py"),
    ]
    subprocess.run(cmd, check=True)
    out_name = "ytdlp-gui.exe" if os.name == "nt" else "ytdlp-gui"
    print("\nBuilt:", os.path.join(HERE, "dist", out_name))


if __name__ == "__main__":
    ensure_ytdlp()
    build()
