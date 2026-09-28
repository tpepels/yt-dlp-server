#!/usr/bin/env python3
"""Build a deterministic shareable ZIP of the Chrome extension."""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "chrome-extension"
DIST_DIR = ROOT / "dist"
FILES = (
    "manifest.json",
    "service-worker.js",
    "options.html",
    "options.js",
    "README.md",
)
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def main():
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    version = manifest["version"]
    DIST_DIR.mkdir(exist_ok=True)
    target = DIST_DIR / f"yt-dlp-server-chrome-extension-v{version}.zip"

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for filename in FILES:
            source = EXTENSION_DIR / filename
            info = zipfile.ZipInfo(filename, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())

    print(target)


if __name__ == "__main__":
    main()
