# Chrome extension

## Download packaged ZIP

[Download v1.0.0](https://github.com/tpepels/yt-dlp-server/raw/refs/heads/main/dist/yt-dlp-server-chrome-extension-v1.0.0.zip)

Unzip it before using **Load unpacked** in Chrome.


Manifest V3 Chrome/Chromium extension for sending album URLs directly to the yt-dlp server.

## Install

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select this `chrome-extension` directory.
5. Open the extension settings (click the extension icon, or **Details -> Extension options**).
6. Enter the server address, for example `http://media-server:4545`, then choose **Save and test**.

Chrome asks for access only to the server origin you configure.

## Use

Right-click:

- a link -> **Send to yt-dlp server**
- selected text -> **Send to yt-dlp server**

For selected text the extension sends:

- plain `http://` / `https://` URLs present in the selected text;
- `www.` URLs;
- actual hyperlink targets intersecting the selected range, even when the visible link text is not itself a URL.

Up to 20 unique URLs are sent from one selection.

Each URL is first checked using the server's existing `/api/probe` endpoint. It is still queued if the probe cannot identify it as an album, but the page gets a small non-blocking warning. Downloads are submitted with playlist/album mode enabled.

Retries and final metadata handling remain entirely on the server.
