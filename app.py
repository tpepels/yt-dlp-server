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

if not IMPORT_SUBDIR or Path(IMPORT_SUBDIR).is_absolute() or ".." in Path(IMPORT_SUBDIR).parts:
    raise RuntimeError("IMPORT_SUBDIR must be a safe relative path")

IMPORT_ROOT = MUSIC_ROOT / IMPORT_SUBDIR
ARCHIVE_FILE = STATE_DIR / "archive.txt"

for directory in (IMPORT_ROOT, STATE_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

OUTPUT_TEMPLATE = (
    "%(album_artist,artist,creator,uploader|Unknown Artist).120S/"
    "%(album,playlist|Singles).160S/"
    "%(track_number,playlist_index&{:02d} - |)s"
    "%(track,title).180S [%(id)s].%(ext)s"
)

jobs = {}
jobs_lock = threading.Lock()
download_queue = queue.Queue(maxsize=MAX_QUEUE)
_progress_re = re.compile(r"^\[download\]\s+(.+?)(?:\s+of\s+|\s+at\s+|\s+ETA\s+|$)")
_playlist_item_re = re.compile(r"^\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)")


def tool_version(command):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (completed.stdout or completed.stderr).strip().splitlines()[0]
    except Exception:
        return "unavailable"


VERSIONS = {
    "yt_dlp": tool_version(["yt-dlp", "--version"]),
    "ffmpeg": tool_version(["ffmpeg", "-version"]),
    "deno": tool_version(["deno", "--version"]),
}


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


def is_youtube_album_playlist(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    hostname = (parsed.hostname or "").lower()
    if hostname not in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"}:
        return False

    playlist_id = parse_qs(parsed.query).get("list", [""])[0]
    return playlist_id.startswith("OLAK5uy_")


def album_playlist_metadata_args():
    return [
        "--parse-metadata",
        "%(playlist_channel,playlist_uploader,album_artist,artist|)s:%(album_artist)s",
        "--replace-in-metadata",
        "album_artist",
        r"\s+- Topic$",
        "",
        "--parse-metadata",
        "%(playlist_title,album,playlist|)s:%(album)s",
    ]


def cookie_args():
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        return ["--cookies", COOKIES_FILE]
    return []


def build_probe_command(url):
    return [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--yes-playlist",
        "--playlist-end",
        "1",
        *cookie_args(),
        url,
    ]


def classify_probe_info(info):
    entries = info.get("entries")
    kind = (
        "playlist"
        if info.get("_type") in {"playlist", "multi_video"} or isinstance(entries, list)
        else "single"
    )

    count = info.get("playlist_count")
    if not isinstance(count, int) or count < 1:
        count = None

    return {
        "kind": kind,
        "title": info.get("title") or info.get("fulltitle") or "Untitled",
        "count": count,
        "extractor": info.get("extractor_key") or info.get("extractor"),
    }


def probe_url(url):
    completed = subprocess.run(
        build_probe_command(url),
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

    return classify_probe_info(info)


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
            cmd.extend(album_playlist_metadata_args())
    else:
        cmd.append("--no-playlist")

    if job["force"]:
        cmd.append("--force-overwrites")
    else:
        cmd.extend(["--no-overwrites", "--download-archive", str(ARCHIVE_FILE)])

    cmd.extend(cookie_args())
    cmd.append(job["url"])
    return cmd


def append_log(job_id, line):
    line = line.rstrip()
    if not line:
        return

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
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
            job["files"].append(filepath)
            job["completed_items"] += 1
            job["message"] = f"Saved {Path(filepath).name}"
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
        job["message"] = "Starting yt-dlp"

    cmd = build_command(job)

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

        with jobs_lock:
            job = jobs[job_id]
            job["returncode"] = returncode
            job["finished_at"] = time.time()
            if returncode == 0:
                job["status"] = "succeeded"
                if job["files"]:
                    count = len(job["files"])
                    job["message"] = f"Completed - {count} file{'s' if count != 1 else ''}"
                else:
                    job["message"] = "Completed - nothing new to download"
            else:
                job["status"] = "failed"
                job["message"] = f"yt-dlp exited with code {returncode}"
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job["status"] = "failed"
            job["finished_at"] = time.time()
            job["message"] = str(exc)
            job["log"].append(f"ERROR: {exc}")


def worker():
    while True:
        job_id = download_queue.get()
        try:
            run_job(job_id)
        finally:
            download_queue.task_done()


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

    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id,
        "url": url,
        "playlist": request.form.get("playlist") == "on",
        "album_mode": request.form.get("playlist") == "on" and is_youtube_album_playlist(url),
        "force": request.form.get("force") == "on",
        "status": "queued",
        "message": "Waiting for worker",
        "progress": "",
        "current_item": 0,
        "total_items": 0,
        "completed_items": 0,
        "files": [],
        "log": [],
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "returncode": None,
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

    try:
        download_queue.put_nowait(job_id)
    except queue.Full:
        with jobs_lock:
            jobs.pop(job_id, None)
        return render_template(
            "index.html",
            jobs=public_jobs(),
            import_root=str(IMPORT_ROOT),
            versions=VERSIONS,
            error="Download queue is full.",
        ), 429

    return redirect(url_for("index"))


@app.post("/api/probe")
def api_probe():
    payload = request.get_json(silent=True) or request.form
    url, error = validate_url(payload.get("url"))
    if error:
        return jsonify({"ok": False, "error": error}), 400

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
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
