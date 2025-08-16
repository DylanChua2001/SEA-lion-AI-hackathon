// popup.js
const API = "http://127.0.0.1:8001"; // FastAPI
const TTS_API = "http://127.0.0.1:8000";
const LAB_URL_TOKEN = "/lab-test-reports/lab"; // used to confirm arrival

/* ------------------------- TTS helpers ------------------------- */

async function speakText(text) {
  try {
    const res = await fetch(`${TTS_API}/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    });
    if (!res.ok) throw new Error("TTS request failed");

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);

    const player = document.getElementById("tts-player");
    if (player) {
      player.src = url;
      player.style.display = "block";
      player.play().catch(() => { });  // autoplay may require user gesture
    }
  } catch (e) {
    console.error("speakText error", e);
  }
}

/* ------------------------- Logging ------------------------- */

function log(m) {
  const el = document.getElementById("log");
  el.textContent += m + "\n";
  el.scrollTop = el.scrollHeight;

  // Speak ONLY lines explicitly marked for TTS (***), stripping the asterisks
  if (m.startsWith("***")) {
    const spoken = m.replace(/^\*+/, "").trim();
    if (spoken) speakText(spoken);
  }
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

/* ------------------------- Utilities ------------------------- */

function sanitizeGoal(input) {
  return (input || "")
    .replace(/\/\/[^ ]+/g, " ")
    .replace(/\[[^\]]+\]/g, " ")
    .replace(/[:.#>][\w\-()]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

async function runTool(tabId, tool, args) {
  return chrome.tabs.sendMessage(tabId, { type: "RUN_TOOL", tool, args });
}

async function injectTools(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
}

async function seedBridgeWithCurrent(tabId) {
  const snap = await runTool(tabId, "get_page_state", {});
  if (snap?.ok && snap.data) {
    await postSnapshot(snap.data);
    return snap.data;
  }
  return null;
}

/* ------------------------- Auth heuristics (client-side) ------------------------- */

function looksLoggedIn(snap) {
  if (!snap || typeof snap !== "object") return false;

  if (snap.session && snap.session.is_authenticated === true) return true;

  const flags = snap.flags || {};
  if (flags.sslIsAnonymous === "True") return false;
  if (flags.hasLoginButton === true) return false;

  const list = (x) => (Array.isArray(x) ? x.map((t) => String(t).toLowerCase()) : []);
  const tb = list(snap.top_buttons);
  const tl = list(snap.top_links);
  const th = list(snap.top_headings);
  if (tb.concat(tl, th).some(t => /logout|my profile|welcome|^hi\s/.test(t))) return true;

  const url = String(snap.url || "").toLowerCase();
  if (url.includes("eservices.healthhub.sg") && !/login/.test(url) && flags.hasLoginButton !== true) return true;

  return false;
}

function looksLikeSingpass(snap) {
  const flags = (snap && snap.flags) || {};
  if (flags.singpassLike) return true;
  const url = String(snap?.url || "").toLowerCase();
  return /singpass|login\.singpass|authorize|oauth|account\/login|myinfo/.test(url);
}

/* ------------------------- Core: one planning+execution pass ------------------------- */

async function runOnce({ tabId, goal, label = "pass" }) {
  const snap = await runTool(tabId, "get_page_state", {});
  if (!snap?.ok) { log(`[${label}] snapshot failed: ${JSON.stringify(snap)}`); return { ok: false }; }
  const page_state = snap.data;
  await postSnapshot(page_state).catch(() => { });

  log(`[${label}] Sending to backend: current_url=${page_state.url}`);

  const planRes = await fetch(`${API}/agent/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal, page_state, current_url: page_state.url }),
  });
  if (!planRes.ok) {
    log(`[${label}] HTTP ${planRes.status} on /agent/run`);
    return { ok: false };
  }

  const planPayload = await planRes.json();
  const steps = Array.isArray(planPayload?.steps) ? planPayload.steps : [];
  const hint = planPayload?.hint || {};
  log(`[${label}] Plan received: ${steps.length} steps`);

  let lastFindTop = null;

  for (const step of steps) {
    const { tool, args } = step || {};
    if (!tool) continue;

    if (tool === "done" || tool === "fail") {
      log(`** ${tool.toUpperCase()}: ${JSON.stringify(args || {})}`);
      // Speak only the backend-provided TTS string (if present) for DONE
      if (tool === "done" && args && typeof args.tts === "string" && args.tts.trim()) {
        speakText(args.tts.trim());
      }
      break;
    }

    const execTool = (tool === "goto") ? "nav" : tool;
    log(`>> RUN_TOOL ${execTool} ${JSON.stringify(args || {})}`);
    let obs = await runTool(tabId, execTool, args || {});
    log(`.. OBS ${execTool}: ${JSON.stringify(obs || {})}`);

    if (execTool === "find" && obs?.ok && Array.isArray(obs.data?.matches) && obs.data.matches.length) {
      lastFindTop = obs.data.matches[0] || null;
    }

    if (execTool === "click" && (!obs?.ok || obs?.data?.error === "element not found")) {
      if (lastFindTop?.href) {
        log(`.. CLICK fallback: navigating to ${lastFindTop.href}`);
        await chrome.tabs.update(tabId, { url: lastFindTop.href }).catch(() => { });
        obs = { ok: true, data: { navigating: lastFindTop.href } };
      }
    }
    if (execTool === "click" && obs?.ok && !obs.data?.navigating && lastFindTop?.href) {
      log(`.. CLICK no nav; forcing nav to ${lastFindTop.href}`);
      await chrome.tabs.update(tabId, { url: lastFindTop.href }).catch(() => { });
      obs = { ok: true, data: { navigating: lastFindTop.href } };
    }
    if (execTool === "click" && obs?.ok && obs.data?.navigate_to) {
      await chrome.tabs.update(tabId, { url: obs.data.navigate_to }).catch(() => { });
      obs = { ok: true, data: { navigating: obs.data.navigate_to } };
      log(`.. NAV via chrome.tabs.update -> ${obs.data.navigating}`);
    }

    const navigated = (execTool === "nav") || (obs?.ok && (obs.data?.navigating || obs.data?.href));
    if (navigated) {
      await waitForTabNavigation(tabId);
      await injectTools(tabId);
      await runTool(tabId, "wait_for_idle", { quietMs: 700, timeout: 8000 });
      await seedBridgeWithCurrent(tabId);
    }
  }

  try {
    const snap2 = await runTool(tabId, "get_page_state", {});
    if (snap2?.ok) {
      const url = (snap2.data?.url || "").toLowerCase();
      if (hint?.expect_path && url.includes(hint.expect_path)) {
        log(`** DONE: Arrived at ${hint.expect_path}`);
      } else if (hint?.summary) {
        log(`** SUMMARY: ${hint.summary}`);
      }
    }
  } catch { }

  return { ok: true };
}

/* ------------------------- Main: two-step with login gate ------------------------- */

async function onRun() {
  const tab = await getActiveTab();
  if (!tab) return log("No active tab");

  await chrome.runtime.sendMessage({ type: "START_TRACK_TAB", tabId: tab.id }).catch(() => { });

  await chrome.tabs.update(tab.id, { url: "https://www.healthhub.sg/" });
  await waitForTabNavigation(tab.id);

  await injectTools(tab.id);
  if (!/^https?:\/\//.test(tab.url || "")) return log("Open a normal webpage first.");
  const h = await fetch(`${API}/health`).then(r => r.json()).catch(e => ({ error: String(e) }));
  log("health: " + JSON.stringify(h));
  if (!h || h.ok !== true) return log("Backend not healthy");

  const rawGoal = (document.getElementById("goal")?.value || "view lab results").trim();
  const goal = sanitizeGoal(rawGoal);

  log("Sending to backend (one-shot): initiating pass 1 (navigate)");
  await runOnce({ tabId: tab.id, goal, label: "pass1:navigate" });

  await injectTools(tab.id);
  await runTool(tab.id, "wait_for_idle", { quietMs: 700, timeout: 8000 });
  const seeded = await seedBridgeWithCurrent(tab.id);

  const needLogin = looksLikeSingpass(seeded) || !looksLoggedIn(seeded);
  if (needLogin) {
    // Mark this as TTS-eligible with ***; TTS will strip the asterisks
    log("*** Please log in with Singpass in the tab, then click ‘Run Agent’ again.");
    return;
  }

  const nowUrl = (seeded?.url || "").toLowerCase();
  const onLabPage = nowUrl.includes(LAB_URL_TOKEN);
  log(onLabPage
    ? "Auto two-step: detected lab page. Starting pass 2 (read/extract)…"
    : "Auto two-step: running pass 2; router will choose the correct reader…");

  await runOnce({ tabId: tab.id, goal, label: "pass2:read" });
}
