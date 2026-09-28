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
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def is_single_video_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "music.youtube.com", "youtu.be",
    }:
        return False
    if host == "youtu.be":
        return True
    return parsed.path == "/watch" and not parse_qs(parsed.query).get("list")


def youtube_probe_args(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return []
    if host not in {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "music.youtube.com", "youtu.be",
    }:
        return []

    args = []
    player_client = os.getenv("YOUTUBE_PLAYER_CLIENT", "mweb").strip()
    if player_client:
        args.extend(["--extractor-args", f"youtube:player_client={player_client}"])

    server_home = Path(
        os.getenv(
            "BGUTIL_SERVER_HOME",
            "/opt/bgutil-ytdlp-pot-provider/server",
        )
    )
    if server_home.is_dir():
        args.extend([
            "--extractor-args",
            f"youtubepot-bgutilscript:server_home={server_home}",
        ])
    return args


def probe_cache_path(cache_dir, url, is_playlist):
    if not cache_dir:
        return None
    token = hashlib.sha256(
        f"full-v2|{int(bool(is_playlist))}|{url}".encode("utf-8")
    ).hexdigest()[:24]
    return Path(cache_dir) / f"{token}.json"


def probe_source(url, is_playlist, cache_dir=None, refresh=False):
    cache_path = probe_cache_path(cache_dir, url, is_playlist)
    if cache_path and cache_path.is_file() and not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict):
            return cached

    command = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--ignore-errors",
    ]
    command.extend(youtube_probe_args(url))
    if is_playlist:
        # Deliberately do NOT use --flat-playlist here. Legacy repair needs
        # each track's music metadata, which flat playlist extraction omits.
        command.append("--yes-playlist")
    else:
        command.append("--no-playlist")
    command.append(url)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    output = (completed.stdout or "").strip()
    if output:
        try:
            info = json.loads(output)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or "").strip()
            raise RuntimeError(
                detail or "yt-dlp returned invalid metadata JSON"
            ) from exc
        if isinstance(info, dict):
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(info, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(tmp, cache_path)
            return info

    detail = (completed.stderr or completed.stdout or "").strip()
    raise RuntimeError(detail or f"yt-dlp exited {completed.returncode}")


def topic_artist(info):
    for key in ("uploader", "channel"):
        raw = str(info.get(key) or "").strip()
        if raw.lower().endswith(" - topic"):
            return staging.clean_artist_name(raw)
    return ""


def unique_consensus(values, total):
    cleaned = [value for value in values if value]
    unique = staging.unique_values(cleaned)
    if len(unique) == 1 and len(cleaned) > total / 2:
        return unique[0], len(cleaned)
    return "", 0


def majority_value(values, total):
    cleaned = [value for value in values if value]
    if not cleaned:
        return "", 0
    value, count = Counter(cleaned).most_common(1)[0]
    if count > total / 2:
        return value, count
    return "", 0


def collection_metadata(info, is_playlist):
    entries = [
        entry for entry in (info.get("entries") or [])
        if isinstance(entry, dict) and entry.get("id")
    ]

    if not entries and not is_playlist and info.get("id"):
        entries = [info]

    per_track = {
        str(entry.get("id")): staging.trustworthy_track_artist(entry)
        for entry in entries
        if entry.get("id")
    }

    explicit_albums = [
        staging.clean_album_name(entry.get("album"))
        for entry in entries
    ]
    album, album_count = unique_consensus(explicit_albums, len(entries))
    album_evidence = (
        f"track album metadata ({album_count}/{len(entries)})"
        if album else ""
    )
    if not album:
        album, album_count = majority_value(explicit_albums, len(entries))
        if album:
            album_evidence = (
                f"majority track album metadata ({album_count}/{len(entries)})"
            )

    if not album and is_playlist:
        album = staging.clean_album_name(
            info.get("title")
            or info.get("playlist_title")
            or info.get("playlist")
        )
        if not album:
            album = staging.most_common(
                staging.clean_album_name(
                    entry.get("playlist_title")
                    or entry.get("playlist")
                )
                for entry in entries
            )
        if album:
            album_evidence = "playlist title"
    elif not album:
        album = staging.clean_album_name(info.get("album"))
        if album:
            album_evidence = "track album metadata"

    explicit_album_artists = [
        staging.clean_artist_name(entry.get("album_artist"))
        for entry in entries
    ]
    album_artist, artist_count = unique_consensus(
        explicit_album_artists, len(entries)
    )
    artist_evidence = (
        f"album_artist metadata ({artist_count}/{len(entries)})"
        if album_artist else ""
    )

    if not album_artist:
        topic_artists = [topic_artist(entry) for entry in entries]
        album_artist, artist_count = unique_consensus(
            topic_artists, len(entries)
        )
        if album_artist:
            artist_evidence = (
                f"stable Artist - Topic channel ({artist_count}/{len(entries)})"
            )

    if not album_artist:
        track_artists = [
            staging.clean_artist_name(entry.get("artist"))
            for entry in entries
        ]
        album_artist, artist_count = unique_consensus(
            track_artists, len(entries)
        )
        if album_artist:
            artist_evidence = (
                f"consistent track artist metadata ({artist_count}/{len(entries)})"
            )

    evidence = {
        "tracks": len(entries),
        "artist": artist_evidence,
        "album": album_evidence,
    }
    return album_artist, album, per_track, evidence


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
    parser.add_argument(
        "--include-singles",
        action="store_true",
        help=(
            "Also repair legacy single-video jobs. By default the repair is "
            "album-only and leaves standalone watch URLs untouched."
        ),
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Ignore cached full-metadata probes and query sources again.",
    )
    args = parser.parse_args()

    import_root = Path(args.music_root) / args.import_subdir.strip("/\\")
    state_dir = Path(args.state_dir)
    jobs_file = state_dir / "jobs.json"
    metadata_cache = state_dir / "repair-metadata-cache"
    groups = collect_groups(load_jobs(jobs_file), import_root, args.url)

    if not groups:
        print("No legacy playlist files found in persisted job history.")
        return 0

    unresolved = 0
    skipped_singles = 0
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

        if is_single_video_url(url) and not args.include_singles:
            print(
                "  SKIPPED: standalone video URL - legacy repair is album-only "
                "(use --include-singles to include it)"
            )
            skipped_singles += 1
            continue

        actual_playlist = not is_single_video_url(url) and group["playlist"]

        try:
            info = probe_source(
                url,
                actual_playlist,
                cache_dir=metadata_cache,
                refresh=args.refresh_metadata,
            )
            album_artist, album, per_track, evidence = collection_metadata(
                info, actual_playlist
            )
        except Exception as exc:
            print(f"  UNRESOLVED: metadata probe failed: {exc}")
            unresolved += 1
            continue

        if evidence["tracks"]:
            print(f"  full metadata: {evidence['tracks']} track(s) inspected")

        if not album_artist or not album:
            print(
                "  UNRESOLVED: could not derive both album artist and album "
                f"(artist={album_artist!r}, album={album!r})"
            )
            if evidence["artist"]:
                print(f"    artist evidence: {evidence['artist']}")
            if evidence["album"]:
                print(f"    album evidence: {evidence['album']}")
            unresolved += 1
            continue

        target = import_root / album_artist / album
        print(f"  target: {target}")
        print(f"    artist evidence: {evidence['artist']}")
        print(f"    album evidence: {evidence['album']}")
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
    print(f"  unresolved albums: {unresolved}")
    print(f"  standalone video jobs skipped: {skipped_singles}")
    if not args.apply:
        print("  DRY RUN ONLY - rerun with --apply to make these changes")

    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
