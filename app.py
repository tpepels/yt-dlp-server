import json
import os
import queue
import re
import subprocess
import threading
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

import staging as library_staging

app = Flask(__name__)

PORT = int(os.getenv("PORT", "4545"))
MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/data/music"))
IMPORT_SUBDIR = os.getenv("IMPORT_SUBDIR", "YT-DLP Imports").strip("/\\")
STATE_DIR = Path(os.getenv("STATE_DIR", "/data/state"))
TEMP_DIR = Path(os.getenv("TEMP_DIR", "/data/tmp"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "50"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "50"))
PROBE_TIMEOUT = int(os.getenv("PROBE_TIMEOUT", "30"))
FALLBACK_TRACK_TIMEOUT = int(os.getenv("FALLBACK_TRACK_TIMEOUT", "600"))
PLEX_RESCAN_NUDGE_SECONDS = float(os.getenv("PLEX_RESCAN_NUDGE_SECONDS", "90"))
BGUTIL_SERVER_HOME = Path(os.getenv("BGUTIL_SERVER_HOME", "/opt/bgutil-ytdlp-pot-provider/server"))
YOUTUBE_PLAYER_CLIENT = os.getenv("YOUTUBE_PLAYER_CLIENT", "mweb").strip() or "mweb"

if not IMPORT_SUBDIR or Path(IMPORT_SUBDIR).is_absolute() or ".." in Path(IMPORT_SUBDIR).parts:
    raise RuntimeError("IMPORT_SUBDIR must be a safe relative path")

if TEMP_DIR.resolve() == MUSIC_ROOT.resolve() or MUSIC_ROOT.resolve() in TEMP_DIR.resolve().parents:
    raise RuntimeError("TEMP_DIR must be outside MUSIC_ROOT so staging cannot be indexed by Plex")

IMPORT_ROOT = MUSIC_ROOT / IMPORT_SUBDIR
ARCHIVE_FILE = STATE_DIR / "archive.txt"
SPOTDL_ARCHIVE_FILE = STATE_DIR / "spotdl-archive.txt"
JOBS_STATE_FILE = STATE_DIR / "jobs.json"
IGNORED_TRACKS_FILE = STATE_DIR / "ignored-tracks.json"
CHROME_EXTENSION_ZIP = Path(app.root_path) / "dist" / "yt-dlp-server-chrome-extension-v1.0.0.zip"

for directory in (IMPORT_ROOT, STATE_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

OUTPUT_TEMPLATE = library_staging.staged_ytdlp_output_template()

jobs = {}
jobs_lock = threading.Lock()
probe_cache = {}
probe_cache_lock = threading.Lock()
download_queue = queue.Queue(maxsize=MAX_QUEUE)
library_nudge_lock = threading.Lock()
library_nudge_timer = None
_progress_re = re.compile(r"^\[download\]\s+(.+?)(?:\s+of\s+|\s+at\s+|\s+ETA\s+|$)")
_playlist_item_re = re.compile(r"^\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)")
_spotdl_progress_re = re.compile(r"(\d+)/(\d+) complete")
_spotdl_downloaded_re = re.compile(r'\bDownloaded "')
_spotdl_existing_re = re.compile(r"\bSkipping .+\(file already exists\)")
_spotify_track_url_re = re.compile(r"https://open\.spotify\.com/track/[A-Za-z0-9]+(?:\?[^\s]*)?")
_youtube_error_re = re.compile(r"^ERROR: \[youtube\] ([A-Za-z0-9_-]{6,}): (.+)$")
_ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _emit_library_change_nudge():
    """Generate a fresh filesystem event after publishing settles.

    Plex can miss directories created while a long library scan is already
    running. Creating and immediately removing a tiny hidden marker produces a
    new watcher event without leaving junk in the music library.
    """
    global library_nudge_timer

    marker = IMPORT_ROOT / f".yt-dlp-refresh-{uuid.uuid4().hex[:8]}"
    try:
        marker.touch(exist_ok=False)
    except OSError:
        marker = None
    finally:
        if marker is not None:
            try:
                marker.unlink()
            except OSError:
                pass

        with library_nudge_lock:
            library_nudge_timer = None


def schedule_library_change_nudge(delay=None):
    """Debounce a post-publish watcher nudge.

    The final directory creation already gives Plex an immediate notification.
    This delayed event is specifically a fallback for the case where that
    notification arrives while Plex is still inside an older scan.
    """
    global library_nudge_timer

    seconds = PLEX_RESCAN_NUDGE_SECONDS if delay is None else float(delay)
    if seconds < 0:
        return

    with library_nudge_lock:
        if library_nudge_timer is not None:
            library_nudge_timer.cancel()

        timer = threading.Timer(seconds, _emit_library_change_nudge)
        timer.daemon = True
        library_nudge_timer = timer
        timer.start()


def tool_version(command):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            return "unavailable"
        output = (completed.stdout or completed.stderr).strip().splitlines()
        return output[0] if output else "unavailable"
    except Exception:
        return "unavailable"


VERSIONS = {
    "yt_dlp": tool_version(["yt-dlp", "--version"]),
    "ffmpeg": tool_version(["ffmpeg", "-version"]),
    "deno": tool_version(["deno", "--version"]),
    "bgutil": tool_version([
        "python",
        "-c",
        "import importlib.metadata; print(importlib.metadata.version('bgutil-ytdlp-pot-provider'))",
    ]),
    "spotdl": tool_version(["spotdl", "--version"]),
}


def persist_jobs_locked():
    """Persist the complete UI/job state atomically. Caller must hold jobs_lock."""
    payload = {
        "version": 1,
        "saved_at": time.time(),
        "jobs": list(jobs.values()),
    }
    tmp_path = JOBS_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, JOBS_STATE_FILE)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def persist_jobs():
    with jobs_lock:
        persist_jobs_locked()


def load_ignored_tracks():
    if not IGNORED_TRACKS_FILE.is_file():
        return {}
    try:
        data = json.loads(IGNORED_TRACKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def persist_ignored_tracks(ignored):
    tmp_path = IGNORED_TRACKS_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(ignored, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, IGNORED_TRACKS_FILE)


def extract_youtube_missing_tracks(lines, ignored=None):
    ignored = ignored if ignored is not None else load_ignored_tracks()
    missing = []
    seen = set()
    for line in lines or []:
        match = _youtube_error_re.match(line.strip())
        if not match:
            continue
        video_id, reason = match.groups()
        if video_id in seen:
            continue
        key = f"youtube:{video_id}"
        missing.append({
            "source": "youtube",
            "source_id": video_id,
            "label": f"YouTube video {video_id}",
            "reason": reason,
            "ignored": key in ignored,
        })
        seen.add(video_id)
    return missing


def job_has_only_ignored_youtube_errors(job, ignored=None):
    ignored = ignored if ignored is not None else load_ignored_tracks()
    errors = [line.strip() for line in job.get("log", []) if line.strip().startswith("ERROR:")]
    if not errors:
        return False

    for line in errors:
        match = _youtube_error_re.match(line)
        if not match or f"youtube:{match.group(1)}" not in ignored:
            return False
    return True


def close_job_if_only_ignored_locked(job, ignored=None):
    ignored = ignored if ignored is not None else load_ignored_tracks()
    youtube_missing = extract_youtube_missing_tracks(job.get("log", []), ignored)
    if youtube_missing:
        spotify_missing = [
            item for item in job.get("missing_tracks", [])
            if item.get("source") != "youtube"
        ]
        job["missing_tracks"] = spotify_missing + youtube_missing

    if not job_has_only_ignored_youtube_errors(job, ignored):
        return False

    ignored_count = len(youtube_missing)
    job["status"] = "succeeded"
    job["failed_items"] = 0
    job["returncode"] = 0
    job["progress"] = ""
    if job.get("total_items"):
        job["message"] = (
            f"Completed - {job['completed_items']}/{job['total_items']} tracks saved; "
            f"{ignored_count} ignored"
        )
    elif job.get("completed_items"):
        job["message"] = (
            f"Completed - {job['completed_items']} tracks saved; {ignored_count} ignored"
        )
    else:
        job["message"] = f"Completed with {ignored_count} ignored unavailable track(s)"
    return True


def normalize_persisted_job(raw):
    if not isinstance(raw, dict) or not raw.get("url"):
        return None

    now = time.time()
    job_id = str(raw.get("id") or uuid.uuid4().hex[:10])
    status = raw.get("status") if raw.get("status") in {"queued", "running", "finalizing", "needs_metadata", "succeeded", "partial", "failed"} else "failed"
    source = raw.get("source") or ("spotify" if is_spotify_url(raw["url"]) else "yt-dlp")
    retry_base_completed = raw.get("retry_base_completed")
    if retry_base_completed is None:
        retry_base_completed = (
            int(raw.get("completed_items") or 0)
            if source == "spotify" and int(raw.get("attempt") or 1) > 1
            else 0
        )

    job = {
        "id": job_id,
        "url": str(raw["url"]),
        "playlist": bool(raw.get("playlist", False)),
        "collection_mode": bool(raw.get("collection_mode", raw.get("playlist", False))),
        "album_mode": bool(raw.get("album_mode", False)),
        "compilation": bool(raw.get("compilation", False)),
        "force": bool(raw.get("force", False)),
        "status": status,
        "message": str(raw.get("message") or "Restored job"),
        "progress": str(raw.get("progress") or ""),
        "current_item": int(raw.get("current_item") or 0),
        "total_items": int(raw.get("total_items") or 0),
        "completed_items": int(raw.get("completed_items") or 0),
        "files": list(raw.get("files") or []),
        "log": list(raw.get("log") or [])[-40:],
        "created_at": float(raw.get("created_at") or now),
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "returncode": raw.get("returncode"),
        "http_403": bool(raw.get("http_403", False)),
        "retry_of": raw.get("retry_of"),
        "attempt": int(raw.get("attempt") or 1),
        "source": source,
        "retry_base_completed": int(retry_base_completed),
        "failed_items": int(raw.get("failed_items") or 0),
        "unidentified_failed_items": int(raw.get("unidentified_failed_items") or 0),
        "missing_tracks": list(raw.get("missing_tracks") or []),
        "resolution_of": raw.get("resolution_of"),
        "spotify_track_url": raw.get("spotify_track_url"),
        "manual_source_url": raw.get("manual_source_url"),
        "download_query": raw.get("download_query"),
        "staging_owner_id": raw.get("staging_owner_id"),
        "source_title": raw.get("source_title"),
        "source_album_artist": raw.get("source_album_artist"),
        "source_thumbnail": raw.get("source_thumbnail"),
        "metadata_artist": raw.get("metadata_artist"),
        "metadata_album": raw.get("metadata_album"),
        "staged_files": int(raw.get("staged_files") or 0),
        "final_dir": raw.get("final_dir"),
        "finalize_target_id": raw.get("finalize_target_id"),
    }

    if source == "yt-dlp":
        youtube_missing = extract_youtube_missing_tracks(job["log"])
        if youtube_missing:
            job["missing_tracks"] = youtube_missing
        close_job_if_only_ignored_locked(job)
    elif source == "spotify":
        logged_missing = extract_spotdl_missing_tracks(job["log"])
        if logged_missing:
            existing = {
                item.get("spotify_url"): item
                for item in job.get("missing_tracks", [])
                if item.get("spotify_url")
            }
            for item in logged_missing:
                previous = existing.get(item["spotify_url"])
                if previous:
                    item["resolved"] = bool(previous.get("resolved"))
                    item["source_url"] = previous.get("source_url")
                existing[item["spotify_url"]] = item
            job["missing_tracks"] = list(existing.values())

    # /api/jobs from older versions intentionally exposed less internal state.
    # Reconstruct a queued manual-resolution query when possible.
    if (
        not job["download_query"]
        and job["resolution_of"]
        and job["spotify_track_url"]
        and job["manual_source_url"]
    ):
        job["download_query"] = f'{job["manual_source_url"]}|{job["spotify_track_url"]}'

    # A process that was running when the container stopped cannot still be
    # running after restart. Preserve it as a retryable interruption instead
    # of silently starting it again and risking duplicate/forced downloads.
    if job["status"] in {"running", "finalizing"}:
        job["status"] = "failed"
        job["finished_at"] = now
        job["returncode"] = None
        job["message"] = "Interrupted by server restart - use Retry / continue"
        job["log"].append("Server restarted while this job was running.")
        job["log"] = job["log"][-40:]

    return job


def load_persisted_jobs():
    if not JOBS_STATE_FILE.is_file():
        return []

    try:
        raw = json.loads(JOBS_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    items = raw.get("jobs", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    restored = []
    for item in items:
        job = normalize_persisted_job(item)
        if job is not None:
            restored.append(job)

    restored.sort(key=lambda item: item["created_at"])
    # Do not drop queued work on restore. Runtime history pruning only removes
    # terminal jobs, so the persisted loader must preserve the same guarantee.
    if len(restored) > MAX_HISTORY:
        queued = [item for item in restored if item["status"] == "queued"]
        terminal = [item for item in restored if item["status"] != "queued"]
        keep_terminal = max(0, MAX_HISTORY - len(queued))
        restored = terminal[-keep_terminal:] + queued if keep_terminal else queued
        restored.sort(key=lambda item: item["created_at"])

    with jobs_lock:
        jobs.clear()
        for job in restored:
            jobs[job["id"]] = job
        persist_jobs_locked()

    return [job["id"] for job in restored if job["status"] == "queued"]


def validate_url(value):
    value = (value or "").strip()
    if not value:
        return None, "Paste a URL."
    try:
        parsed = urlparse(value)
    except ValueError:
        return None, "That is not a valid URL."
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None, "Only http:// and https:// URLs are accepted."
    if parsed.username or parsed.password:
        return None, "URLs containing credentials are not accepted."
    return value, None


def is_spotify_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    hostname = (parsed.hostname or "").lower()
    return hostname in {
        "open.spotify.com",
        "spotify.com",
        "www.spotify.com",
        "spotify.link",
    }


def spotify_link_type(url):
    if not is_spotify_url(url):
        return None
    parts = [part for part in urlparse(url).path.split("/") if part]
    for item_type in ("track", "album", "playlist", "artist", "show", "episode"):
        if item_type in parts:
            return item_type
    return "link"


def is_youtube_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    hostname = (parsed.hostname or "").lower()
    return hostname in {
        "youtube.com",
        "www.youtube.com",
        "music.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }


def is_youtube_album_playlist(url):
    if not is_youtube_url(url):
        return False

    parsed = urlparse(url)
    playlist_id = parse_qs(parsed.query).get("list", [""])[0]
    return playlist_id.startswith("OLAK5uy_")


def youtube_extractor_args(url):
    if not is_youtube_url(url):
        return []

    args = [
        "--extractor-args",
        f"youtube:player_client={YOUTUBE_PLAYER_CLIENT}",
    ]

    if BGUTIL_SERVER_HOME.is_dir():
        args.extend([
            "--extractor-args",
            f"youtubepot-bgutilscript:server_home={BGUTIL_SERVER_HOME}",
        ])

    return args


def spotdl_ytdlp_args():
    args = [
        "--extractor-args",
        f"youtube:player_client={YOUTUBE_PLAYER_CLIENT}",
    ]
    if BGUTIL_SERVER_HOME.is_dir():
        args.extend([
            "--extractor-args",
            f"youtubepot-bgutilscript:server_home={BGUTIL_SERVER_HOME}",
        ])
    return " ".join(args)


def spotdl_error_file(job_id):
    return TEMP_DIR / f"spotdl-errors-{job_id}.log"


def extract_spotdl_missing_tracks(errors):
    missing = []
    seen = set()
    for error in errors:
        matches = list(_spotify_track_url_re.finditer(error))
        for match in matches:
            spotify_url = match.group(0)
            if spotify_url in seen:
                continue

            if "No results found for song:" in error:
                label = error.split("No results found for song:", 1)[1].strip()
            else:
                label = error.replace(spotify_url, "", 1).strip(" -:")[:240]

            missing.append({
                "spotify_url": spotify_url,
                "label": label or spotify_url,
                "reason": error.strip(),
                "resolved": False,
                "source_url": None,
            })
            seen.add(spotify_url)
    return missing


def is_manual_spotify_source_url(value):
    url, error = validate_url(value)
    if error:
        return False
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname == "youtu.be":
        return True
    if hostname in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"}:
        return "watch" in parsed.path and bool(parse_qs(parsed.query).get("v"))
    if hostname == "soundcloud.com" or hostname.endswith(".soundcloud.com"):
        return True
    if hostname == "bandcamp.com" or hostname.endswith(".bandcamp.com"):
        return True
    return False


def apply_manual_resolution_success(job):
    parent_id = job.get("resolution_of")
    spotify_url = job.get("spotify_track_url")
    source_url = job.get("manual_source_url")
    if not parent_id or not spotify_url:
        return

    parent = jobs.get(parent_id)
    if not parent:
        return

    newly_resolved = False
    for missing in parent.get("missing_tracks", []):
        if missing.get("spotify_url") == spotify_url:
            newly_resolved = not missing.get("resolved")
            missing["resolved"] = True
            missing["source_url"] = source_url

    unresolved = [
        item for item in parent.get("missing_tracks", [])
        if not item.get("resolved")
    ]
    if newly_resolved:
        parent["completed_items"] += 1
        if parent.get("total_items"):
            parent["completed_items"] = min(
                parent["completed_items"],
                parent["total_items"],
            )
        parent["failed_items"] = len(unresolved)
        parent["unidentified_failed_items"] = 0
        parent["log"].append(f"Resolved {spotify_url} from {source_url}")
        parent["log"] = parent["log"][-40:]

    if not unresolved and parent.get("missing_tracks"):
        parent["status"] = "succeeded"
        parent["failed_items"] = 0
        if parent.get("total_items"):
            parent["completed_items"] = parent["total_items"]
            parent["current_item"] = parent["total_items"]
            parent["progress"] = ""
            parent["message"] = f"Completed - {parent['total_items']}/{parent['total_items']} tracks saved"
        else:
            parent["message"] = "Completed - missing track resolved manually"
    elif newly_resolved:
        parent["status"] = "partial"
        if parent.get("total_items"):
            parent["message"] = (
                f"Partial - {parent['completed_items']}/{parent['total_items']} "
                f"tracks saved; {len(unresolved)} failed"
            )
        else:
            parent["message"] = (
                f"Partial - {parent['completed_items']} tracks saved; "
                f"{len(unresolved)} failed"
            )


def album_playlist_metadata_args(compilation=False):
    # Collection-level artist/album decisions are made after the entire
    # download has finished. Never promote playlist uploader/channel names to
    # album artists while downloading.
    return [
        "--parse-metadata",
        "%(playlist_title,album,playlist|)s:%(album)s",
        "--parse-metadata",
        "%(playlist_index)s:%(track_number)s",
    ]


def cookie_args():
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        return ["--cookies", COOKIES_FILE]
    return []


def build_probe_command(url, full_playlist=False):
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--yes-playlist",
    ]
    if not full_playlist:
        cmd.extend(["--playlist-end", "1"])
    cmd.extend(youtube_extractor_args(url))
    cmd.extend(cookie_args())
    cmd.append(url)
    return cmd


def normalize_topic_artist(value):
    value = (value or "").strip()
    value = re.sub(r"\s+- Topic$", "", value, flags=re.IGNORECASE).strip()
    if value.lower() in {"youtube", "youtube music"}:
        return ""
    return value


def infer_album_artist(info):
    artists = set()
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue

        value = entry.get("album_artist") or entry.get("artist")
        if not value:
            for key in ("uploader", "channel"):
                raw = str(entry.get(key) or "").strip()
                if re.search(r"\s+-\s+Topic$", raw, flags=re.IGNORECASE):
                    value = raw
                    break

        value = normalize_topic_artist(value)
        if value:
            artists.add(value)

    if len(artists) > 1:
        return "Various Artists"
    if len(artists) == 1:
        return next(iter(artists))

    top = info.get("album_artist") or info.get("artist")
    if not top:
        for key in ("uploader", "channel"):
            raw = str(info.get(key) or "").strip()
            if re.search(r"\s+-\s+Topic$", raw, flags=re.IGNORECASE):
                top = raw
                break
    return normalize_topic_artist(top) or None


def best_thumbnail_url(info):
    value = str(info.get("thumbnail") or "").strip()
    if value.startswith(("http://", "https://")):
        return value

    thumbnails = info.get("thumbnails") or []
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("url") or "").strip()
            if candidate.startswith(("http://", "https://")):
                return candidate

    # Playlist-level YouTube metadata often has no thumbnail even though every
    # track does. Use the first available track thumbnail for the queue card.
    entries = info.get("entries") or []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = str(entry.get("thumbnail") or "").strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
            child_thumbnails = entry.get("thumbnails") or []
            if isinstance(child_thumbnails, list):
                for item in reversed(child_thumbnails):
                    if not isinstance(item, dict):
                        continue
                    candidate = str(item.get("url") or "").strip()
                    if candidate.startswith(("http://", "https://")):
                        return candidate

            video_id = str(entry.get("id") or "").strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


def classify_probe_info(info, url=None):
    entries = info.get("entries")
    kind = (
        "playlist"
        if info.get("_type") in {"playlist", "multi_video"} or isinstance(entries, list)
        else "single"
    )

    count = info.get("playlist_count")
    if not isinstance(count, int) or count < 1:
        count = len(entries) if isinstance(entries, list) and entries else None

    extractor = str(info.get("extractor_key") or info.get("extractor") or "")
    path = urlparse(url).path.lower() if url else ""
    album_mode = bool(
        (url and is_youtube_album_playlist(url))
        or info.get("album")
        or "/album/" in path
        or "album" in extractor.lower()
    )
    return {
        "kind": kind,
        "title": info.get("title") or info.get("fulltitle") or "Untitled",
        "count": count,
        "extractor": extractor or None,
        "album_mode": album_mode,
        "album_artist": infer_album_artist(info) if album_mode else None,
        "thumbnail": best_thumbnail_url(info),
    }


def cache_probe_result(url, result):
    with probe_cache_lock:
        probe_cache[url] = {"timestamp": time.time(), "result": dict(result)}


def get_cached_probe_result(url, max_age=600):
    with probe_cache_lock:
        cached = probe_cache.get(url)
        if not cached:
            return None
        if time.time() - cached["timestamp"] > max_age:
            probe_cache.pop(url, None)
            return None
        return dict(cached["result"])


def probe_url(url):
    completed = subprocess.run(
        build_probe_command(url, full_playlist=is_youtube_album_playlist(url)),
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT,
        check=False,
    )

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"yt-dlp exited with code {completed.returncode}"
        raise RuntimeError(message)

    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid probe data") from exc

    result = classify_probe_info(info, url=url)
    cache_probe_result(url, result)
    return result


def probe_ytdlp_display(url):
    completed = subprocess.run(
        build_probe_command(url, full_playlist=False),
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Could not refresh source preview metadata.")
    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid preview metadata") from exc
    return {
        "title": info.get("title") or info.get("fulltitle") or "",
        "thumbnail": best_thumbnail_url(info),
    }


def probe_spotify_url(url):
    cached = get_cached_probe_result(url)
    if cached:
        return cached

    spotify_type = spotify_link_type(url)
    result = {
        "kind": "spotify",
        "title": f"Spotify {spotify_type}",
        "count": None,
        "extractor": "Spotify oEmbed",
        "album_mode": spotify_type == "album",
        "album_artist": None,
        "thumbnail": None,
    }

    endpoint = "https://open.spotify.com/oembed?" + urlencode({"url": url})
    try:
        request_obj = Request(
            endpoint,
            headers={"User-Agent": "yt-dlp-server/1.0"},
        )
        with urlopen(request_obj, timeout=min(PROBE_TIMEOUT, 10)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        title = str(payload.get("title") or "").strip()
        thumbnail = str(payload.get("thumbnail_url") or "").strip()
        if title:
            result["title"] = title
        if thumbnail.startswith(("http://", "https://")):
            result["thumbnail"] = thumbnail
    except Exception:
        # oEmbed is a display enhancement only. Spotify downloads must not fail
        # merely because the preview service is unavailable.
        pass

    cache_probe_result(url, result)
    return result


def normalize_fallback_text(value):
    value = str(value or "").casefold().strip()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def first_tag_value(tags, *names):
    if not tags:
        return ""
    for name in names:
        value = tags.get(name)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        value = str(value or "").strip()
        if value:
            return value
    return ""


def validate_fallback_metadata(path, artist, title):
    try:
        audio = library_staging.MutagenFile(path, easy=True)
    except Exception:
        audio = None
    if audio is None or not getattr(audio, "tags", None):
        return False, "Downloaded fallback audio has no readable metadata."

    candidate_title = first_tag_value(audio.tags, "title")
    candidate_artist = first_tag_value(audio.tags, "artist", "albumartist", "album artist")
    expected_title = normalize_fallback_text(title)
    expected_artist = normalize_fallback_text(artist)

    if not candidate_title or normalize_fallback_text(candidate_title) != expected_title:
        return False, f"Fallback title mismatch: {candidate_title or 'unknown'}"
    if not candidate_artist or expected_artist not in normalize_fallback_text(candidate_artist):
        return False, f"Fallback artist mismatch: {candidate_artist or 'unknown'}"
    return True, None


def build_fallback_track_command(query, output_dir):
    cmd = [
        "spotdl",
        "download",
        query,
        "--simple-tui",
        "--headless",
        "--log-level",
        "INFO",
        "--threads",
        "1",
        "--format",
        "opus",
        "--bitrate",
        "disable",
        "--output",
        str(Path(output_dir) / "{title}.{output-ext}"),
        "--overwrite",
        "force",
        "--audio",
        "youtube-music",
        "youtube",
        "bandcamp",
        "soundcloud",
        "--yt-dlp-args",
        spotdl_ytdlp_args(),
    ]
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        cmd.extend(["--cookie-file", COOKIES_FILE])
    return cmd


def build_spotdl_command(job):
    query = job.get("download_query") or job["url"]
    queries = query if isinstance(query, list) else [query]
    cmd = [
        "spotdl",
        "download",
        *queries,
        "--simple-tui",
        "--headless",
        "--log-level",
        "INFO",
        "--threads",
        "1",
        "--format",
        "opus",
        "--bitrate",
        "disable",
        "--output",
        library_staging.staged_spotdl_output_template(TEMP_DIR, job),
        "--overwrite",
        "force" if job["force"] else "skip",
        "--lyrics",
        "--print-errors",
        "--save-errors",
        str(spotdl_error_file(job["id"])),
        "--max-filename-length",
        "180",
        "--audio",
        "youtube-music",
        "youtube",
        "bandcamp",
        "soundcloud",
        "--yt-dlp-args",
        spotdl_ytdlp_args(),
    ]

    if not job["force"]:
        cmd.extend(["--archive", str(SPOTDL_ARCHIVE_FILE)])
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        cmd.extend(["--cookie-file", COOKIES_FILE])

    return cmd


def build_command(job):
    stage = library_staging.ensure_stage_dir(TEMP_DIR, job)
    cmd = [
        "yt-dlp",
        "--newline",
        "--no-color",
        "--progress",
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "best",
        "--embed-metadata",
        "--embed-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--no-embed-chapters",
        "--no-embed-info-json",
        "--write-info-json",
        "--trim-filenames",
        "180",
        "--paths",
        str(stage),
        "--paths",
        f"temp:{stage / '.tmp'}",
        "--output",
        OUTPUT_TEMPLATE,
        "--print",
        "before_dl:__YTDLP_ITEM__=%(playlist_index|0)s/%(playlist_count|0)s",
        "--print",
        "after_move:__YTDLP_FILE__=%(filepath)s",
    ]

    if job["playlist"]:
        cmd.append("--yes-playlist")
        if job["album_mode"]:
            cmd.extend(album_playlist_metadata_args(job["compilation"]))
    else:
        cmd.append("--no-playlist")

    if job["force"]:
        cmd.append("--force-overwrites")
    else:
        cmd.extend(["--no-overwrites", "--download-archive", str(ARCHIVE_FILE)])

    cmd.extend(youtube_extractor_args(job["url"]))
    cmd.extend(cookie_args())
    cmd.append(job["url"])
    return cmd


def append_log(job_id, line):
    line = _ansi_re.sub("", line.rstrip())
    if not line:
        return

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return

        if "HTTP Error 403: Forbidden" in line:
            job["http_403"] = True

        if job.get("source") == "spotify":
            progress_match = _spotdl_progress_re.search(line)
            if progress_match:
                processed = int(progress_match.group(1))
                remaining_total = int(progress_match.group(2))
                base = job.get("retry_base_completed", 0)
                job["current_item"] = base + processed
                job["total_items"] = max(job["total_items"], base + remaining_total)
                job["progress"] = f"{processed}/{remaining_total} processed this attempt"

            if _spotdl_downloaded_re.search(line) or _spotdl_existing_re.search(line):
                job["completed_items"] += 1
                if job["total_items"]:
                    job["completed_items"] = min(job["completed_items"], job["total_items"])
                job["message"] = "Saved Spotify track"
                persist_jobs_locked()

            job["log"].append(line)
            job["log"] = job["log"][-40:]
            return

        if line.startswith("__YTDLP_ITEM__="):
            position = line.split("=", 1)[1]
            current, separator, total = position.partition("/")
            if separator and current.isdigit() and total.isdigit():
                job["current_item"] = int(current)
                job["total_items"] = int(total)
            return

        if line.startswith("__YTDLP_FILE__="):
            filepath = line.split("=", 1)[1]
            if filepath not in job["files"]:
                job["files"].append(filepath)
                job["completed_items"] += 1
            job["message"] = f"Saved {Path(filepath).name}"
            persist_jobs_locked()
            return

        playlist_match = _playlist_item_re.match(line)
        if playlist_match:
            job["current_item"] = int(playlist_match.group(1))
            job["total_items"] = int(playlist_match.group(2))

        match = _progress_re.match(line)
        if match:
            job["progress"] = match.group(1).strip()

        job["log"].append(line)
        job["log"] = job["log"][-40:]

def finalize_staged_job(job_id):
    with jobs_lock:
        source_job = jobs.get(job_id)
        if not source_job:
            return None

        target_id = source_job.get("finalize_target_id") or job_id
        target_job = jobs.get(target_id) or source_job
        target_id = target_job["id"]

        # Manual resolution/retry jobs can share the original collection's
        # staging directory. Keep the target pointed at that same owner.
        if not target_job.get("staging_owner_id") and source_job.get("staging_owner_id"):
            target_job["staging_owner_id"] = source_job["staging_owner_id"]

        target_job["status"] = "finalizing"
        target_job["message"] = "Publishing successful downloads"
        persist_jobs_locked()

        snapshot = dict(target_job)
        artist_override = target_job.get("metadata_artist")
        album_override = target_job.get("metadata_album")

    try:
        result = library_staging.publish_stage(
            TEMP_DIR,
            IMPORT_ROOT,
            snapshot,
            artist=artist_override,
            album=album_override,
        )
    except Exception as exc:
        result = {
            "status": "error",
            "message": str(exc),
            "artist": artist_override or "",
            "album": album_override or "",
            "media_count": 0,
        }

    with jobs_lock:
        target_job = jobs.get(target_id)
        source_job = jobs.get(job_id)
        if not target_job:
            return result

        target_job["metadata_artist"] = result.get("artist") or ""
        target_job["metadata_album"] = result.get("album") or ""
        target_job["staged_files"] = int(result.get("media_count") or 0)

        if result["status"] == "needs_metadata":
            target_job["status"] = "needs_metadata"
            target_job["message"] = (
                f"Ready to import - {target_job['staged_files']} staged file(s); "
                "confirm artist and album"
            )
        elif result["status"] == "published":
            is_partial = (
                target_job.get("source") == "spotify"
                and int(target_job.get("failed_items") or 0) > 0
            )
            target_job["status"] = "partial" if is_partial else "succeeded"
            target_job["progress"] = ""
            if not is_partial:
                target_job["failed_items"] = 0
            target_job["final_dir"] = result.get("final_dir")
            target_job["files"] = list(result.get("files") or [])
            target_job["staged_files"] = 0
            target_job["finished_at"] = time.time()

            if is_partial:
                failed_count = max(1, int(target_job.get("failed_items") or 0))
                if target_job.get("total_items"):
                    target_job["message"] = (
                        f"Partial - {target_job['completed_items']}/{target_job['total_items']} "
                        f"tracks published; {failed_count} failed"
                    )
                else:
                    target_job["message"] = (
                        f"Partial - {target_job['completed_items']} tracks published; "
                        f"{failed_count} failed"
                    )
            else:
                ignored_count = sum(
                    1 for item in target_job.get("missing_tracks", [])
                    if item.get("source") == "youtube" and item.get("ignored")
                )
                artist = result.get("artist") or ""
                album = result.get("album") or ""
                if target_job.get("source") == "spotify":
                    if target_job.get("total_items"):
                        target_job["message"] = (
                            f"Published - {target_job['completed_items']}/"
                            f"{target_job['total_items']} tracks saved"
                        )
                    else:
                        target_job["message"] = (
                            f"Published - {len(target_job['files'])} file(s)"
                        )
                elif target_job.get("total_items"):
                    suffix = f"; {ignored_count} ignored" if ignored_count else ""
                    target_job["message"] = (
                        f"Published - {target_job['completed_items']}/{target_job['total_items']} "
                        f"tracks saved{suffix} - {artist} / {album}"
                    )
                else:
                    target_job["message"] = (
                        f"Published - {len(target_job['files'])} file(s) - {artist} / {album}"
                    )
        else:
            # A partial run is only useful if its successful files actually
            # reach the library. Publishing errors remain retryable failures.
            target_job["status"] = "failed"
            target_job["message"] = f"Library import failed: {result.get('message') or 'unknown error'}"
            target_job["finished_at"] = time.time()

        if source_job and source_job["id"] != target_job["id"]:
            if result["status"] == "published":
                source_job["status"] = "succeeded"
                source_job["message"] = "Resolved missing track - published"
                source_job["finished_at"] = time.time()
            elif result["status"] == "needs_metadata":
                source_job["status"] = "succeeded"
                source_job["message"] = "Resolved missing track - album waiting for metadata"
                source_job["finished_at"] = time.time()
            else:
                source_job["status"] = "failed"
                source_job["message"] = target_job["message"]
                source_job["finished_at"] = time.time()

        persist_jobs_locked()

    if result.get("status") == "published":
        schedule_library_change_nudge()

    return result


def run_job(job_id):
    with jobs_lock:
        job = jobs[job_id]
        job["status"] = "running"
        job["started_at"] = time.time()
        job["message"] = "Starting spotDL" if job.get("source") == "spotify" else "Starting yt-dlp"
        persist_jobs_locked()

    is_spotify = job.get("source") == "spotify"
    cmd = build_spotdl_command(job) if is_spotify else build_command(job)
    error_file = spotdl_error_file(job_id) if is_spotify else None
    if error_file and error_file.exists():
        error_file.unlink()

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        assert process.stdout is not None
        for line in process.stdout:
            append_log(job_id, line)

        returncode = process.wait()
        spotdl_errors = []
        if error_file and error_file.exists():
            for error_line in error_file.read_text(encoding="utf-8", errors="replace").splitlines():
                error_line = error_line.strip()
                if not error_line or re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}", error_line):
                    continue
                spotdl_errors.append(error_line)
            try:
                error_file.unlink()
            except OSError:
                pass

        spotdl_missing_tracks = extract_spotdl_missing_tracks(spotdl_errors)
        unidentified_spotdl_errors = sum(
            1 for error in spotdl_errors
            if not _spotify_track_url_re.search(error)
        )

        should_finalize = False
        with jobs_lock:
            job = jobs[job_id]
            job["returncode"] = returncode
            job["finished_at"] = time.time()
            job["failed_items"] = (
                (len(spotdl_missing_tracks) + unidentified_spotdl_errors)
                if is_spotify
                else len(extract_youtube_missing_tracks(job.get("log", [])))
            )
            if is_spotify:
                job["missing_tracks"] = spotdl_missing_tracks
                job["unidentified_failed_items"] = unidentified_spotdl_errors
            else:
                job["missing_tracks"] = extract_youtube_missing_tracks(job.get("log", []))
            if spotdl_errors:
                job["log"].append("spotDL errors:")
                job["log"].extend(spotdl_errors[-20:])
                job["log"] = job["log"][-40:]

            ignored_youtube_only = (
                not is_spotify
                and returncode != 0
                and close_job_if_only_ignored_locked(job)
            )
            failed = (returncode != 0 or bool(spotdl_errors)) and not ignored_youtube_only
            staged_media = library_staging.media_files(
                library_staging.stage_dir(TEMP_DIR, job)
            )

            if is_spotify and failed and (staged_media or job.get("retry_base_completed", 0)):
                job["status"] = "partial"
                if job["total_items"]:
                    job["message"] = (
                        f"Partial - {job['completed_items']}/{job['total_items']} tracks saved; "
                        f"{max(1, job['failed_items'])} failed"
                    )
                else:
                    job["message"] = (
                        f"Partial - {job['completed_items']} tracks saved; "
                        f"{max(1, job['failed_items'])} failed"
                    )
                should_finalize = bool(staged_media)
            elif not failed:
                job["status"] = "succeeded"
                should_finalize = bool(staged_media)
                if is_spotify:
                    if job["total_items"]:
                        job["message"] = f"Completed - {job['completed_items']}/{job['total_items']} tracks saved"
                    elif job["completed_items"]:
                        job["message"] = f"Completed - {job['completed_items']} tracks saved"
                    else:
                        job["message"] = "Completed - nothing new to download"
                    apply_manual_resolution_success(job)
                elif ignored_youtube_only:
                    pass
                elif job["files"]:
                    count = len(job["files"])
                    job["message"] = f"Completed - {count} file{'s' if count != 1 else ''}"
                else:
                    job["message"] = "Completed - nothing new to download"
            else:
                job["status"] = "failed"
                if is_spotify:
                    if job["total_items"]:
                        job["message"] = (
                            f"Failed - 0 new tracks published; "
                            f"{max(1, job['failed_items'])} track(s) failed"
                        )
                    else:
                        job["message"] = f"spotDL failed; {max(1, job['failed_items'])} track(s) failed"
                elif job["http_403"] and is_youtube_url(job["url"]):
                    if BGUTIL_SERVER_HOME.is_dir():
                        job["message"] = "YouTube returned HTTP 403 despite PO-token support"
                    else:
                        job["message"] = "YouTube returned HTTP 403 - PO-token provider unavailable"
                else:
                    job["message"] = f"yt-dlp exited with code {returncode}"
            persist_jobs_locked()

        if should_finalize:
            finalize_staged_job(job_id)
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job["status"] = "failed"
            job["finished_at"] = time.time()
            job["message"] = str(exc)
            job["log"].append(f"ERROR: {exc}")
            persist_jobs_locked()

def backfill_job_display_metadata():
    with jobs_lock:
        candidates = [
            (
                job["id"],
                job["url"],
                job.get("source", "yt-dlp"),
                not bool(job.get("source_title")),
                not bool(job.get("source_thumbnail")),
            )
            for job in jobs.values()
            if not job.get("source_title") or not job.get("source_thumbnail")
        ]

    changed = False
    for job_id, url, source, needs_title, needs_thumbnail in candidates:
        try:
            result = (
                probe_spotify_url(url)
                if source == "spotify"
                else probe_ytdlp_display(url)
            )
        except (subprocess.TimeoutExpired, RuntimeError):
            continue

        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                continue
            title = result.get("title")
            thumbnail = result.get("thumbnail")
            if (
                needs_title
                and title
                and title != f"Spotify {spotify_link_type(url)}"
            ):
                job["source_title"] = title
                changed = True
            if needs_thumbnail and thumbnail:
                job["source_thumbnail"] = thumbnail
                changed = True

    if changed:
        persist_jobs()


def worker():
    while True:
        job_id = download_queue.get()
        try:
            run_job(job_id)
        finally:
            download_queue.task_done()


def staged_jobs_to_finalize_after_restore():
    finalize_ids = []
    changed = False
    with jobs_lock:
        for job in jobs.values():
            staged = library_staging.media_files(
                library_staging.stage_dir(TEMP_DIR, job)
            )
            if not staged:
                continue

            if job.get("status") in {"succeeded", "partial"}:
                finalize_ids.append(job["id"])
                continue

            # Before partial publishing existed, mixed Spotify runs were
            # persisted as failed even though their successful tracks remained
            # safely staged. Recover those jobs on the first restart after
            # upgrading instead of forcing the entire playlist to run again.
            if job.get("source") == "spotify" and job.get("status") == "failed":
                job["status"] = "partial"
                job["failed_items"] = max(
                    1,
                    int(job.get("failed_items") or 0),
                    len([
                        item for item in job.get("missing_tracks", [])
                        if not item.get("resolved")
                    ]),
                )
                job["message"] = (
                    f"Recovering partial Spotify download - "
                    f"{len(staged)} staged track(s) ready to publish"
                )
                finalize_ids.append(job["id"])
                changed = True

        if changed:
            persist_jobs_locked()

    return finalize_ids


restored_queue_ids = load_persisted_jobs()
restored_finalize_ids = staged_jobs_to_finalize_after_restore()

for restored_id in restored_finalize_ids:
    finalize_staged_job(restored_id)

for restored_id in restored_queue_ids:
    try:
        download_queue.put_nowait(restored_id)
    except queue.Full:
        with jobs_lock:
            restored_job = jobs.get(restored_id)
            if restored_job:
                restored_job["status"] = "failed"
                restored_job["message"] = "Could not restore queued job - queue capacity exceeded"
                restored_job["finished_at"] = time.time()
                persist_jobs_locked()

threading.Thread(target=worker, name="yt-dlp-worker", daemon=True).start()
threading.Thread(
    target=backfill_job_display_metadata,
    name="job-display-metadata",
    daemon=True,
).start()

# A short startup nudge also repairs the specific upgrade case where files are
# already in the library but Plex previously noticed them during another scan
# and never queued a follow-up.
schedule_library_change_nudge(delay=min(5, PLEX_RESCAN_NUDGE_SECONDS))


def request_root_id_locked(job):
    current = job
    seen = set()
    while current and current.get("id") not in seen:
        seen.add(current.get("id"))
        parent_id = current.get("retry_of") or current.get("resolution_of")
        if not parent_id:
            return current.get("id")
        parent = jobs.get(parent_id)
        if parent is None:
            return current.get("id")
        current = parent
    return job.get("id")


def request_members_locked(root_id):
    return [
        job for job in jobs.values()
        if request_root_id_locked(job) == root_id
    ]


def request_representative_locked(members):
    primary = [job for job in members if not job.get("resolution_of")]
    pool = primary or members
    return max(
        pool,
        key=lambda item: (
            item.get("attempt", 1),
            item.get("created_at", 0),
        ),
    )


def public_job_from_group_locked(root_id, members):
    representative = request_representative_locked(members)
    primary = sorted(
        [job for job in members if not job.get("resolution_of")],
        key=lambda item: (item.get("attempt", 1), item.get("created_at", 0)),
    )
    if not primary:
        primary = [representative]

    # Prefer the most recently known display metadata anywhere in the request
    # chain so a retry does not lose the useful title/cover from its parent.
    display_order = sorted(
        members,
        key=lambda item: item.get("created_at", 0),
        reverse=True,
    )

    def first_value(key):
        for item in display_order:
            value = item.get(key)
            if value:
                return value
        return ""

    active_resolutions = [
        item.get("spotify_track_url")
        for item in members
        if item.get("resolution_of")
        and item.get("status") in {"queued", "running", "finalizing"}
        and item.get("spotify_track_url")
    ]

    attempt_history = [
        {
            "id": item["id"],
            "attempt": item.get("attempt", 1),
            "status": item.get("status"),
            "message": item.get("message") or "",
        }
        for item in reversed(primary)
    ]

    return {
        "id": representative["id"],
        "request_id": root_id,
        "request_member_ids": [item["id"] for item in members],
        "attempt_count": len(primary),
        "attempt_history": attempt_history,
        "active_resolution_tracks": active_resolutions,
        "url": representative["url"],
        "status": representative["status"],
        "message": representative["message"],
        "progress": representative["progress"],
        "playlist": representative["playlist"],
        "album_mode": representative["album_mode"],
        "compilation": representative["compilation"],
        "force": representative["force"],
        "current_item": representative["current_item"],
        "total_items": representative["total_items"],
        "completed_items": representative["completed_items"],
        "files": list(representative["files"]),
        "log": list(representative["log"]),
        "created_at": max(item["created_at"] for item in members),
        "started_at": representative["started_at"],
        "finished_at": representative["finished_at"],
        "returncode": representative["returncode"],
        "http_403": representative["http_403"],
        "retry_of": representative["retry_of"],
        "attempt": representative["attempt"],
        "source": representative.get("source", "yt-dlp"),
        "failed_items": representative.get("failed_items", 0),
        "missing_tracks": [
            dict(item) for item in representative.get("missing_tracks", [])
        ],
        "resolution_of": None,
        "spotify_track_url": None,
        "manual_source_url": None,
        "ignored_items": sum(
            1 for item in representative.get("missing_tracks", [])
            if item.get("source") == "youtube" and item.get("ignored")
        ),
        "staging_owner_id": representative.get("staging_owner_id"),
        "source_title": first_value("source_title"),
        "source_album_artist": first_value("source_album_artist"),
        "source_thumbnail": first_value("source_thumbnail"),
        "metadata_artist": first_value("metadata_artist"),
        "metadata_album": first_value("metadata_album"),
        "staged_files": representative.get("staged_files", 0),
        "final_dir": first_value("final_dir"),
    }


def public_jobs():
    with jobs_lock:
        groups = {}
        for job in jobs.values():
            root_id = request_root_id_locked(job)
            groups.setdefault(root_id, []).append(job)

        public = [
            public_job_from_group_locked(root_id, members)
            for root_id, members in groups.items()
        ]
        public.sort(key=lambda item: item["created_at"], reverse=True)
        return public


@app.get("/chrome-extension.zip")
def download_chrome_extension():
    if not CHROME_EXTENSION_ZIP.is_file():
        return jsonify({"ok": False, "error": "Chrome extension package is unavailable."}), 404

    return send_file(
        CHROME_EXTENSION_ZIP,
        mimetype="application/zip",
        as_attachment=True,
        download_name=CHROME_EXTENSION_ZIP.name,
        max_age=0,
    )


@app.get("/")
def index():
    return render_template(
        "index.html",
        jobs=public_jobs(),
        import_root=str(IMPORT_ROOT),
        versions=VERSIONS,
    )


@app.post("/download")
def download():
    url, error = validate_url(request.form.get("url"))
    if error:
        return render_template(
            "index.html",
            jobs=public_jobs(),
            import_root=str(IMPORT_ROOT),
            versions=VERSIONS,
            error=error,
        ), 400

    source = "spotify" if is_spotify_url(url) else "yt-dlp"
    playlist_requested = request.form.get("playlist") == "on" if source == "yt-dlp" else False
    probe_result = get_cached_probe_result(url)
    if probe_result is None:
        try:
            probe_result = (
                probe_spotify_url(url)
                if source == "spotify"
                else probe_url(url)
            )
        except (subprocess.TimeoutExpired, RuntimeError):
            probe_result = None

    detected_collection = bool(
        probe_result and probe_result.get("kind") == "playlist"
    )
    playlist_enabled = bool(
        source == "yt-dlp"
        and playlist_requested
        and (detected_collection if probe_result else True)
    )
    collection_mode = bool(playlist_enabled and detected_collection)
    album_mode = bool(
        source == "yt-dlp"
        and collection_mode
        and is_youtube_album_playlist(url)
    )

    compilation = bool(
        probe_result
        and probe_result.get("album_artist") == "Various Artists"
    )
    expected_count = (
        probe_result.get("count")
        if probe_result and isinstance(probe_result.get("count"), int)
        else 0
    )

    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id,
        "url": url,
        "playlist": playlist_enabled,
        "collection_mode": collection_mode,
        "album_mode": album_mode,
        "compilation": compilation,
        "force": request.form.get("force") == "on",
        "status": "queued",
        "message": "Waiting for worker",
        "progress": "",
        "current_item": 0,
        "total_items": expected_count,
        "completed_items": 0,
        "files": [],
        "log": [],
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "returncode": None,
        "http_403": False,
        "retry_of": None,
        "attempt": 1,
        "source": source,
        "retry_base_completed": 0,
        "failed_items": 0,
        "missing_tracks": [],
        "resolution_of": None,
        "spotify_track_url": None,
        "manual_source_url": None,
        "download_query": None,
        "staging_owner_id": job_id,
        "source_title": probe_result.get("title") if probe_result else None,
        "source_album_artist": (
            probe_result.get("album_artist")
            if probe_result and album_mode
            else None
        ),
        "source_thumbnail": probe_result.get("thumbnail") if probe_result else None,
        "metadata_artist": None,
        "metadata_album": None,
        "staged_files": 0,
        "final_dir": None,
        "finalize_target_id": None,
    }

    with jobs_lock:
        jobs[job_id] = job
        if len(jobs) > MAX_HISTORY:
            removable = sorted(
                (item for item in jobs.values() if item["status"] in {"succeeded", "partial", "failed"}),
                key=lambda item: item["created_at"],
            )
            while len(jobs) > MAX_HISTORY and removable:
                jobs.pop(removable.pop(0)["id"], None)
        persist_jobs_locked()

    try:
        download_queue.put_nowait(job_id)
    except queue.Full:
        with jobs_lock:
            jobs.pop(job_id, None)
            persist_jobs_locked()
        return render_template(
            "index.html",
            jobs=public_jobs(),
            import_root=str(IMPORT_ROOT),
            versions=VERSIONS,
            error="Download queue is full.",
        ), 429

    return redirect(url_for("index"))


@app.delete("/api/jobs/<job_id>")
def remove_job(job_id):
    removable_statuses = {"failed", "succeeded", "partial", "needs_metadata"}

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404

        root_id = request_root_id_locked(job)
        members = request_members_locked(root_id)
        representative = request_representative_locked(members)

        if representative.get("status") not in removable_statuses:
            return jsonify({
                "ok": False,
                "error": "Only finished, failed, or metadata-waiting requests can be removed.",
            }), 409

        active = [
            item for item in members
            if item.get("status") in {"queued", "running", "finalizing"}
        ]
        if active:
            return jsonify({
                "ok": False,
                "error": "This request still has active work.",
            }), 409

        owner_ids = {
            library_staging.staging_owner_id(item)
            for item in members
            if library_staging.staging_owner_id(item)
        }
        member_ids = {item["id"] for item in members}
        destructive = representative.get("status") not in {"succeeded", "partial"}

        for member_id in member_ids:
            jobs.pop(member_id, None)

        remaining_owner_ids = {
            library_staging.staging_owner_id(item)
            for item in jobs.values()
        }
        discard_owner_ids = (
            owner_ids - remaining_owner_ids
            if destructive
            else set()
        )
        persist_jobs_locked()

    for owner_id in discard_owner_ids:
        library_staging.discard_stage(
            TEMP_DIR,
            {"id": owner_id, "staging_owner_id": owner_id},
        )

    return jsonify({
        "ok": True,
        "removed": sorted(member_ids),
        "removed_count": len(member_ids),
        "discarded_staging": bool(discard_owner_ids),
    }), 200


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id):
    with jobs_lock:
        previous = jobs.get(job_id)
        if previous is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        if previous["status"] not in {"failed", "partial"}:
            return jsonify({"ok": False, "error": "Only failed or partial jobs can be retried."}), 409
        if any(job.get("retry_of") == job_id for job in jobs.values()):
            return jsonify({
                "ok": False,
                "error": "This attempt has already been retried. Retry the latest attempt instead.",
            }), 409

        retry_id = uuid.uuid4().hex[:10]
        completed = (
            min(previous["completed_items"], previous["total_items"])
            if previous["total_items"]
            else previous["completed_items"]
        )
        source = previous.get("source", "yt-dlp")
        unresolved_urls = [
            item.get("spotify_url")
            for item in previous.get("missing_tracks", [])
            if item.get("spotify_url") and not item.get("resolved")
        ]
        download_query = (
            list(dict.fromkeys(unresolved_urls))
            if (
                source == "spotify"
                and unresolved_urls
                and not previous.get("unidentified_failed_items")
            )
            else None
        )
        retry_count = len(download_query or [])

        retry = {
            "id": retry_id,
            "url": previous["url"],
            "playlist": previous["playlist"],
            "collection_mode": previous.get("collection_mode", previous["playlist"]),
            "album_mode": previous["album_mode"],
            "compilation": previous["compilation"],
            # A retry is a continuation: never overwrite files that already succeeded.
            "force": False,
            "status": "queued",
            "message": (
                f"Retrying {retry_count} failed Spotify track(s)"
                if retry_count
                else (
                    f"Continuing after {completed}/{previous['total_items']} tracks"
                    if previous["total_items"]
                    else "Retry queued"
                )
            ),
            "progress": "",
            "current_item": completed,
            "total_items": previous["total_items"],
            "completed_items": completed,
            "files": list(previous["files"]),
            "log": [
                (
                    f"Retrying {retry_count} unresolved Spotify track(s) from job {job_id}."
                    if retry_count
                    else f"Retrying job {job_id}; existing files and archive entries will be skipped."
                )
            ],
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "http_403": False,
            "retry_of": job_id,
            "attempt": previous.get("attempt", 1) + 1,
            "source": source,
            "retry_base_completed": completed,
            "failed_items": 0,
            "unidentified_failed_items": 0,
            "missing_tracks": [],
            "resolution_of": None,
            "spotify_track_url": None,
            "manual_source_url": None,
            "download_query": download_query,
            "staging_owner_id": previous.get("staging_owner_id") or retry_id,
            "source_title": previous.get("source_title"),
            "source_album_artist": previous.get("source_album_artist"),
            "source_thumbnail": previous.get("source_thumbnail"),
            "metadata_artist": previous.get("metadata_artist"),
            "metadata_album": previous.get("metadata_album"),
            "staged_files": 0,
            "final_dir": previous.get("final_dir"),
            "finalize_target_id": None,
        }
        jobs[retry_id] = retry
        persist_jobs_locked()

    try:
        download_queue.put_nowait(retry_id)
    except queue.Full:
        with jobs_lock:
            jobs.pop(retry_id, None)
            persist_jobs_locked()
        return jsonify({"ok": False, "error": "Download queue is full."}), 429

    return jsonify({
        "ok": True,
        "job_id": retry_id,
        "retry_tracks": retry_count,
    }), 202


@app.post("/api/jobs/<job_id>/ignore")
def ignore_missing_youtube_track(job_id):
    payload = request.get_json(silent=True) or request.form
    source_id = (payload.get("source_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", source_id):
        return jsonify({"ok": False, "error": "A valid YouTube video ID is required."}), 400

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        if job.get("source") != "yt-dlp" or job["status"] != "failed":
            return jsonify({"ok": False, "error": "Only failed yt-dlp jobs can ignore missing tracks."}), 409

        missing = next(
            (
                item for item in extract_youtube_missing_tracks(job.get("log", []))
                if item.get("source_id") == source_id
            ),
            None,
        )
        if missing is None:
            return jsonify({"ok": False, "error": "That video is not a missing track in this job."}), 404

        ignored = load_ignored_tracks()
        key = f"youtube:{source_id}"
        ignored[key] = {
            "source": "youtube",
            "source_id": source_id,
            "reason": missing.get("reason") or "Unavailable",
            "ignored_at": time.time(),
        }
        try:
            persist_ignored_tracks(ignored)
        except OSError as exc:
            return jsonify({"ok": False, "error": f"Could not persist ignored track: {exc}"}), 500

        # The ignore is global by video ID. Refresh every historical attempt
        # containing the same unavailable video so stale red failures disappear.
        for candidate in jobs.values():
            if candidate.get("source") != "yt-dlp":
                continue
            ids = {
                item.get("source_id")
                for item in extract_youtube_missing_tracks(candidate.get("log", []), ignored)
            }
            if source_id not in ids:
                continue
            candidate["missing_tracks"] = extract_youtube_missing_tracks(candidate.get("log", []), ignored)
            if candidate.get("status") == "failed":
                close_job_if_only_ignored_locked(candidate, ignored)

        persist_jobs_locked()
        requested = jobs.get(job_id)
        should_finalize = bool(
            requested
            and requested.get("status") == "succeeded"
            and library_staging.media_files(
                library_staging.stage_dir(TEMP_DIR, requested)
            )
        )

    if should_finalize:
        finalize_staged_job(job_id)

    return jsonify({"ok": True, "source_id": source_id}), 200


@app.post("/api/jobs/<job_id>/metadata")
def confirm_job_metadata(job_id):
    payload = request.get_json(silent=True) or request.form
    artist = library_staging.clean_artist_name(payload.get("artist"))
    album = library_staging.clean_album_name(payload.get("album"))

    if not artist:
        return jsonify({"ok": False, "error": "Artist / album artist is required."}), 400
    if not album:
        return jsonify({"ok": False, "error": "Album name is required."}), 400

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        if job.get("status") != "needs_metadata":
            return jsonify({"ok": False, "error": "This job is not waiting for metadata."}), 409

        job["metadata_artist"] = artist
        job["metadata_album"] = album
        persist_jobs_locked()

    result = finalize_staged_job(job_id)
    if not result:
        return jsonify({"ok": False, "error": "Job disappeared during finalization."}), 404
    if result.get("status") == "published":
        return jsonify({"ok": True, "final_dir": result.get("final_dir")}), 200
    if result.get("status") == "needs_metadata":
        return jsonify({"ok": False, "error": "Metadata is still incomplete."}), 409
    return jsonify({"ok": False, "error": result.get("message") or "Library import failed."}), 500


@app.post("/api/jobs/<job_id>/resolve")
def resolve_missing_spotify_track(job_id):
    payload = request.get_json(silent=True) or request.form
    spotify_url = (payload.get("spotify_url") or "").strip()
    source_url = (payload.get("source_url") or "").strip()

    if not spotify_url or not is_spotify_url(spotify_url) or spotify_link_type(spotify_url) != "track":
        return jsonify({"ok": False, "error": "A valid Spotify track URL is required."}), 400
    if not is_manual_spotify_source_url(source_url):
        return jsonify({
            "ok": False,
            "error": "Use a direct YouTube watch, Bandcamp track, or SoundCloud track URL.",
        }), 400

    with jobs_lock:
        parent = jobs.get(job_id)
        if parent is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        if parent.get("source") != "spotify" or parent["status"] not in {"failed", "partial"}:
            return jsonify({"ok": False, "error": "Only failed or partial Spotify jobs can resolve missing tracks."}), 409

        missing = next(
            (item for item in parent.get("missing_tracks", []) if item.get("spotify_url") == spotify_url),
            None,
        )
        if missing is None:
            return jsonify({"ok": False, "error": "That track is not listed as missing for this job."}), 404
        if missing.get("resolved"):
            return jsonify({"ok": False, "error": "That track has already been resolved."}), 409
        if any(
            item.get("resolution_of") == job_id
            and item.get("spotify_track_url") == spotify_url
            and item.get("status") in {"queued", "running"}
            for item in jobs.values()
        ):
            return jsonify({"ok": False, "error": "A resolution attempt is already running for this track."}), 409

        resolution_id = uuid.uuid4().hex[:10]
        resolution = {
            "id": resolution_id,
            "url": spotify_url,
            "playlist": False,
            "collection_mode": False,
            "album_mode": False,
            "compilation": False,
            "force": False,
            "status": "queued",
            "message": f"Resolving {missing.get('label') or 'missing Spotify track'}",
            "progress": "",
            "current_item": 0,
            "total_items": 1,
            "completed_items": 0,
            "files": [],
            "log": [f"Manual source: {source_url}"],
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "http_403": False,
            "retry_of": None,
            "attempt": 1,
            "source": "spotify",
            "retry_base_completed": 0,
            "failed_items": 0,
            "unidentified_failed_items": 0,
            "missing_tracks": [],
            "resolution_of": job_id,
            "spotify_track_url": spotify_url,
            "manual_source_url": source_url,
            "download_query": f"{source_url}|{spotify_url}",
            "staging_owner_id": parent.get("staging_owner_id") or resolution_id,
            "source_title": parent.get("source_title"),
            "source_album_artist": parent.get("source_album_artist"),
            "source_thumbnail": parent.get("source_thumbnail"),
            "metadata_artist": parent.get("metadata_artist"),
            "metadata_album": parent.get("metadata_album"),
            "staged_files": parent.get("staged_files", 0),
            "final_dir": None,
            "finalize_target_id": job_id,
        }
        jobs[resolution_id] = resolution
        persist_jobs_locked()

    try:
        download_queue.put_nowait(resolution_id)
    except queue.Full:
        with jobs_lock:
            jobs.pop(resolution_id, None)
            persist_jobs_locked()
        return jsonify({"ok": False, "error": "Download queue is full."}), 429

    return jsonify({"ok": True, "job_id": resolution_id}), 202


@app.post("/api/fallback-track")
def api_fallback_track():
    payload = request.get_json(silent=True) or {}
    artist = str(payload.get("artist") or "").strip()
    title = str(payload.get("title") or "").strip()
    album = str(payload.get("album") or "").strip()

    if not artist or not title:
        return jsonify({
            "ok": False,
            "error": "artist and title are required for fallback matching.",
        }), 400
    if len(artist) > 300 or len(title) > 300 or len(album) > 300:
        return jsonify({"ok": False, "error": "Fallback metadata is too long."}), 400
    if VERSIONS.get("spotdl", "unavailable") == "unavailable":
        return jsonify({"ok": False, "error": "spotDL is unavailable."}), 503

    request_id = uuid.uuid4().hex[:12]
    work_dir = TEMP_DIR / "fallback" / request_id
    work_dir.mkdir(parents=True, exist_ok=True)
    query = f"{artist} - {title}"

    try:
        completed = subprocess.run(
            build_fallback_track_command(query, work_dir),
            capture_output=True,
            text=True,
            timeout=FALLBACK_TRACK_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({
            "ok": False,
            "error": "spotDL fallback timed out.",
            "query": query,
        }), 504
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({
            "ok": False,
            "error": f"Could not start spotDL fallback: {exc}",
            "query": query,
        }), 502

    files = library_staging.media_files(work_dir)
    if completed.returncode != 0 or len(files) != 1:
        detail = [
            line.strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip()
        ][-8:]
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({
            "ok": False,
            "error": (
                "spotDL fallback did not produce exactly one audio file."
                if completed.returncode == 0
                else f"spotDL fallback exited with code {completed.returncode}."
            ),
            "query": query,
            "detail": detail,
        }), 404 if completed.returncode == 0 else 502

    audio_file = files[0]
    valid, validation_error = validate_fallback_metadata(audio_file, artist, title)
    if not valid:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({
            "ok": False,
            "error": validation_error or "Fallback metadata did not match.",
            "query": query,
        }), 409

    response = send_file(
        audio_file,
        as_attachment=False,
        download_name=audio_file.name,
        max_age=0,
    )
    response.headers["X-Fallback-Provider"] = "spotdl"
    response.headers["X-Fallback-Query"] = query
    if album:
        response.headers["X-Fallback-Album"] = album[:200]
    response.call_on_close(lambda: shutil.rmtree(work_dir, ignore_errors=True))
    return response


@app.post("/api/probe")
def api_probe():
    payload = request.get_json(silent=True) or request.form
    url, error = validate_url(payload.get("url"))
    if error:
        return jsonify({"ok": False, "error": error}), 400

    if is_spotify_url(url):
        result = probe_spotify_url(url)
        return jsonify({
            "ok": True,
            **result,
            "is_album": bool(result.get("album_mode")),
        })

    try:
        result = probe_url(url)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Link inspection timed out."}), 504
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    return jsonify({
        "ok": True,
        **result,
        "is_album": bool(result.get("album_mode")),
    })


@app.get("/api/jobs")
def api_jobs():
    return jsonify(public_jobs())


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "queue": download_queue.qsize(),
            "import_root": str(IMPORT_ROOT),
            "versions": VERSIONS,
            "jobs_state": {
                "path": str(JOBS_STATE_FILE),
                "persisted": JOBS_STATE_FILE.is_file(),
            },
            "staging": {
                "path": str(TEMP_DIR / "staging"),
                "outside_library": not str(TEMP_DIR).startswith(str(MUSIC_ROOT)),
            },
            "ignored_tracks": {
                "path": str(IGNORED_TRACKS_FILE),
                "count": len(load_ignored_tracks()),
            },
            "fallback_track": {
                "enabled": VERSIONS.get("spotdl", "unavailable") != "unavailable",
                "endpoint": "/api/fallback-track",
                "timeout_seconds": FALLBACK_TRACK_TIMEOUT,
            },
            "spotify": {
                "enabled": VERSIONS.get("spotdl", "unavailable") != "unavailable",
                "archive": str(SPOTDL_ARCHIVE_FILE),
                "format": "opus",
            },
            "youtube_po": {
                "available": BGUTIL_SERVER_HOME.is_dir(),
                "server_home": str(BGUTIL_SERVER_HOME),
                "player_client": YOUTUBE_PLAYER_CLIENT,
            },
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
