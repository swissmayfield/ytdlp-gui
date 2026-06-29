#!/usr/bin/env python3
"""Build the standalone ytdlp-gui executable.

Downloads a pinned yt-dlp.exe, verifies its SHA-256 against the value published
by the yt-dlp project, bundles it, and runs PyInstaller.

    py build.py

Pinning + verifying the hash means a corrupted download, a wrong file, or a
man-in-the-middle swap can't silently end up inside the released binary. To move
to a newer yt-dlp, bump YTDLP_VERSION and YTDLP_SHA256 together (the expected
hash is the `yt-dlp.exe` line in that release's SHA2-256SUMS).
"""

import hashlib
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "build_assets")

YTDLP_VERSION = "2026.06.09"
YTDLP_SHA256 = "3a48cb955d55c8821b60ccbdbbc6f61bc958f2f3d3b7ad5eaf3d83a543293a27"
YTDLP_URL = f"https://github.com/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/yt-dlp.exe"
YTDLP_EXE = os.path.join(ASSETS, "yt-dlp.exe")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_ytdlp():
    """Download yt-dlp.exe if missing/wrong, then verify its hash (or abort)."""
    os.makedirs(ASSETS, exist_ok=True)
    if not os.path.exists(YTDLP_EXE) or sha256(YTDLP_EXE) != YTDLP_SHA256:
        print(f"Downloading yt-dlp {YTDLP_VERSION} ...")
        urllib.request.urlretrieve(YTDLP_URL, YTDLP_EXE)
    digest = sha256(YTDLP_EXE)
    if digest != YTDLP_SHA256:
        raise SystemExit(
            "SHA-256 mismatch for yt-dlp.exe — refusing to build with an "
            f"unverified binary.\n  expected {YTDLP_SHA256}\n  got      {digest}")
    print(f"Verified yt-dlp.exe ({digest})")


def build():
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed",
        "--onefile", "--name", "ytdlp-gui",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        "--add-binary", f"{YTDLP_EXE}{sep}.",
        os.path.join(HERE, "ytdlp_gui.py"),
    ]
    subprocess.run(cmd, check=True)
    print("\nBuilt:", os.path.join(HERE, "dist", "ytdlp-gui.exe"))


if __name__ == "__main__":
    ensure_ytdlp()
    build()
