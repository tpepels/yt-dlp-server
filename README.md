# yt-dlp music server

Small LAN web interface around [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a media URL, inspect whether it resolves to a single item or playlist, queue an audio-only download, and write the result into a Plex-friendly music tree.

## What it does

- Web UI on port **4545**
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
- Uses a persistent yt-dlp download archive to avoid accidental duplicates
- Includes ffmpeg, Deno, yt-dlp EJS support, curl-cffi, and the BgUtils PO-token provider in the image
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

## Duplicate handling

Normal downloads use `/data/state/archive.txt`. yt-dlp records successfully downloaded source IDs there. Submitting the same source again will normally do nothing.

The **Ignore archive and overwrite existing file** checkbox deliberately bypasses that protection.

## Cookies

Some sites may require authentication. Export a Netscape-format `cookies.txt`, place it at:

    /srv/yt-dlp-server/state/cookies.txt

and add:

    COOKIES_FILE: /data/state/cookies.txt

to the service environment. Treat the cookie file as a secret.

## Updating yt-dlp

yt-dlp changes often because upstream sites change. The image intentionally installs the current PyPI yt-dlp release at build time rather than pinning an old extractor release.

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
| `STATE_DIR` | `/data/state` | Persistent archive/state |
| `TEMP_DIR` | `/data/tmp` | Temporary files |
| `COOKIES_FILE` | empty | Optional Netscape-format cookies file |
| `MAX_QUEUE` | `50` | Maximum waiting jobs |
| `MAX_HISTORY` | `50` | In-memory UI history |
| `PROBE_TIMEOUT` | `30` | Maximum link-inspection time in seconds |
| `BGUTIL_SERVER_HOME` | `/opt/bgutil-ytdlp-pot-provider/server` | Bundled BgUtils provider scripts |
| `YOUTUBE_PLAYER_CLIENT` | `mweb` | YouTube client used with automatic PO tokens |

## Security

This interface has no login and is intended for a trusted LAN. Do not expose port 4545 directly to the public internet.
