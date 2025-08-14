// popup.js
const API = "http://127.0.0.1:8001"; // FastAPI
const THREAD_ID = crypto.randomUUID(); // Stable per popup session

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

/* ---------- Inline clarification prompt ---------- */
function setBusyPrompting(busy) {
  const btn = document.getElementById("run");
  if (btn) btn.disabled = !!busy;
}
function askInline(promptText) {
  return new Promise((resolve) => {
    const box = document.getElementById("clarify");
    const txt = document.getElementById("clarify-text");
    const inp = document.getElementById("clarify-input");
    const ok = document.getElementById("clarify-ok");
    const cancel = document.getElementById("clarify-cancel");

    txt.textContent = promptText || "What should I do next?";
    inp.value = "";
    box.style.display = "block";
    inp.focus();
    setBusyPrompting(true);

    function cleanup(val) {
      setBusyPrompting(false);
      box.style.display = "none";
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      resolve(val);
    }
    function onOk() { cleanup(inp.value.trim()); }
    function onCancel() { cleanup(null); }

    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
  });
}

/* ---------- Tab navigation helper ---------- */
async function waitForTabNavigation(tabId, { timeout = 25000 } = {}) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    function cleanup() {
      chrome.tabs.onUpdated.removeListener(onUpdated);
    }
    function onUpdated(id, info, tab) {
      if (id === tabId && info.status === "complete" && /^https?:\/\//.test(tab.url || "")) {
        cleanup();
        resolve(tab);
      }
    }
    chrome.tabs.onUpdated.addListener(onUpdated);
    const timer = setInterval(() => {
      if (Date.now() - start > timeout) {
        cleanup();
        clearInterval(timer);
        reject(new Error("Navigation timeout"));
      }
    }, 500);
  });
}

/* ---------- Scoring helpers ---------- */
function scoreClickable(text) {
  const t = (text || "").toLowerCase();
  let s = 0;
  if (!t) return s;
  if (t.length <= 3) s -= 2;
  if (t.includes("appointment")) s += 20;
  if (t.includes("book")) s += 12;
  if (t.includes("login") || t.includes("sign in")) s += 10;
  if (t.includes("search")) s += 4;
  if (t.includes("healthier sg")) s += 3;
  if (t.includes("payments")) s += 3;
  if (t.includes("results")) s += 2;
  s += Math.min(6, Math.max(0, Math.floor((t.length - 8) / 10)));
  return s;
}

function dedupeBySelector(items) {
  const seen = new Set();
  const out = [];
  for (const it of items) {
    const sel = it.selector || "";
    if (sel && !seen.has(sel)) {
      seen.add(sel);
      out.push(it);
    }
  }
  return out;
}

function compactClickableList(snapshot) {
  const raw = Array.isArray(snapshot?.buttons) ? snapshot.buttons : [];
  const shaped = raw
    .map(b => ({ text: (b.text || "").trim(), selector: b.selector || "" }))
    .filter(b => b.selector && b.text)
    .map(b => ({ ...b, score: scoreClickable(b.text) }));
  const deduped = dedupeBySelector(shaped);
  deduped.sort((a, b) => (b.score - a.score) || (a.selector.length - b.selector.length));
  return deduped;
}

// Optional: background “SCOUT_URLS” previewer (CORS-safe HTML peek)
async function scoutUrls(urls) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type: "SCOUT_URLS", urls }, (resp) => {
        resolve(resp?.data || []);
      });
    } catch {
      resolve([]);
    }
  });
}

/* ---------- Guided prompt helpers ---------- */
let __lastFindData = null; // keep last find().data so we can suggest exact matches quickly

function buildLocalGuidance({ page_state, lastFindData, thing }) {
  const lines = [];
  const add = (s = "") => lines.push(s);

  add("I couldn’t decide the next action. Pick one of these or type your own instruction:\n");

  // Prefer the most recent find() candidates
  const findMatches = (lastFindData?.matches || []).slice(0, 5);
  if (findMatches.length) {
    add("• From what I just searched:");
    findMatches.forEach((m, i) => {
      const txt = (m.text || "").trim().slice(0, 80) || "(unlabeled button/link)";
      add(`  ${i + 1}. Click “${txt}”`);
    });
  } else {
    // Otherwise suggest the best on-page buttons
    const topButtons = compactClickableList(page_state).slice(0, 5);
    if (topButtons.length) {
      add("• Popular actions on this page:");
      topButtons.forEach((b, i) => {
        const txt = (b.text || "").trim().slice(0, 80);
        add(`  ${i + 1}. Click “${txt}”`);
      });
    }
  }

  if (thing) {
    add("\n• Goal-oriented suggestions:");
    add(`  - Search again for: “${thing}”`);
    add(`  - Try: “find a:contains('${thing}')”`);
  }

  add(
    "\nReply with one of the numbers above (e.g., 1), or a command like:",
    "  • click <exact label>",
    "  • find <keywords>",
    "  • type selector=<css> text=<value>"
  );

  return lines.join("\n");
}

function buildAgentGuidanceText(info) {
  // Render options/examples coming from the agent interrupt payload
  let guided = (info.prompt || "I need clarification to continue.");
  if (Array.isArray(info.options) && info.options.length) {
    guided += "\n\nOptions:\n";
    for (const o of info.options) {
      const n = typeof o.n === "number" ? o.n : "-";
      const label = o.label || "(unlabeled)";
      guided += `  ${n}. ${label}\n`;
    }
    if (Array.isArray(info.examples) && info.examples.length) {
      guided += "\nTry: " + info.examples.map(x => `“${x}”`).join(", ");
    }
  }
  return guided;
}

/* ---------- Main loop ---------- */
async function onRun() {
  const tab = await getActiveTab();
  if (!tab) return log("No active tab");
  if (!/^https?:\/\//.test(tab.url || "")) return log("Open a normal webpage first.");

  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });

  const h = await fetch(`${API}/health`).then(r => r.json()).catch(e => ({ error: String(e) }));
  log("health: " + JSON.stringify(h));
  if (!h || h.ok !== true) return log("Backend not healthy");

  const goal = (document.getElementById("goal")?.value || "Find the Book Appointment button").trim();

  const runTool = (tool, args) =>
    chrome.tabs.sendMessage(tab.id, { type: "RUN_TOOL", tool, args });

  let hops = 0;
  let finished = false;
  let userReply = null;
  let lastTool = null;
  let lastObs = null;

  while (hops < 12 && !finished) {
    const snap = await runTool("get_page_state", {});
    if (!snap?.ok) { log("snapshot failed: " + JSON.stringify(snap)); return; }
    const page_state = snap.data;
    log(`Snapshot: url=${page_state.url} buttons=${(page_state.buttons || []).length} links=${(page_state.links || []).length}`);

    const body = {
      goal,
      page_state,
      thread_id: THREAD_ID,
      ...(userReply ? { user_reply: userReply } : {}),
      ...(lastTool ? { last_tool: lastTool } : {}),
      ...(lastObs ? { last_obs: lastObs } : {}),
    };
    log(">> POST /agent/run (turn=" + (hops + 1) + ")");
    const res = await fetch(`${API}/agent/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!res.ok) { log(`HTTP ${res.status} on /agent/run`); return; }
    const payload = await res.json();
    const msgs = payload?.messages || [];
    log("<< turn response, messages=" + msgs.length);

    const planMsg = msgs.find((m) => m.name === "EXECUTION_PLAN");
    const steps = planMsg?.content ? (JSON.parse(planMsg.content).steps || []) : [];
    log(`EXECUTION_PLAN: ${steps.length} step(s)`);

    let navigated = false;
    for (const step of steps) {
      const { tool, args } = step || {};
      if (!tool) continue;

      if (tool === "done" || tool === "fail") {
        log(`** ${tool.toUpperCase()}: ${JSON.stringify(args || {})}`);
        finished = true;
        break;
      }

      if (tool === "goto") {
        log(`>> RUN_TOOL nav ${JSON.stringify(args || {})}`);
        const navObs = await runTool("nav", args || {});
        log(`.. OBS nav: ${JSON.stringify(navObs || {})}`);
        lastTool = "nav";
        lastObs = JSON.stringify(navObs || {});
        navigated = true;

        await waitForTabNavigation(tab.id);
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
        break;
      }

      log(`>> RUN_TOOL ${tool} ${JSON.stringify(args || {})}`);
      const obs = await runTool(tool, args || {});
      log(`.. OBS ${tool}: ${JSON.stringify(obs || {})}`);
      lastTool = tool;
      lastObs = JSON.stringify(obs || {});

      if (tool === "nav" || (obs?.ok && (obs.data?.navigating || obs.data?.href))) {
        navigated = true;
        await waitForTabNavigation(tab.id);
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
        break;
      }
    }
    hops++;
  }
}