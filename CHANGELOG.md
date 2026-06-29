# Changelog

## v1.0 — 2026-06-28

First public release.

### Added
- Queue multiple URLs and download them sequentially, with a results summary.
- Fetch video info and choose from the real available formats.
- Quality presets, subtitles, SponsorBlock removal, and a download archive.
- Optional rclone upload to a remote (cloud / SFTP / S3).
- Extra-args passthrough with a clickable in-app glossary.
- Light/dark themes, Simple/Advanced views, and a native menu bar with Help.
- Standalone Windows `.exe` that bundles a SHA-256-verified `yt-dlp`.

### Security
- The rclone "Upload to" value is validated before use, blocking command
  injection through yt-dlp's shell-run `--exec`.
- "Open … folder" only opens existing directories.
- The bundled `yt-dlp.exe` is pinned and hash-verified at build time.

### Tooling
- pytest test suite, ruff linting, and a GitHub Actions CI pipeline that also
  runs bandit (static security) and pip-audit (dependency advisories).
