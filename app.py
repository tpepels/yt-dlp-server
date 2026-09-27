import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

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
        "after_move:__YTDLP_FILE__=%(filepath)s",
    ]

    if job["playlist"]:
        cmd.append("--yes-playlist")
    else:
        cmd.append("--no-playlist")

    if job["force"]:
        cmd.append("--force-overwrites")
    else:
        cmd.extend(["--no-overwrites", "--download-archive", str(ARCHIVE_FILE)])

    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        cmd.extend(["--cookies", COOKIES_FILE])

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

        if line.startswith("__YTDLP_FILE__="):
            filepath = line.split("=", 1)[1]
            job["files"].append(filepath)
            job["message"] = f"Saved {Path(filepath).name}"
            return

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
                "force": job["force"],
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
        "force": request.form.get("force") == "on",
        "status": "queued",
        "message": "Waiting for worker",
        "progress": "",
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
