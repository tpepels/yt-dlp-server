#!/usr/bin/env python3
"""Repair legacy yt-dlp imports that were published before staging existed.

Dry-run by default. Use --apply only after reviewing the plan.

The tool uses persisted job URLs to recover collection-level metadata, prepares
all replacement files in a hidden incoming directory, retags the copies, and
only then removes the old scattered files. It never uses arbitrary YouTube
uploader/channel names as artists.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import staging


def load_jobs(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
    if not isinstance(jobs, list):
        raise RuntimeError(f"{path} does not contain a job list")
    return jobs


def under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def probe_source(url, is_playlist):
    command = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    if is_playlist:
        command.extend(["--flat-playlist", "--yes-playlist"])
    else:
        command.append("--no-playlist")
    command.append(url)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"yt-dlp exited {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid metadata JSON") from exc


def collection_metadata(info, is_playlist):
    if is_playlist:
        album = staging.clean_album_name(
            info.get("title")
            or info.get("playlist_title")
            or info.get("playlist")
        )
    else:
        album = staging.clean_album_name(info.get("album"))

    entries = [
        entry for entry in (info.get("entries") or [])
        if isinstance(entry, dict)
    ]
    per_track = {
        str(entry.get("id")): staging.trustworthy_track_artist(entry)
        for entry in entries
        if entry.get("id")
    }

    top_artist = staging.clean_artist_name(
        info.get("album_artist") or info.get("artist")
    )
    if not top_artist:
        for key in ("uploader", "channel"):
            raw = str(info.get(key) or "").strip()
            if raw.lower().endswith(" - topic"):
                top_artist = staging.clean_artist_name(raw)
                break

    artists = staging.unique_values(per_track.values())
    if top_artist:
        album_artist = top_artist
    elif len(artists) == 1:
        album_artist = artists[0]
    elif len(artists) > 1:
        album_artist = "Various Artists"
    else:
        album_artist = ""

    return album_artist, album, per_track


def collect_groups(jobs, import_root, only_url=None):
    groups = {}
    for job in jobs:
        if job.get("source", "yt-dlp") != "yt-dlp":
            continue
        url = str(job.get("url") or "").strip()
        if not url or (only_url and url != only_url):
            continue

        existing = []
        for raw_path in job.get("files") or []:
            path = Path(raw_path)
            if path.is_file() and under(path, import_root):
                existing.append(path)

        if not existing:
            continue

        group = groups.setdefault(
            url,
            {
                "paths": {},
                "attempts": 0,
                "playlist": bool(job.get("collection_mode", job.get("playlist"))),
            },
        )
        group["attempts"] += 1
        group["playlist"] = group["playlist"] or bool(
            job.get("collection_mode", job.get("playlist"))
        )
        for path in existing:
            source_id = staging.source_id_from_filename(path)
            key = source_id or str(path.resolve())
            # Prefer the most recently seen copy for a duplicate source id.
            group["paths"][key] = path
    return groups


def remove_empty_parents(path, stop):
    path = Path(path)
    stop = Path(stop).resolve()
    while True:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved == stop or stop not in resolved.parents:
            return
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def prepare_and_publish(import_root, url, paths, album_artist, album, per_track):
    token = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    incoming_parent = import_root / ".repair-incoming"
    incoming = incoming_parent / token
    if incoming.exists():
        shutil.rmtree(incoming)
    incoming.mkdir(parents=True, exist_ok=False)

    prepared = []
    try:
        for source in paths:
            destination = incoming / source.name
            shutil.copy2(source, destination)
            source_id = staging.source_id_from_filename(source)
            track_artist = per_track.get(source_id, "")
            if not track_artist and album_artist != "Various Artists":
                track_artist = album_artist
            staging.retag_media(
                destination,
                album=album,
                album_artist=album_artist,
                track_artist=track_artist,
            )
            prepared.append(destination)

        final_dir = (
            import_root
            / staging.safe_component(album_artist)
            / staging.safe_component(album)
        )
        final_dir.parent.mkdir(parents=True, exist_ok=True)

        if not final_dir.exists():
            os.replace(incoming, final_dir)
        else:
            for prepared_file in list(incoming.iterdir()):
                final_file = final_dir / prepared_file.name
                if final_file.exists():
                    prepared_file.unlink()
                else:
                    os.replace(prepared_file, final_file)
            incoming.rmdir()

        # Delete legacy originals only after every replacement file was
        # prepared successfully and published.
        for source in paths:
            final_file = final_dir / source.name
            try:
                same_file = source.resolve() == final_file.resolve()
            except OSError:
                same_file = False
            if not same_file and source.exists() and final_file.exists():
                source.unlink()
                remove_empty_parents(source.parent, import_root)

        try:
            if incoming_parent.exists() and not any(incoming_parent.iterdir()):
                incoming_parent.rmdir()
        except OSError:
            pass

        return final_dir
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Repair legacy yt-dlp imports using persisted playlist jobs"
    )
    parser.add_argument(
        "--music-root",
        default=os.getenv("MUSIC_ROOT", "/data/music"),
    )
    parser.add_argument(
        "--import-subdir",
        default=os.getenv("IMPORT_SUBDIR", "YT-DLP Imports"),
    )
    parser.add_argument(
        "--state-dir",
        default=os.getenv("STATE_DIR", "/data/state"),
    )
    parser.add_argument(
        "--url",
        help="Limit the repair to one exact persisted job URL",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually retag/move files. Without this flag, only print the plan.",
    )
    args = parser.parse_args()

    import_root = Path(args.music_root) / args.import_subdir.strip("/\\")
    jobs_file = Path(args.state_dir) / "jobs.json"
    groups = collect_groups(load_jobs(jobs_file), import_root, args.url)

    if not groups:
        print("No legacy playlist files found in persisted job history.")
        return 0

    unresolved = 0
    planned = 0
    changed = 0

    for url, group in sorted(groups.items()):
        paths = sorted(group["paths"].values())
        print(f"\n{url}")
        kind = "playlist" if group["playlist"] else "single item"
        print(
            f"  existing files: {len(paths)} from {group['attempts']} "
            f"recorded attempt(s) ({kind})"
        )

        try:
            info = probe_source(url, group["playlist"])
            album_artist, album, per_track = collection_metadata(
                info, group["playlist"]
            )
        except Exception as exc:
            print(f"  UNRESOLVED: metadata probe failed: {exc}")
            unresolved += 1
            continue

        if not album_artist or not album:
            print(
                "  UNRESOLVED: could not derive both album artist and album "
                f"(artist={album_artist!r}, album={album!r})"
            )
            unresolved += 1
            continue

        target = import_root / album_artist / album
        print(f"  target: {target}")
        source_dirs = sorted({str(path.parent) for path in paths})
        for directory in source_dirs[:8]:
            print(f"    from: {directory}")
        if len(source_dirs) > 8:
            print(f"    ... and {len(source_dirs) - 8} more directories")

        planned += len(paths)
        if not args.apply:
            continue

        try:
            final_dir = prepare_and_publish(
                import_root,
                url,
                paths,
                album_artist,
                album,
                per_track,
            )
        except Exception as exc:
            print(f"  FAILED: {exc}")
            unresolved += 1
            continue

        print(f"  repaired -> {final_dir}")
        changed += len(paths)

    print("\nSummary")
    print(f"  files planned: {planned}")
    print(f"  files repaired: {changed}")
    print(f"  unresolved collections: {unresolved}")
    if not args.apply:
        print("  DRY RUN ONLY - rerun with --apply to make these changes")

    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
