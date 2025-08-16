// popup.js
const API = "http://127.0.0.1:8001"; // FastAPI

function log(m) {
  const el = document.getElementById("log");
  el.textContent += m + "\n";
  el.scrollTop = el.scrollHeight;
}

async function getActiveTab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t;
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("run").addEventListener("click", onRun);
});

/* ------------------------- Bridge helpers ------------------------- */

async function postSnapshot(snap, { retries = 2 } = {}) {
  const body = JSON.stringify(snap || {});
  for (let i = 0; i <= retries; i++) {
    try {
      const r = await fetch(`${API}/bridge/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (r.ok) return true;
    } catch (e) {
      if (i === retries) throw e;
      await new Promise((res) => setTimeout(res, 150 + 150 * i));
    }
  }
  return false;
}

// Debounce to avoid flooding the server during SPA reflows
let _debounceTimer = null;
let _debounceLastSnap = null;
function postSnapshotDebounced(snap) {
  _debounceLastSnap = snap;
  if (_debounceTimer) clearTimeout(_debounceTimer);
  _debounceTimer = setTimeout(async () => {
    try { await postSnapshot(_debounceLastSnap); }
    catch (e) { console.warn("bridge snapshot (debounced) failed", e); }
    _debounceTimer = null;
    _debounceLastSnap = null;
  }, 200);
}

/* ------------------------- Background snapshots ------------------------- */

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "FRESH_SNAPSHOT") {
    const snap = msg.data || {};
    const buttons = Array.isArray(snap.buttons) ? snap.buttons : [];
    const links = Array.isArray(snap.links) ? snap.links : [];
    const headings = Array.isArray(snap.headings) ? snap.headings : [];
    const firstN = (arr, n) => arr.slice(0, n).map(x => (x.text || "").trim()).filter(Boolean);
    const summary = {
      url: snap.url,
      title: snap.title,
      counts: { buttons: buttons.length, links: links.length, headings: headings.length },
      top_headings: firstN(headings, 5),
      top_buttons: firstN(buttons, 5),
      top_links: firstN(links, 5),
    };
    log("** FRESH SNAPSHOT: " + JSON.stringify(summary));

    // Forward to server (debounced) so subgraphs see the live DOM
    postSnapshotDebounced(snap);
  }
});

/* ------------------------- Tab navigation helper ------------------------- */

async function waitForTabNavigation(tabId, { timeout = 25000 } = {}) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    function onUpdated(id, info, tab) {
      if (id === tabId && info.status === "complete" && /^https?:\/\//.test(tab.url || "")) {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        clearInterval(timer);
        resolve(tab);
      }
    }
    chrome.tabs.onUpdated.addListener(onUpdated);
    const timer = setInterval(() => {
      if (Date.now() - start > timeout) {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        clearInterval(timer);
        reject(new Error("Navigation timeout"));
      }
    }, 500);
  });
}

/* ------------------------- Small utilities ------------------------- */

function sanitizeGoal(input) {
  // Strip common XPath/CSS fragments that confuse the planner's text-only `find`
  return (input || "")
    .replace(/\/\/[^ ]+/g, " ")      // remove //xpath like bits
    .replace(/\[[^\]]+\]/g, " ")     // remove [predicates]
    .replace(/[:.#>][\w\-()]+/g, " ")// remove obvious CSS tokens
    .replace(/\s+/g, " ")
    .trim();
}

/* ------------------------- Main loop (stateless) ------------------------- */

async function onRun() {
  const tab = await getActiveTab();
  if (!tab) return log("No active tab");

  // Track this tab and auto-snapshot on page changes
  await chrome.runtime.sendMessage({ type: "START_TRACK_TAB", tabId: tab.id }).catch(() => {});

  // Always start from HealthHub home
  await chrome.tabs.update(tab.id, { url: "https://www.healthhub.sg/" });
  await waitForTabNavigation(tab.id);

  // Inject content tools
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  if (!/^https?:\/\//.test(tab.url || "")) return log("Open a normal webpage first.");

  // Backend health check
  const h = await fetch(`${API}/health`).then(r => r.json()).catch(e => ({ error: String(e) }));
  log("health: " + JSON.stringify(h));
  if (!h || h.ok !== true) return log("Backend not healthy");

  // Sanitize goal to plain text
  const rawGoal = (document.getElementById("goal")?.value || "Find the Book Appointment button").trim();
  const goal = sanitizeGoal(rawGoal);

  const runTool = (tool, args) => chrome.tabs.sendMessage(tab.id, { type: "RUN_TOOL", tool, args });

  // 1) Initial snapshot for context
  const snap = await runTool("get_page_state", {});
  if (!snap?.ok) { log("snapshot failed: " + JSON.stringify(snap)); return; }
  const page_state = snap.data;
  log(`Snapshot: url=${page_state.url} buttons=${(page_state.buttons || []).length} links=${(page_state.links || []).length}`);

  // Seed the server bridge *synchronously* before we call /agent/run
  try {
    await postSnapshot(page_state);
  } catch (e) {
    console.warn("bridge snapshot (initial) failed", e);
  }

  log(`Sending to backend (one-shot): current_url=${page_state.url}`);

  // 2) Request plan (stateless)
  const planRes = await fetch(`${API}/agent/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      goal,
      page_state,
      current_url: page_state.url
    })
  });
  if (!planRes.ok) { log(`HTTP ${planRes.status} on /agent/run`); return; }

  const planPayload = await planRes.json();
  const steps = Array.isArray(planPayload?.steps) ? planPayload.steps : [];
  const hint = planPayload?.hint || {};
  log(`Plan received: ${steps.length} steps`);

  // 3) Execute steps locally
  let lastFindTop = null; // <-- remember top find result (for href fallback)

  for (const step of steps) {
    const { tool, args } = step || {};
    if (!tool) continue;

    if (tool === "done" || tool === "fail") {
      log(`** ${tool.toUpperCase()}: ${JSON.stringify(args || {})}`);
      break;
    }

    // Optional alias
    const execTool = (tool === "goto") ? "nav" : tool;
    log(`>> RUN_TOOL ${execTool} ${JSON.stringify(args || {})}`);

    // Execute tool
    let obs = await runTool(execTool, args || {});
    log(`.. OBS ${execTool}: ${JSON.stringify(obs || {})}`);

    // Record last find's top match for later href fallback
    if (execTool === "find" && obs?.ok && Array.isArray(obs.data?.matches) && obs.data.matches.length) {
      lastFindTop = obs.data.matches[0] || null;
    }

    // If CLICK failed (element not found), fall back to navigating to last find's href
    if (execTool === "click" && (!obs?.ok || obs?.data?.error === "element not found")) {
      if (lastFindTop?.href) {
        log(`.. CLICK fallback: navigating to last find href ${lastFindTop.href}`);
        try {
          await chrome.tabs.update(tab.id, { url: lastFindTop.href });
          obs = { ok: true, data: { navigating: lastFindTop.href } };
        } catch (e) {
          log(`.. CLICK fallback failed: ${e?.message || e}`);
        }
      }
    }

    // If CLICK succeeded but didn’t navigate (common on JS-intercepted anchors), force nav to last find href
    if (execTool === "click" && obs?.ok && !obs.data?.navigating && lastFindTop?.href) {
      log(`.. CLICK no nav; forcing nav to ${lastFindTop.href}`);
      try {
        await chrome.tabs.update(tab.id, { url: lastFindTop.href });
        obs = { ok: true, data: { navigating: lastFindTop.href } };
      } catch (e) {
        log(`.. force nav failed: ${e?.message || e}`);
      }
    }

    // Also handle explicit navigate_to from content click (anchors)
    if (execTool === "click" && obs?.ok && obs.data?.navigate_to) {
      try {
        await chrome.tabs.update(tab.id, { url: obs.data.navigate_to });
        obs = { ok: true, data: { navigating: obs.data.navigate_to } };
        log(`.. NAV via chrome.tabs.update -> ${obs.data.navigating}`);
      } catch (e) {
        console.warn("chrome.tabs.update failed", e);
      }
    }

    // If navigation occurred, wait, re-inject tools, let page settle, then seed the bridge
    const navigated = (execTool === "nav") || (obs?.ok && (obs.data?.navigating || obs.data?.href));
    if (navigated) {
      await waitForTabNavigation(tab.id);
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      await runTool("wait_for_idle", { quietMs: 700, timeout: 8000 });

      // Synchronous bridge seed after nav settles
      try {
        const snapNow = await runTool("get_page_state", {});
        if (snapNow?.ok && snapNow.data) {
          await postSnapshot(snapNow.data);
        }
      } catch (e) {
        console.warn("bridge snapshot (post-nav) failed", e);
      }
    }
  }

  // 4) Optional arrival verification (independent of auto snapshots)
  try {
    const snap2 = await runTool("get_page_state", {});
    if (snap2?.ok) {
      const url = (snap2.data?.url || "").toLowerCase();
      if (hint?.expect_path && url.includes(hint.expect_path)) {
        log(`** DONE: Arrived at ${hint.expect_path}`);
      } else if (hint?.summary) {
        log(`** SUMMARY: ${hint.summary}`);
      }
    }
  } catch {}

  // Optional: stop tracking
  // await chrome.runtime.sendMessage({ type: "STOP_TRACK_TAB" }).catch(() => {});
}
