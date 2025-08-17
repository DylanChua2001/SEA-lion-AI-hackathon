// background.js (MV3 service worker)

let trackedTabId = null;
const debounceTimers = new Map();     // key -> timeout id
const lastSentByTab = new Map();      // tabId -> "url|title" signature

function isHttpUrl(url) {
  return /^https?:\/\//i.test(url || "");
}

function debounce(key, fn, wait = 500) {
  const old = debounceTimers.get(key);
  if (old) clearTimeout(old);
  const t = setTimeout(() => {
    debounceTimers.delete(key);
    fn();
  }, wait);
  debounceTimers.set(key, t);
}

async function snapshotTab(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab || !isHttpUrl(tab.url)) return;

    // Re-inject content.js to ensure it's present on the new page
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });

    // Ask content script for a fresh snapshot
    const snap = await chrome.tabs
      .sendMessage(tabId, { type: "RUN_TOOL", tool: "get_page_state", args: {} })
      .catch(() => null);

    if (!snap || !snap.ok) return;
    const data = snap.data || {};

    // Suppress duplicates (same URL + title)
    const sig = `${data.url || ""}|${data.title || ""}`;
    if (lastSentByTab.get(tabId) === sig) return;
    lastSentByTab.set(tabId, sig);

    // Forward to popup (or any other listener)
    chrome.runtime.sendMessage({
      type: "FRESH_SNAPSHOT",
      tabId,
      data,
    });
  } catch (_) {
    // swallow; keep the worker alive
  }
}

// Track which tab we should snapshot on navigation
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "START_TRACK_TAB") {
      trackedTabId = msg.tabId ?? null;
      // Immediately snapshot the current page too
      if (trackedTabId) {
        debounce(`init:${trackedTabId}`, () => snapshotTab(trackedTabId), 100);
      }
      sendResponse({ ok: true, tracking: trackedTabId });
      return;
    }
    if (msg?.type === "STOP_TRACK_TAB") {
      trackedTabId = null;
      sendResponse({ ok: true });
      return;
    }
    if (msg?.type === "SCOUT_URLS") {
      // existing helper — keep compatible
      const out = (msg.urls || []).map((u) => ({ url: u }));
      sendResponse({ ok: true, data: out });
      return;
    }
    sendResponse({ ok: false, error: "unknown message" });
  })();
  return true;
});

// Full navigations (page loads)
chrome.webNavigation.onCompleted.addListener((details) => {
  if (!trackedTabId || details.tabId !== trackedTabId) return;
  if (!isHttpUrl(details.url)) return;
  debounce(`completed:${details.tabId}`, () => snapshotTab(details.tabId), 400);
});

// SPA route changes (history.pushState / replaceState)
chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (!trackedTabId || details.tabId !== trackedTabId) return;
  if (!isHttpUrl(details.url)) return;
  debounce(`spa:${details.tabId}`, () => snapshotTab(details.tabId), 400);
});

// Optional: clean up when tab closes or updates away
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === trackedTabId) trackedTabId = null;
  debounceTimers.forEach((_, key) => {
    if (key.endsWith(`:${tabId}`)) {
      clearTimeout(debounceTimers.get(key));
      debounceTimers.delete(key);
    }
  });
  lastSentByTab.delete(tabId);
});
