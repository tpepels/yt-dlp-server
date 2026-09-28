const MENU_ID = "send-to-ytdlp";
const MAX_SELECTION_URLS = 20;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "Send to yt-dlp server",
      contexts: ["link", "selection"],
    });
  });
});

chrome.action.onClicked.addListener(() => {
  chrome.runtime.openOptionsPage();
});

function normalizeServerUrl(value) {
  const raw = String(value || "").trim().replace(/\/+$/, "");
  if (!raw) return "";

  try {
    const parsed = new URL(raw);
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    return parsed.origin + parsed.pathname.replace(/\/+$/, "");
  } catch (_) {
    return "";
  }
}

function permissionPattern(serverUrl) {
  const parsed = new URL(serverUrl);
  return parsed.origin + "/*";
}

function cleanCandidate(value) {
  let candidate = String(value || "").trim();
  candidate = candidate.replace(/^[<("'\[]+/, "");
  candidate = candidate.replace(/[>)"'\],.;:!?]+$/, "");

  if (/^www\./i.test(candidate)) {
    candidate = "https://" + candidate;
  }

  try {
    const parsed = new URL(candidate);
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    return parsed.href;
  } catch (_) {
    return "";
  }
}

function urlsFromText(text) {
  const source = String(text || "");
  const matches = source.match(/(?:https?:\/\/|www\.)[^\s<>"'`]+/gi) || [];
  return matches.map(cleanCandidate).filter(Boolean);
}

function uniqueUrls(values) {
  return [...new Set(values.filter(Boolean))];
}

async function getSettings() {
  const stored = await chrome.storage.sync.get({ serverUrl: "" });
  return {
    serverUrl: normalizeServerUrl(stored.serverUrl),
  };
}

async function hasServerPermission(serverUrl) {
  try {
    return await chrome.permissions.contains({
      origins: [permissionPattern(serverUrl)],
    });
  } catch (_) {
    return false;
  }
}

function collectSelectedPageUrls() {
  const urls = new Set();
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return [];

  const text = selection.toString();
  const textMatches = text.match(/(?:https?:\/\/|www\.)[^\s<>"'`]+/gi) || [];
  for (let value of textMatches) {
    value = value.replace(/^[<("'\[]+/, "").replace(/[>)"'\],.;:!?]+$/, "");
    if (/^www\./i.test(value)) value = "https://" + value;
    try {
      const parsed = new URL(value);
      if (["http:", "https:"].includes(parsed.protocol)) urls.add(parsed.href);
    } catch (_) {}
  }

  const anchors = [...document.querySelectorAll("a[href]")];
  for (let rangeIndex = 0; rangeIndex < selection.rangeCount; rangeIndex += 1) {
    const range = selection.getRangeAt(rangeIndex);
    for (const anchor of anchors) {
      try {
        if (range.intersectsNode(anchor)) {
          const parsed = new URL(anchor.href, document.baseURI);
          if (["http:", "https:"].includes(parsed.protocol)) urls.add(parsed.href);
        }
      } catch (_) {}
    }
  }

  return [...urls];
}

async function selectionUrls(tabId, selectionText) {
  const urls = urlsFromText(selectionText);

  if (Number.isInteger(tabId)) {
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        func: collectSelectedPageUrls,
      });
      for (const result of results || []) {
        if (Array.isArray(result.result)) urls.push(...result.result);
      }
    } catch (_) {
      // Restricted pages (chrome://, Web Store, PDFs) may not permit
      // injection. Plain selected URL text still works through the fallback.
    }
  }

  return uniqueUrls(urls).slice(0, MAX_SELECTION_URLS);
}

async function probe(serverUrl, url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);

  try {
    const response = await fetch(serverUrl + "/api/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
      signal: controller.signal,
    });

    if (!response.ok) return null;
    return await response.json();
  } catch (_) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function queueUrl(serverUrl, url) {
  const preview = await probe(serverUrl, url);
  const body = new URLSearchParams();
  body.set("url", url);
  body.set("playlist", "on");

  const response = await fetch(serverUrl + "/download", {
    method: "POST",
    body,
    redirect: "follow",
  });

  if (!response.ok) {
    throw new Error("Server returned HTTP " + response.status);
  }

  return {
    url,
    preview,
    album: preview ? Boolean(preview.is_album) : null,
  };
}

async function showPageToast(tabId, message, kind = "success") {
  if (!Number.isInteger(tabId)) return false;

  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (text, tone) => {
        const old = document.getElementById("__ytdlp_server_toast");
        if (old) old.remove();

        const node = document.createElement("div");
        node.id = "__ytdlp_server_toast";
        node.textContent = text;
        Object.assign(node.style, {
          position: "fixed",
          top: "18px",
          right: "18px",
          zIndex: "2147483647",
          maxWidth: "360px",
          padding: "10px 13px",
          borderRadius: "9px",
          background: tone === "error" ? "#4a2020" : tone === "warn" ? "#443b20" : "#1d3d2a",
          color: "#f4f5f5",
          border: "1px solid " + (tone === "error" ? "#8b3b3b" : tone === "warn" ? "#84712f" : "#397450"),
          boxShadow: "0 8px 30px rgba(0,0,0,.35)",
          font: "13px/1.4 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
          opacity: "1",
          transition: "opacity .25s ease",
        });
        document.documentElement.appendChild(node);

        setTimeout(() => {
          node.style.opacity = "0";
          setTimeout(() => node.remove(), 300);
        }, 2600);
      },
      args: [message, kind],
    });
    return true;
  } catch (_) {
    return false;
  }
}

async function setBadge(message, kind = "success") {
  const text = kind === "error" ? "!" : kind === "warn" ? "?" : "✓";
  const color = kind === "error" ? "#9b3c3c" : kind === "warn" ? "#8a742f" : "#2f7b4b";

  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
  await chrome.action.setTitle({ title: message });

  setTimeout(async () => {
    await chrome.action.setBadgeText({ text: "" });
    await chrome.action.setTitle({ title: "yt-dlp server settings" });
  }, 3500);
}

async function feedback(tabId, message, kind = "success") {
  const shown = await showPageToast(tabId, message, kind);
  if (!shown) await setBadge(message, kind);
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID) return;

  const { serverUrl } = await getSettings();
  if (!serverUrl) {
    await feedback(tab && tab.id, "Set the yt-dlp server address first.", "warn");
    chrome.runtime.openOptionsPage();
    return;
  }

  if (!(await hasServerPermission(serverUrl))) {
    await feedback(tab && tab.id, "Server permission is missing. Open settings to grant it.", "warn");
    chrome.runtime.openOptionsPage();
    return;
  }

  let urls = [];
  if (info.linkUrl) {
    const link = cleanCandidate(info.linkUrl);
    if (link) urls.push(link);
  }

  if (info.selectionText) {
    urls.push(...await selectionUrls(tab && tab.id, info.selectionText));
  }

  urls = uniqueUrls(urls).slice(0, MAX_SELECTION_URLS);

  if (!urls.length) {
    await feedback(tab && tab.id, "No URL found in the selection.", "warn");
    return;
  }

  let queued = 0;
  let failed = 0;
  let nonAlbums = 0;

  for (const url of urls) {
    try {
      const result = await queueUrl(serverUrl, url);
      queued += 1;
      if (result.album === false) nonAlbums += 1;
    } catch (_) {
      failed += 1;
    }
  }

  if (!queued) {
    await feedback(tab && tab.id, "Could not send the selected URL(s) to the server.", "error");
    return;
  }

  let message = queued === 1
    ? "Sent 1 link to yt-dlp."
    : "Sent " + queued + " links to yt-dlp.";

  if (nonAlbums) {
    message += " " + nonAlbums + (nonAlbums === 1 ? " does" : " do") + " not look like an album.";
  }
  if (failed) {
    message += " " + failed + " failed.";
  }

  await feedback(tab && tab.id, message, failed ? "warn" : nonAlbums ? "warn" : "success");
});
