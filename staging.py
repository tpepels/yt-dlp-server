import json
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter
from pathlib import Path

MEDIA_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mka", ".mp3", ".mp4",
    ".ogg", ".opus", ".wav", ".webm",
}
UNKNOWN_VALUES = {
    "", "unknown", "unknown album", "[unknown album]",
    "unknown artist", "[unknown artist]", "untitled",
}
SOURCE_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{6,})\](?:\.[^.]+)?$")


def safe_component(value):
    value = str(value or "").strip()
    value = value.replace("/", "／").replace("\\", "＼")
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:160]


def clean_album_name(value):
    value = safe_component(value)
    if value.lower() in UNKNOWN_VALUES:
        return ""
    value = re.sub(r"^album\s*[-–—:]\s*", "", value, flags=re.IGNORECASE).strip()
    if value.lower() in UNKNOWN_VALUES:
        return ""
    return value


def clean_artist_name(value):
    value = safe_component(value)
    value = re.sub(r"\s+-\s+Topic$", "", value, flags=re.IGNORECASE).strip()
    if value.lower() in UNKNOWN_VALUES:
        return ""
    return value


def staging_owner_id(job):
    return str(job.get("staging_owner_id") or job.get("id"))


def stage_dir(temp_dir, job):
    return Path(temp_dir) / "staging" / staging_owner_id(job)


def ensure_stage_dir(temp_dir, job):
    path = stage_dir(temp_dir, job)
    path.mkdir(parents=True, exist_ok=True)
    (path / ".tmp").mkdir(parents=True, exist_ok=True)
    return path


def staged_ytdlp_output_template():
    return (
        "%(playlist_index,track_number&{:02d} - |)s"
        "%(track,title).180S [%(id)s].%(ext)s"
    )


def staged_spotdl_output_template(temp_dir, job):
    root = ensure_stage_dir(temp_dir, job)
    return str(
        root
        / "{album-artist}"
        / "{album}"
        / "{track-number} - {title} [{track-id}].{output-ext}"
    )


def media_files(root):
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and ".tmp" not in path.parts
    )


def load_info_jsons(root):
    infos = []
    for path in Path(root).rglob("*.info.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data["_sidecar_path"] = str(path)
            infos.append(data)
    return infos


def source_id_from_filename(path):
    match = SOURCE_ID_RE.search(Path(path).name)
    return match.group(1) if match else None


def title_artist(value):
    value = str(value or "").strip()
    for separator in (" - ", " – ", " — "):
        if separator not in value:
            continue
        prefix, _ = value.split(separator, 1)
        prefix = clean_artist_name(prefix)
        if prefix and len(prefix) <= 80:
            return prefix
    return ""


def trustworthy_track_artist(info):
    for key in ("artist", "album_artist"):
        value = clean_artist_name(info.get(key))
        if value:
            return value

    for key in ("uploader", "channel"):
        raw = str(info.get(key) or "").strip()
        if re.search(r"\s+-\s+Topic$", raw, flags=re.IGNORECASE):
            value = clean_artist_name(raw)
            if value:
                return value

    return title_artist(info.get("track") or info.get("title"))


def unique_values(values):
    values = [value for value in values if value]
    return list(dict.fromkeys(values))


def most_common(values):
    values = [value for value in values if value]
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


def analyze_ytdlp(stage, job):
    infos = load_info_jsons(stage)
    files = media_files(stage)
    info_by_id = {
        str(info.get("id")): info
        for info in infos
        if info.get("id")
    }

    playlist_titles = [
        clean_album_name(
            info.get("playlist_title")
            or info.get("playlist")
            or info.get("playlist_name")
        )
        for info in infos
    ]
    explicit_albums = [clean_album_name(info.get("album")) for info in infos]

    if job.get("playlist"):
        album = (
            clean_album_name(job.get("source_title"))
            or most_common(playlist_titles)
            or most_common(explicit_albums)
        )
    else:
        album = most_common(explicit_albums)

    explicit_album_artists = unique_values(
        clean_artist_name(info.get("album_artist")) for info in infos
    )
    per_track_artists = [
        trustworthy_track_artist(info)
        for info in infos
    ]
    unique_track_artists = unique_values(per_track_artists)

    hinted_artist = clean_artist_name(job.get("source_album_artist"))
    if hinted_artist and hinted_artist != "Various Artists":
        album_artist = hinted_artist
    elif len(explicit_album_artists) == 1:
        album_artist = explicit_album_artists[0]
    elif len(unique_track_artists) == 1:
        album_artist = unique_track_artists[0]
    elif len(unique_track_artists) > 1:
        album_artist = "Various Artists"
    else:
        album_artist = ""

    records = []
    for path in files:
        source_id = source_id_from_filename(path)
        info = info_by_id.get(source_id, {})
        artist = trustworthy_track_artist(info)
        if not artist and album_artist and album_artist != "Various Artists":
            artist = album_artist
        records.append({
            "path": str(path),
            "source_id": source_id,
            "artist": artist,
        })

    return {
        "artist": album_artist,
        "album": album,
        "records": records,
        "info_count": len(infos),
        "media_count": len(files),
    }


def analyze_spotdl(stage, job):
    files = media_files(stage)
    artists = []
    albums = []
    records = []

    root = Path(stage)
    for path in files:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        parts = rel.parts
        artist = clean_artist_name(parts[0]) if len(parts) >= 3 else ""
        album = clean_album_name(parts[1]) if len(parts) >= 3 else ""
        if artist:
            artists.append(artist)
        if album:
            albums.append(album)
        records.append({
            "path": str(path),
            "source_id": source_id_from_filename(path),
            "artist": "",
        })

    unique_artists = unique_values(artists)
    unique_albums = unique_values(albums)
    album_artist = unique_artists[0] if len(unique_artists) == 1 else ""
    album = unique_albums[0] if len(unique_albums) == 1 else ""

    return {
        "artist": album_artist,
        "album": album,
        "records": records,
        "info_count": 0,
        "media_count": len(files),
    }


def analyze_stage(temp_dir, job):
    stage = stage_dir(temp_dir, job)
    if job.get("source") == "spotify":
        result = analyze_spotdl(stage, job)
    else:
        result = analyze_ytdlp(stage, job)
    result["stage_dir"] = str(stage)
    result["needs_metadata"] = not bool(result["artist"] and result["album"])
    return result


def retag_media(path, album, album_artist, track_artist=""):
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.retag-{uuid.uuid4().hex[:8]}{path.suffix}")
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(path),
        "-map", "0",
        "-c", "copy",
        "-map_metadata", "0",
        "-metadata", f"album={album}",
        "-metadata", f"album_artist={album_artist}",
        "-metadata", f"albumartist={album_artist}",
    ]
    if track_artist:
        cmd.extend(["-metadata", f"artist={track_artist}"])
    cmd.append(str(tmp))

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"ffmpeg metadata update failed for {path.name}: "
                f"{detail or completed.returncode}"
            )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def publish_stage(temp_dir, import_root, job, artist=None, album=None):
    analysis = analyze_stage(temp_dir, job)
    artist = clean_artist_name(artist or analysis["artist"])
    album = clean_album_name(album or analysis["album"])

    if not analysis["records"]:
        return {
            "status": "error",
            "message": "No staged audio files were found.",
            **analysis,
        }

    if not artist or not album:
        return {
            "status": "needs_metadata",
            "message": "Artist or album metadata is unresolved.",
            "artist": artist,
            "album": album,
            **analysis,
        }

    for record in analysis["records"]:
        track_artist = clean_artist_name(record.get("artist"))
        retag_media(
            record["path"],
            album=album,
            album_artist=artist,
            track_artist=track_artist,
        )

    import_root = Path(import_root)
    import_root.mkdir(parents=True, exist_ok=True)
    incoming_parent = import_root / ".incoming"
    incoming_parent.mkdir(parents=True, exist_ok=True)
    incoming = incoming_parent / f"{staging_owner_id(job)}-{uuid.uuid4().hex[:8]}"
    incoming.mkdir(parents=True, exist_ok=False)

    copied = []
    for record in analysis["records"]:
        source = Path(record["path"])
        destination = incoming / source.name
        shutil.copy2(source, destination)
        copied.append(destination)

    final_dir = import_root / safe_component(artist) / safe_component(album)
    final_dir.parent.mkdir(parents=True, exist_ok=True)

    final_files = []
    if not final_dir.exists():
        os.replace(incoming, final_dir)
        final_files = [str(final_dir / path.name) for path in copied]
    else:
        final_dir.mkdir(parents=True, exist_ok=True)
        for path in list(incoming.iterdir()):
            destination = final_dir / path.name
            if destination.exists():
                if job.get("force"):
                    destination.unlink()
                else:
                    path.unlink()
                    final_files.append(str(destination))
                    continue
            os.replace(path, destination)
            final_files.append(str(destination))
        incoming.rmdir()

    stage = stage_dir(temp_dir, job)
    shutil.rmtree(stage, ignore_errors=True)

    try:
        if incoming_parent.is_dir() and not any(incoming_parent.iterdir()):
            incoming_parent.rmdir()
    except OSError:
        pass

    return {
        "status": "published",
        "message": f"Published {len(final_files)} file(s) to {artist}/{album}",
        "artist": artist,
        "album": album,
        "final_dir": str(final_dir),
        "files": final_files,
        "media_count": len(final_files),
        "stage_dir": str(stage),
        "needs_metadata": False,
    }
