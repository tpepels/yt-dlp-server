import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for

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
BGUTIL_SERVER_HOME = Path(os.getenv("BGUTIL_SERVER_HOME", "/opt/bgutil-ytdlp-pot-provider/server"))
YOUTUBE_PLAYER_CLIENT = os.getenv("YOUTUBE_PLAYER_CLIENT", "mweb").strip() or "mweb"

if not IMPORT_SUBDIR or Path(IMPORT_SUBDIR).is_absolute() or ".." in Path(IMPORT_SUBDIR).parts:
    raise RuntimeError("IMPORT_SUBDIR must be a safe relative path")

IMPORT_ROOT = MUSIC_ROOT / IMPORT_SUBDIR
ARCHIVE_FILE = STATE_DIR / "archive.txt"
SPOTDL_ARCHIVE_FILE = STATE_DIR / "spotdl-archive.txt"
JOBS_STATE_FILE = STATE_DIR / "jobs.json"

for directory in (IMPORT_ROOT, STATE_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

OUTPUT_TEMPLATE = (
    "%(album_artist,artist,creator,uploader|Unknown Artist).120S/"
    "%(album,playlist|Singles).160S/"
    "%(track_number,playlist_index&{:02d} - |)s"
    "%(track,title).180S [%(id)s].%(ext)s"
)
SPOTDL_OUTPUT_TEMPLATE = str(
    IMPORT_ROOT
    / "{album-artist}"
    / "{album}"
    / "{track-number} - {title} [{track-id}].{output-ext}"
)

jobs = {}
jobs_lock = threading.Lock()
probe_cache = {}
probe_cache_lock = threading.Lock()
download_queue = queue.Queue(maxsize=MAX_QUEUE)
_progress_re = re.compile(r"^\[download\]\s+(.+?)(?:\s+of\s+|\s+at\s+|\s+ETA\s+|$)")
_playlist_item_re = re.compile(r"^\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)")
_spotdl_progress_re = re.compile(r"(\d+)/(\d+) complete")
_spotdl_downloaded_re = re.compile(r'\bDownloaded "')
_spotdl_existing_re = re.compile(r"\bSkipping .+\(file already exists\)")
_spotify_track_url_re = re.compile(r"https://open\.spotify\.com/track/[A-Za-z0-9]+(?:\?[^\s]*)?")
_ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


def normalize_persisted_job(raw):
    if not isinstance(raw, dict) or not raw.get("url"):
        return None

    now = time.time()
    job_id = str(raw.get("id") or uuid.uuid4().hex[:10])
    status = raw.get("status") if raw.get("status") in {"queued", "running", "succeeded", "failed"} else "failed"
    source = raw.get("source") or ("spotify" if is_spotify_url(raw["url"]) else "yt-dlp")
    job = {
        "id": job_id,
        "url": str(raw["url"]),
        "playlist": bool(raw.get("playlist", False)),
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
        "retry_base_completed": int(raw.get("retry_base_completed") or 0),
        "failed_items": int(raw.get("failed_items") or 0),
        "missing_tracks": list(raw.get("missing_tracks") or []),
        "resolution_of": raw.get("resolution_of"),
        "spotify_track_url": raw.get("spotify_track_url"),
        "manual_source_url": raw.get("manual_source_url"),
        "download_query": raw.get("download_query"),
    }

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
    if job["status"] == "running":
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
    if len(restored) > MAX_HISTORY:
        restored = restored[-MAX_HISTORY:]

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
        if "No results found for song:" not in error:
            continue
        match = _spotify_track_url_re.search(error)
        if not match:
            continue
        spotify_url = match.group(0)
        if spotify_url in seen:
            continue
        label = error.split("No results found for song:", 1)[1].strip() or spotify_url
        missing.append({
            "spotify_url": spotify_url,
            "label": label,
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

    for missing in parent.get("missing_tracks", []):
        if missing.get("spotify_url") == spotify_url:
            missing["resolved"] = True
            missing["source_url"] = source_url

    unresolved = [item for item in parent.get("missing_tracks", []) if not item.get("resolved")]
    if not unresolved and parent.get("missing_tracks"):
        parent["status"] = "succeeded"
        parent["failed_items"] = 0
        if parent.get("total_items"):
            parent["completed_items"] = parent["total_items"]
            parent["current_item"] = parent["total_items"]
            parent["progress"] = ""
            parent["message"] = f"Completed - {parent['total_items']}/{parent['total_items']} tracks saved"
        else:
            parent["completed_items"] += 1
            parent["message"] = "Completed - missing track resolved manually"
        parent["log"].append(f"Resolved {spotify_url} from {source_url}")
        parent["log"] = parent["log"][-40:]


def album_playlist_metadata_args(compilation=False):
    album_artist_source = (
        "Various Artists"
        if compilation
        else "%(playlist_channel,playlist_uploader,album_artist,artist|)s"
    )
    return [
        "--parse-metadata",
        f"{album_artist_source}:%(album_artist)s",
        "--replace-in-metadata",
        "album_artist",
        r"\s+- Topic$",
        "",
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
        value = (
            entry.get("artist")
            or entry.get("album_artist")
            or entry.get("uploader")
            or entry.get("channel")
        )
        value = normalize_topic_artist(value)
        if value:
            artists.add(value)

    if len(artists) > 1:
        return "Various Artists"
    if len(artists) == 1:
        return next(iter(artists))

    return normalize_topic_artist(
        info.get("album_artist")
        or info.get("uploader")
        or info.get("channel")
    ) or None


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

    album_mode = bool(url and is_youtube_album_playlist(url))
    return {
        "kind": kind,
        "title": info.get("title") or info.get("fulltitle") or "Untitled",
        "count": count,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "album_mode": album_mode,
        "album_artist": infer_album_artist(info) if album_mode else None,
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


def build_spotdl_command(job):
    cmd = [
        "spotdl",
        "download",
        job.get("download_query") or job["url"],
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
        SPOTDL_OUTPUT_TEMPLATE,
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
        "--trim-filenames",
        "180",
        "--paths",
        str(IMPORT_ROOT),
        "--paths",
        f"temp:{TEMP_DIR}",
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

        with jobs_lock:
            job = jobs[job_id]
            job["returncode"] = returncode
            job["finished_at"] = time.time()
            job["failed_items"] = len(spotdl_errors)
            job["missing_tracks"] = spotdl_missing_tracks
            if spotdl_errors:
                job["log"].append("spotDL errors:")
                job["log"].extend(spotdl_errors[-20:])
                job["log"] = job["log"][-40:]

            failed = returncode != 0 or bool(spotdl_errors)
            if not failed:
                job["status"] = "succeeded"
                if is_spotify:
                    if job["total_items"]:
                        job["message"] = f"Completed - {job['completed_items']}/{job['total_items']} tracks saved"
                    elif job["completed_items"]:
                        job["message"] = f"Completed - {job['completed_items']} tracks saved"
                    else:
                        job["message"] = "Completed - nothing new to download"
                    apply_manual_resolution_success(job)
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
                            f"Partial - {job['completed_items']}/{job['total_items']} tracks saved; "
                            f"{max(1, job['failed_items'])} failed"
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
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job["status"] = "failed"
            job["finished_at"] = time.time()
            job["message"] = str(exc)
            job["log"].append(f"ERROR: {exc}")
            persist_jobs_locked()

def worker():
    while True:
        job_id = download_queue.get()
        try:
            run_job(job_id)
        finally:
            download_queue.task_done()


restored_queue_ids = load_persisted_jobs()
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


def public_jobs():
    with jobs_lock:
        ordered = sorted(jobs.values(), key=lambda item: item["created_at"], reverse=True)
        return [
            {
                "id": job["id"],
                "url": job["url"],
                "status": job["status"],
                "message": job["message"],
                "progress": job["progress"],
                "playlist": job["playlist"],
                "album_mode": job["album_mode"],
                "compilation": job["compilation"],
                "force": job["force"],
                "current_item": job["current_item"],
                "total_items": job["total_items"],
                "completed_items": job["completed_items"],
                "files": list(job["files"]),
                "log": list(job["log"]),
                "created_at": job["created_at"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
                "returncode": job["returncode"],
                "http_403": job["http_403"],
                "retry_of": job["retry_of"],
                "attempt": job["attempt"],
                "source": job.get("source", "yt-dlp"),
                "failed_items": job.get("failed_items", 0),
                "missing_tracks": [dict(item) for item in job.get("missing_tracks", [])],
                "resolution_of": job.get("resolution_of"),
                "spotify_track_url": job.get("spotify_track_url"),
                "manual_source_url": job.get("manual_source_url"),
            }
            for job in ordered
        ]


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
    playlist_enabled = request.form.get("playlist") == "on" if source == "yt-dlp" else False
    album_mode = source == "yt-dlp" and playlist_enabled and is_youtube_album_playlist(url)
    probe_result = get_cached_probe_result(url) if album_mode else None
    if album_mode and probe_result is None:
        try:
            probe_result = probe_url(url)
        except (subprocess.TimeoutExpired, RuntimeError):
            probe_result = None

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
    }

    with jobs_lock:
        jobs[job_id] = job
        if len(jobs) > MAX_HISTORY:
            removable = sorted(
                (item for item in jobs.values() if item["status"] in {"succeeded", "failed"}),
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


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id):
    with jobs_lock:
        previous = jobs.get(job_id)
        if previous is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        if previous["status"] != "failed":
            return jsonify({"ok": False, "error": "Only failed jobs can be retried."}), 409
        if any(job.get("retry_of") == job_id for job in jobs.values()):
            return jsonify({
                "ok": False,
                "error": "This attempt has already been retried. Retry the latest failed attempt instead.",
            }), 409

        retry_id = uuid.uuid4().hex[:10]
        completed = min(previous["completed_items"], previous["total_items"]) if previous["total_items"] else previous["completed_items"]
        retry = {
            "id": retry_id,
            "url": previous["url"],
            "playlist": previous["playlist"],
            "album_mode": previous["album_mode"],
            "compilation": previous["compilation"],
            # A retry is a continuation: never overwrite files that already succeeded.
            "force": False,
            "status": "queued",
            "message": (
                f"Continuing after {completed}/{previous['total_items']} tracks"
                if previous["total_items"]
                else "Retry queued"
            ),
            "progress": "",
            "current_item": completed,
            "total_items": previous["total_items"],
            "completed_items": completed,
            "files": list(previous["files"]),
            "log": [f"Retrying failed job {job_id}; existing files and archive entries will be skipped."],
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "http_403": False,
            "retry_of": job_id,
            "attempt": previous.get("attempt", 1) + 1,
            "source": previous.get("source", "yt-dlp"),
            "retry_base_completed": completed,
            "failed_items": 0,
            "missing_tracks": [],
            "resolution_of": None,
            "spotify_track_url": None,
            "manual_source_url": None,
            "download_query": None,
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

    return jsonify({"ok": True, "job_id": retry_id}), 202


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
        if parent.get("source") != "spotify" or parent["status"] != "failed":
            return jsonify({"ok": False, "error": "Only failed Spotify jobs can resolve missing tracks."}), 409

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
            "missing_tracks": [],
            "resolution_of": job_id,
            "spotify_track_url": spotify_url,
            "manual_source_url": source_url,
            "download_query": f"{source_url}|{spotify_url}",
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


@app.post("/api/probe")
def api_probe():
    payload = request.get_json(silent=True) or request.form
    url, error = validate_url(payload.get("url"))
    if error:
        return jsonify({"ok": False, "error": error}), 400

    if is_spotify_url(url):
        return jsonify({
            "ok": True,
            "kind": "spotify",
            "title": f"Spotify {spotify_link_type(url)}",
            "count": None,
            "extractor": "spotDL",
            "album_mode": False,
            "album_artist": None,
        })

    try:
        result = probe_url(url)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Link inspection timed out."}), 504
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    return jsonify({"ok": True, **result})


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
