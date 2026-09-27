# yt-dlp music server

Small LAN web interface around [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [spotDL](https://github.com/spotDL/spotify-downloader). Paste a media URL, queue an audio-only download, and write the result into a Plex-friendly music tree. Spotify URLs use Spotify metadata while spotDL matches audio from YouTube Music, YouTube, Bandcamp, then SoundCloud.

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
- Uses yt-dlp's music metadata fields first
- Keeps all imports isolated under `YT-DLP Imports/`
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

The extension follows the best source audio. If the extractor does not provide music metadata, the server does not invent it. It falls back to:

    YT-DLP Imports/
      Uploader/
        Singles/
          Video title [source-id].ext

That fallback is deliberate: generic uploads cannot scatter incorrectly tagged files through the rest of the collection.

The source ID is kept in the filename to prevent collisions. Embedded tags remain based on the extractor metadata.

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
| `TEMP_DIR` | `/data/tmp` | Temporary files |
| `COOKIES_FILE` | empty | Optional Netscape-format cookies file |
| `MAX_QUEUE` | `50` | Maximum waiting jobs |
| `MAX_HISTORY` | `50` | In-memory UI history |
| `PROBE_TIMEOUT` | `30` | Maximum link-inspection time in seconds |
| `BGUTIL_SERVER_HOME` | `/opt/bgutil-ytdlp-pot-provider/server` | Bundled BgUtils provider scripts |
| `YOUTUBE_PLAYER_CLIENT` | `mweb` | YouTube client used with automatic PO tokens |

## Security

This interface has no login and is intended for a trusted LAN. Do not expose port 4545 directly to the public internet.
