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

// Listen for auto snapshots from background
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

    // ⬇️ Publish fresh snapshot to server so subgraphs see the live page
    (async () => {
      try {
        await fetch(`${API}/bridge/snapshot`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(snap),
        });
      } catch (e) {
        console.warn("bridge snapshot (fresh) failed", e);
      }
    })();
  }
});

/* ---------- Tab navigation helper ---------- */
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

/* ---------- Main loop (stateless) ---------- */
async function onRun() {
  const tab = await getActiveTab();
  if (!tab) return log("No active tab");

  // ⬇️ Tell background to track this tab and auto-snapshot on page changes
  await chrome.runtime.sendMessage({ type: "START_TRACK_TAB", tabId: tab.id }).catch(() => { });

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

  const goal = (document.getElementById("goal")?.value || "Find the Book Appointment button").trim();
  const runTool = (tool, args) => chrome.tabs.sendMessage(tab.id, { type: "RUN_TOOL", tool, args });

  // 1) Snapshot for context
  const snap = await runTool("get_page_state", {});
  if (!snap?.ok) { log("snapshot failed: " + JSON.stringify(snap)); return; }
  const page_state = snap.data;
  log(`Snapshot: url=${page_state.url} buttons=${(page_state.buttons || []).length} links=${(page_state.links || []).length}`);
  log(`Sending to backend (one-shot): current_url=${page_state.url}`);

  // ⬇️ Seed the server’s bridge so subgraphs see the same DOM we see here
  try {
    await fetch(`${API}/bridge/snapshot`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(page_state),
    });
  } catch (e) {
    console.warn("bridge snapshot (initial) failed", e);
  }

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
  for (const step of steps) {
    const { tool, args } = step || {};
    if (!tool) continue;

    if (tool === "done" || tool === "fail") {
      log(`** ${tool.toUpperCase()}: ${JSON.stringify(args || {})}`);
      break;
    }

    // Support optional alias
    const execTool = (tool === "goto") ? "nav" : tool;
    log(`>> RUN_TOOL ${execTool} ${JSON.stringify(args || {})}`);
    const obs = await runTool(execTool, args || {});
    log(`.. OBS ${execTool}: ${JSON.stringify(obs || {})}`);

    // If navigation occurred, wait, re-inject tools, and let page settle
    const navigated = (execTool === "nav") || (obs?.ok && (obs.data?.navigating || obs.data?.href));
    if (navigated) {
      await waitForTabNavigation(tab.id);
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      await runTool("wait_for_idle", { quietMs: 600, timeout: 6000 });
      // ⬆️ Background will also auto-snapshot and push **FRESH_SNAPSHOT** here (and we forward it to the server).
      // ⬇️ Synchronous, guaranteed bridge seed after navigation settles
      try {
        const snapNow = await runTool("get_page_state", {});
        if (snapNow?.ok && snapNow.data) {
          await fetch(`${API}/bridge/snapshot`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(snapNow.data),
          });
        }
      } catch (e) {
        console.warn("bridge snapshot (post-nav) failed", e);
      }
    }
  }

  // 4) Keep your optional arrival verification (independent of auto snapshots)
  const snap2 = await runTool("get_page_state", {});
  if (snap2?.ok) {
    const url = (snap2.data?.url || "").toLowerCase();
    if (hint?.expect_path && url.includes(hint.expect_path)) {
      log(`** DONE: Arrived at ${hint.expect_path}`);
    } else if (hint?.summary) {
      log(`** SUMMARY: ${hint.summary}`);
    }
  }

  // Optional: stop tracking when all is done
  // await chrome.runtime.sendMessage({ type: "STOP_TRACK_TAB" }).catch(() => {});
}
