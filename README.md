# yt-dlp music server

Small LAN web interface around [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a media URL, queue an audio-only download, and write the result into a Plex-friendly music tree.

## What it does

- Web UI on port **4545**
- One download worker
- Audio only
- Keeps the best source audio format instead of transcoding everything to MP3
- Embeds source metadata and cover art when available
- Uses yt-dlp's music metadata fields first
- Keeps all imports isolated under `YT-DLP Imports/`
- Uses a persistent yt-dlp download archive to avoid accidental duplicates
- Supports playlists/albums explicitly via a checkbox
- Includes ffmpeg, Deno, yt-dlp EJS support and curl-cffi in the image
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
      user: "\${UID}:\${GID}"
      ports:
        - "4545:4545"
      environment:
        TZ: \${TZ}
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
    sudo chown -R "$UID:$GID" /srv/yt-dlp-server

Then:

    docker compose build yt-dlp-server
    docker compose up -d yt-dlp-server

Open `http://YOUR-SERVER-IP:4545`.

## Playlists

Playlist downloading is off by default. This prevents a YouTube track URL containing a `list=` parameter from unexpectedly pulling the whole playlist.

Tick **Download playlist / album** when you want the collection.

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

## Security

This interface has no login and is intended for a trusted LAN. Do not expose port 4545 directly to the public internet.
