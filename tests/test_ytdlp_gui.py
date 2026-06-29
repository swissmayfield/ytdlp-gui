"""Unit tests for the pure-logic parts of ytdlp_gui.

These cover the functions that don't need a running Tk window: the yt-dlp
command prefix, the formatting helpers, the progress-line regexes, the rclone
remote validator, and config loading. Importing the module pulls in tkinter but
never creates a window, so the suite runs headless.
"""

import sys

import pytest

import ytdlp_gui as g
from ytdlp_gui import YtDlpGui


def test_ytdlp_base_from_source():
    base = g.ytdlp_base()
    assert base == [sys.executable, "-m", "yt_dlp"]


@pytest.mark.parametrize("seconds,expected", [
    (None, "?:??"),
    (0, "?:??"),
    (5, "0:05"),
    (75, "1:15"),
    (3725, "1:02:05"),
])
def test_fmt_duration(seconds, expected):
    assert YtDlpGui._fmt_duration(seconds) == expected


@pytest.mark.parametrize("num_bytes,expected", [
    (None, ""),
    (0, ""),
    (1500, "~1KiB"),
    (45_000_000, "~43MiB"),
])
def test_fmt_size(num_bytes, expected):
    assert YtDlpGui._fmt_size(num_bytes) == expected


@pytest.mark.parametrize("line,pct,speed,eta", [
    ("[download]  42.7% of 10.00MiB at 1.50MiB/s ETA 00:04", "42.7", "1.50MiB/s", "00:04"),
    ("[download] 100% of 5.00MiB in 00:03", "100", None, None),
    ("[download]   0.0% of ~ 8.00MiB at Unknown B/s ETA Unknown", "0.0", None, None),
])
def test_progress_regexes(line, pct, speed, eta):
    assert g.PERCENT_RE.search(line).group(1) == pct
    sm = g.SPEED_RE.search(line)
    assert (sm.group(1) if sm else None) == speed
    em = g.ETA_RE.search(line)
    assert (em.group(1) if em else None) == eta


@pytest.mark.parametrize("remote", [
    "gdrive:Movies", "s3-backup:bucket/path", "drive:", "r:videos/2024", "my_remote:a/b c",
])
def test_rclone_remote_accepts_valid(remote):
    assert g.RCLONE_REMOTE_RE.match(remote)


@pytest.mark.parametrize("remote", [
    'gdrive:x" & calc.exe & "',   # quote break-out + chained command
    "gdrive:$(rm -rf ~)",          # command substitution
    "remote:`whoami`",             # backtick substitution
    "x:|cmd",                       # pipe
    "a;b:c",                        # semicolon
    "name:a&b",                     # ampersand
    "no-colon",                     # not a remote at all
    "",                             # empty
])
def test_rclone_remote_rejects_injection(remote):
    assert not g.RCLONE_REMOTE_RE.match(remote)


def test_load_config_missing_valid_and_corrupt(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(g, "CONFIG_PATH", str(cfg))
    # missing file -> empty dict
    assert YtDlpGui._load_config(None) == {}
    # valid json -> parsed dict
    cfg.write_text('{"dark": true}', encoding="utf-8")
    assert YtDlpGui._load_config(None) == {"dark": True}
    # corrupt json -> empty dict (no crash)
    cfg.write_text("{not valid json", encoding="utf-8")
    assert YtDlpGui._load_config(None) == {}
