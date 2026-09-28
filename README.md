# yt-dlp music server

Small LAN web interface around [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [spotDL](https://github.com/spotDL/spotify-downloader). Paste a media URL, queue an audio-only download, stage it outside the Plex library, validate collection metadata, and only then publish it into a Plex-friendly music tree. Spotify URLs use Spotify metadata while spotDL matches audio from YouTube Music, YouTube, Bandcamp, then SoundCloud.

## What it does

- Web UI on port **4545**
- Spotify tracks, albums, playlists and artists via spotDL 4.5.2
- Automatic Spotify audio fallback: YouTube Music -> YouTube -> Bandcamp -> SoundCloud
- Manual missing-track resolver using a direct YouTube, Bandcamp or SoundCloud URL while preserving Spotify metadata
- Automatically probes pasted links before download
- Shows whether yt-dlp sees a single item or playlist/album, including item count when available
- Detects YouTube auto-generated album playlists and keeps compilation albums together with a shared Album Artist
- Shows per-track playlist progress and keeps expanded logs open while the UI refreshes
- Playlist/album downloading enabled by default
- One download worker
- Audio only
- Keeps the best source audio format instead of transcoding everything to MP3
- Embeds source metadata and cover art when available
- Downloads every job into persistent staging under `/data/tmp/staging/` first
- Publishes into the Plex library only after the complete job and metadata validation succeed
- Uses collection-level metadata for albums/playlists instead of per-video uploader metadata
- Never promotes arbitrary YouTube uploader/channel names to album artists
- Stops in **Needs metadata** when artist or album cannot be derived, with editable fields in the UI
- Retags the completed staged files with the validated album/album-artist metadata before publishing
- Keeps all final imports isolated under `YT-DLP Imports/`
- Uses persistent yt-dlp and spotDL archives to avoid accidental duplicates and support retry/continue
- Includes ffmpeg, Deno, yt-dlp EJS support, curl-cffi, spotDL, and the BgUtils PO-token provider in the image
- Optional `cookies.txt` support for sources that require login

## Library layout

Default output:

    /data/music/
      YT-DLP Imports/
        Artist/
          Album/
            01 - Track [source-id].opus

The extension follows the best source audio. The source ID is kept in the filename to prevent collisions.

Downloads are **not** written directly to that tree. They first live under:

    /data/tmp/staging/<job-id>/

For YouTube/YouTube Music the server writes yt-dlp info JSON sidecars into staging, then evaluates the whole collection together. For playlists, one collection-level album name is selected (normally the playlist/album title), and arbitrary uploaders such as the person who happened to upload one video are never treated as album artists.

If both artist/album-artist and album are trustworthy, the server retags the staged audio and publishes the complete collection. If either is unresolved, the job becomes **Needs metadata** and remains outside Plex until the user confirms the fields.

## Add it to the existing media-server compose

Assuming this repository is cloned as `./yt-dlp-server` next to the compose file:

    yt-dlp-server:
      build:
        context: ./yt-dlp-server
      container_name: yt-dlp-server
      user: "${UID:-1000}:${GID:-1000}"
      ports:
        - "4545:4545"
      environment:
        TZ: ${TZ}
        PORT: 4545
        MUSIC_ROOT: /data/music
        IMPORT_SUBDIR: YT-DLP Imports
        STATE_DIR: /data/state
        TEMP_DIR: /data/tmp
      volumes:
        - /srv/completed/music:/data/music
        - /srv/yt-dlp-server/state:/data/state
        - /srv/yt-dlp-server/tmp:/data/tmp
      restart: unless-stopped

Prepare writable state directories once:

    sudo mkdir -p /srv/yt-dlp-server/state /srv/yt-dlp-server/tmp
    sudo chown -R "$(id -u):$(id -g)" /srv/yt-dlp-server

Then:

    docker compose build yt-dlp-server
    docker compose up -d yt-dlp-server

Open `http://YOUR-SERVER-IP:4545`.


## Chrome extension

The running web app also exposes the same ZIP directly from the page through the **Chrome extension** button. The download is served by the yt-dlp server itself at `/chrome-extension.zip`, so users do not need GitHub access once the server is running.

A ready-to-share package is available directly from the repository:

[Download Chrome extension v1.0.0](https://github.com/tpepels/yt-dlp-server/raw/refs/heads/main/dist/yt-dlp-server-chrome-extension-v1.0.0.zip)

Recipients only need to unzip it, open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**, and select the unzipped folder.

The repository includes a Manifest V3 Chrome/Chromium extension in `chrome-extension/`.

It adds **Send to yt-dlp server** to the right-click menu for:

- links;
- selected text containing one or more URLs;
- selected hyperlink text, even when the visible text is not itself a URL.

Selected text may contain up to 20 unique URLs. Each URL is first passed through the server's normal preflight probe, then queued with playlist/album mode enabled. Non-album links are still accepted, but the extension shows the same kind of small advisory warning rather than blocking them.

Install it locally:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select the `chrome-extension` directory.
5. Click the extension icon (or open **Details -> Extension options**).
6. Enter the server URL, for example `http://media-server:4545`, and choose **Save and test**.

The extension does not request blanket host access at install time. It asks Chrome for permission only to the configured server origin. Page access for reading a right-click selection uses `activeTab`, which is temporary and tied to the user's context-menu action.

## Staged imports and metadata gate

Direct-to-library downloads are deliberately not supported. A job now has two phases:

1. **Download/stage** - media, metadata sidecars and partial retries remain under `/data/tmp/staging/<collection-id>/`, which is on the persistent `/srv/yt-dlp-server/tmp` host mount.
2. **Validate/publish** - after the job is complete, the server derives one album/collection name and one album artist for the collection, retags the staged audio, copies it into a hidden `.incoming` directory inside `YT-DLP Imports`, then publishes it into `Artist/Album/`. Only after that succeeds is the external staging directory removed. Tag updates use mutagen in place where supported so Opus/Vorbis cover metadata is preserved instead of remuxing every embedded stream through ffmpeg.

The metadata rules are intentionally conservative:

- `album_artist` and `artist` music fields are trusted;
- a YouTube channel ending in ` - Topic` may be used as an artist;
- arbitrary uploaders/channels are **never** used as artists;
- for a playlist, the collection title is preferred over individual videos' album fields, preventing a single playlist from fragmenting into `[Unknown Album]`, `Album - X`, and uploader-specific pseudo-albums;
- multiple legitimate track artists become `Various Artists` at album level while track artists are retained where available;
- exact placeholders such as `[Unknown Album]`, `Unknown Album`, `Unknown Artist`, and `Untitled` are rejected rather than published.

When metadata cannot be resolved, the job shows **Needs metadata** with pre-filled **Artist / album artist** and **Album** fields. Editing those fields pauses automatic card refresh so the form is not overwritten while typing. **Move to library** performs the same retag + publish pipeline using the confirmed values.

## Spotify / spotDL

Spotify URLs are routed through **spotDL 4.5.2** automatically. No separate page or service is required.

For public Spotify track, album, playlist and artist URLs, spotDL retrieves the Spotify metadata, searches YouTube Music first, then YouTube, Bandcamp and SoundCloud as fallbacks, downloads audio only, and embeds the Spotify metadata and cover art. spotDL does **not** download the audio stream from Spotify itself.

Spotify output uses:

    YT-DLP Imports/
      Album Artist/
        Album/
          01 - Track [spotify-track-id].opus

The downloader uses:

- OPUS output;
- `--bitrate disable` to avoid an unnecessary bitrate conversion where possible;
- one spotDL download thread so the server's queue/progress model remains deterministic;
- Spotify album artist, album, track number, title, artwork and other embedded metadata;
- a persistent archive at `/data/state/spotdl-archive.txt`;
- the existing YouTube BgUtils PO-token configuration through spotDL's `--yt-dlp-args` passthrough;
- YouTube Music as the first audio match provider, then YouTube, Bandcamp and SoundCloud as fallbacks;
- no lyrics providers.

The normal **Ignore archive and overwrite existing file** option also applies to Spotify. When unchecked, completed Spotify tracks are recorded in the spotDL archive. A failed partial job can therefore use **Retry automatic search** and only the missing tracks are attempted again.

When every automatic provider fails for a specific Spotify track, the failed job exposes that track separately. Paste a direct YouTube watch URL, Bandcamp track URL or SoundCloud track URL into **Resolve track**. The server sends spotDL its supported manual mapping form, `SOURCE_URL|SPOTIFY_TRACK_URL`, so the chosen source supplies the audio while the original Spotify track still supplies the metadata and cover art. A successful manual resolution is added to the spotDL archive and upgrades the original partial album job to completed once all missing tracks are resolved.

spotDL's own processed counter includes failed tracks, so the web UI does not call that value "saved." Saved-track progress is counted separately from successful downloads/existing files, while spotDL's counter is only used to establish the collection size and processing position.

Public Spotify URLs do not require Spotify developer credentials with spotDL's current default metadata client. Authenticated Spotify library shortcuts are outside this web UI's URL-based workflow.

## Playlist preflight

When a valid URL is pasted, the browser asks the server to inspect it before download.

The probe uses yt-dlp in simulation mode with a flat playlist and only the first playlist entry. It does not download media. The result is shown as either:

- `Single item detected - ...`
- `Playlist / album detected - ... - N items`

Playlist/album mode is checked by default. If a URL points to a video that is also part of a playlist, yt-dlp is instructed to use the playlist. Uncheck the option if you only want that individual item.

For YouTube's auto-generated album playlists (`OLAK5uy_...`), the preflight inspects the flat track list. Multiple distinct track artists/channels are treated as a compilation and written with `Album Artist = Various Artists`, while the individual track `Artist` tags are preserved. The playlist title is used consistently as the album title. This prevents Plex from splitting one compilation into separate albums per performer.

A failed probe is advisory: the actual download remains available because some extractors may fail lightweight inspection while still working normally.


## YouTube PO tokens and HTTP 403

YouTube increasingly requires Proof-of-Origin (PO) tokens for some media requests. Without the required token, a video or track can resolve normally and still fail when yt-dlp fetches the actual media with `HTTP Error 403: Forbidden`.

This image bundles `bgutil-ytdlp-pot-provider` **2.0.0** plus its matching BgUtils server scripts. For YouTube URLs only, the app:

- uses the yt-dlp `mweb` player client;
- asks the BgUtils script provider to generate per-video PO tokens automatically;
- runs that provider locally with the already-bundled Deno runtime;
- stores temporary token/cache data under the writable `XDG_CACHE_HOME`;
- leaves non-YouTube extractors unchanged.

The app uses script mode rather than exposing the BgUtils HTTP service. This server has one download worker, so the script-mode concurrency tradeoff is acceptable and there is no additional LAN-facing token-provider port.

The provider path and player client can be overridden if needed:

    BGUTIL_SERVER_HOME=/opt/bgutil-ytdlp-pot-provider/server
    YOUTUBE_PLAYER_CLIENT=mweb

The `/health` endpoint reports whether the provider directory is available and which YouTube client is configured.

A PO token improves compatibility with current YouTube enforcement but cannot guarantee that every YouTube media request will succeed. If YouTube still returns a 403, the job now reports that it failed despite PO-token support instead of showing only a generic yt-dlp exit code.

## Unavailable YouTube playlist tracks

YouTube playlists can contain hidden or removed videos. yt-dlp normally continues downloading the rest of the playlist, but still exits nonzero when an unavailable video is encountered. The server detects errors such as:

    ERROR: [youtube] riu2Bx6miIU: Video unavailable

and exposes the unavailable video as a missing track on the failed job.

The job card offers **Ignore permanently**. Ignored YouTube video IDs are stored at:

    /data/state/ignored-tracks.json

Ignoring does not fabricate or count a file as downloaded. A 13/14 playlist therefore remains 13/14 tracks saved, but the job is closed successfully with `1 ignored`. Future runs of the same playlist may still make yt-dlp encounter the unavailable entry, but the server recognizes that all remaining errors are explicitly ignored and does not leave the job failed again.

Retry remains available until the missing track is ignored, so temporary availability failures can still be retried instead.

## Repairing imports created by older versions

Versions before the staging pipeline may already have created uploader folders, duplicate pseudo-albums, or `[Unknown Album]` entries. The image includes a one-time repair tool that uses the persisted job URLs and source IDs to rebuild those imports.

First run a **dry run**:

    docker compose exec yt-dlp-server \
      python /app/tools/repair_legacy_imports.py

It probes the original YouTube/YouTube Music URLs again, groups the files by the original collection, prints the intended `Artist/Album` destination, and reports anything it cannot resolve. Album repair deliberately performs a **full per-track metadata probe** rather than yt-dlp's flat-playlist probe: this is slower, but it recovers `album_artist`, `artist`, and stable `Artist - Topic` channel metadata that flat playlist data often omits. Successful full probes are cached under `/data/state/repair-metadata-cache/`, so the later `--apply` reuses the exact metadata reviewed in the dry run instead of querying every track again. Add `--refresh-metadata` if the source has changed and you intentionally want to rebuild that cache. It does not modify music files without `--apply`.

After reviewing the plan:

    docker compose exec yt-dlp-server \
      python /app/tools/repair_legacy_imports.py --apply

For each collection, the repair tool first copies and retags all replacement files into a hidden incoming directory. Only after the whole replacement collection is prepared successfully does it publish the corrected album and remove the old scattered copies. Arbitrary uploader names are never used as artists here either.

To repair only one exact persisted source URL, add:

    --url 'https://music.youtube.com/playlist?list=...'

Legacy standalone YouTube/YouTube Music watch URLs are **skipped by default**. The repair command is album-focused and will not turn an old one-track download into a one-file album directory. Add `--include-singles` only if you explicitly want those old single-video jobs considered too.

Artist inference is conservative: a value must be supported by a majority of the fully inspected tracks. Explicit `album_artist` wins, followed by a stable `Artist - Topic` channel, followed by one consistent per-track artist. Playlist-level generic values are never trusted as the artist.

## Removing finished or abandoned jobs

Terminal job cards can be removed from the UI:

- **Discard** on failed or metadata-waiting jobs removes the job and cleans its unpublished staging directory when no retry/resolution still shares that staging area.
- **Remove from list** on succeeded jobs removes only the history card. Published music files are never deleted.
- Queued/running jobs cannot be removed through this action while work is active.

This is useful for album-level failures that are not worth resolving track-by-track, such as a Spotify album where every spotDL match failed.

## Persistent queue and restart recovery

Job history and queued work are persisted to:

    /data/state/jobs.json

The state file is updated atomically when jobs are created, retried, manually resolved, started, completed, fail, or save another track. Because `/data/state` is a persistent volume, queued jobs and the visible job history survive container rebuilds/restarts.

Restart behavior is deliberately conservative:

- jobs that were **queued** are automatically requeued after restart;
- jobs that were **running** are restored as failed with `Interrupted by server restart - use Retry / continue`;
- completed and failed job history remains visible;
- an interrupted job is not silently restarted, which avoids accidentally repeating a forced/overwrite download.

### Migrating the current in-memory queue

Versions before persistent queue support only keep jobs in memory. Before the first upgrade to a version with queue persistence, save the current `/api/jobs` response directly into the persistent state directory:

    curl -fsS http://127.0.0.1:4545/api/jobs \
      -o /srv/yt-dlp-server/state/jobs.json

The new server accepts this older JSON-array format on first startup, normalizes it into the persistent state format, restores queued jobs, and preserves the rest of the visible history.

After this migration, no manual queue export is required for future updates.

## Duplicate handling

Normal yt-dlp downloads use `/data/state/archive.txt`. Spotify/spotDL downloads use `/data/state/spotdl-archive.txt`. Each archive records successfully completed source items so retries and repeated submissions can skip tracks that are already complete.

The **Ignore archive and overwrite existing file** checkbox deliberately bypasses that protection.

## Cookies

Some sites may require authentication. Export a Netscape-format `cookies.txt`, place it at:

    /srv/yt-dlp-server/state/cookies.txt

and add:

    COOKIES_FILE: /data/state/cookies.txt

to the service environment. Treat the cookie file as a secret.

## Updating yt-dlp and spotDL

yt-dlp changes often because upstream sites change. The image intentionally installs the current PyPI yt-dlp release at build time rather than pinning an old extractor release. spotDL is pinned to the tested 4.5.2 release so its CLI and progress behavior remain stable for this app.

Rebuild when downloads start failing:

    git pull
    docker compose build --pull --no-cache yt-dlp-server
    docker compose up -d yt-dlp-server

Watchtower does not rebuild locally built images, so it cannot update yt-dlp inside this service by itself.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `4545` | HTTP port inside the container |
| `MUSIC_ROOT` | `/data/music` | Mounted Plex music root |
| `IMPORT_SUBDIR` | `YT-DLP Imports` | Isolated directory inside the music root |
| `STATE_DIR` | `/data/state` | Persistent archives, queue and job history |
| `TEMP_DIR` | `/data/tmp` | Persistent staging and temporary files; keep this outside `MUSIC_ROOT` |
| `COOKIES_FILE` | empty | Optional Netscape-format cookies file |
| `MAX_QUEUE` | `50` | Maximum waiting jobs |
| `MAX_HISTORY` | `50` | In-memory UI history |
| `PROBE_TIMEOUT` | `30` | Maximum link-inspection time in seconds |
| `BGUTIL_SERVER_HOME` | `/opt/bgutil-ytdlp-pot-provider/server` | Bundled BgUtils provider scripts |
| `YOUTUBE_PLAYER_CLIENT` | `mweb` | YouTube client used with automatic PO tokens |

## Security

This interface has no login and is intended for a trusted LAN. Do not expose port 4545 directly to the public internet.
