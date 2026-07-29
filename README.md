# Photo Deduper

CLI that indexes photos in a local SQLite database, finds exact duplicates by size then content hash, and exports Markdown/JSON reports. Safe dry-run cleaning by default.

Works on local folders and cloud **sync** paths (Google Drive / OneDrive desktop). No cloud OAuth in v1.

## Install

```bash
cd photo-dedupe
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

Optional faster hashing:

```bash
pip install -e ".[blake3]"
```

## Usage

```bash
# Scan one or more roots (including synced Drive/OneDrive folders)
photo-dedupe scan ~/Pictures ~/OneDrive/Pictures

# Custom database path
photo-dedupe --db ~/photo-index.sqlite scan ~/Pictures

# Index health summary
photo-dedupe status

# Print duplicate groups
photo-dedupe duplicates
# Only groups that involve files under Downloads
photo-dedupe duplicates --under ~/Downloads

# Write report.md and duplicates.json to ./output
photo-dedupe report --format both
photo-dedupe report --under ~/Downloads
# Custom output directory
photo-dedupe report -o ~/photo-reports

# Dry-run summary (files to delete + total size)
photo-dedupe clean

# Dry-run with per-file keep/delete listing
photo-dedupe clean --detailed

# Only delete duplicates under one or more directories
photo-dedupe clean --delete-under ~/Downloads --delete-under ~/OneDrive/Camera\ Roll -d

# Actually delete extras (keeps oldest mtime; shortest path on ties)
photo-dedupe clean --apply
# Same, limited to a folder
photo-dedupe clean --delete-under ~/Downloads --apply

# Keeper policy: prefer library copies; only delete under Downloads
photo-dedupe clean --prefer-root ~/Pictures --delete-under ~/Downloads -d
photo-dedupe clean --keep newest --prefer-root ~/Pictures -d

# Preview / remove stale index rows (files gone since last scan)
photo-dedupe purge-missing
photo-dedupe purge-missing --detailed
photo-dedupe purge-missing --apply
```

## How it works

1. Walk roots for common image extensions (does not follow symlinks by default)
2. Store path, name, size, mtime in SQLite
3. Reuse hashes when size + mtime are unchanged
4. Hash only files that share a size with another present file
5. Exact duplicate group = same content hash
6. Reports also flag same name + size with different hashes

Default hash algorithm is **BLAKE3** when the `blake3` package is installed, otherwise **SHA-256**.

## Development

```bash
pip install -e ".[dev]"
pytest
```
