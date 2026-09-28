const serverInput = document.getElementById("server-url");
const saveButton = document.getElementById("save");
const testButton = document.getElementById("test");
const statusNode = document.getElementById("status");

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
  return new URL(serverUrl).origin + "/*";
}

function setStatus(message, good = null) {
  statusNode.textContent = message;
  statusNode.className = good === true ? "good" : good === false ? "bad" : "";
}

async function load() {
  const stored = await chrome.storage.sync.get({ serverUrl: "" });
  serverInput.value = stored.serverUrl || "";
}

async function testServer(serverUrl) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 7000);

  try {
    const response = await fetch(serverUrl + "/health", {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error("HTTP " + response.status);

    const health = await response.json();
    if (health.status !== "ok") throw new Error("Unexpected health response");

    setStatus("Connected. Server is ready.", true);
    return true;
  } catch (error) {
    setStatus("Could not reach the server: " + error.message, false);
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function ensurePermission(serverUrl) {
  const origin = permissionPattern(serverUrl);
  const alreadyGranted = await chrome.permissions.contains({ origins: [origin] });
  if (alreadyGranted) return true;

  return await chrome.permissions.request({ origins: [origin] });
}

saveButton.addEventListener("click", async () => {
  const serverUrl = normalizeServerUrl(serverInput.value);
  if (!serverUrl) {
    setStatus("Enter a valid http:// or https:// server URL.", false);
    return;
  }

  const granted = await ensurePermission(serverUrl);
  if (!granted) {
    setStatus("Chrome needs permission to contact that server.", false);
    return;
  }

  const previous = await chrome.storage.sync.get({ serverUrl: "" });
  const oldUrl = normalizeServerUrl(previous.serverUrl);

  await chrome.storage.sync.set({ serverUrl });
  serverInput.value = serverUrl;

  if (oldUrl && oldUrl !== serverUrl) {
    try {
      await chrome.permissions.remove({ origins: [permissionPattern(oldUrl)] });
    } catch (_) {}
  }

  setStatus("Saved. Testing connection…");
  await testServer(serverUrl);
});

testButton.addEventListener("click", async () => {
  const serverUrl = normalizeServerUrl(serverInput.value);
  if (!serverUrl) {
    setStatus("Enter a valid server URL first.", false);
    return;
  }

  if (!(await ensurePermission(serverUrl))) {
    setStatus("Chrome needs permission to contact that server.", false);
    return;
  }

  setStatus("Testing connection…");
  await testServer(serverUrl);
});

load();
