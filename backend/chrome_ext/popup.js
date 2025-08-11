// popup.js
const API = "http://127.0.0.1:8001"; // FastAPI

function log(m) { const el = document.getElementById("log"); el.textContent += m + "\n"; el.scrollTop = el.scrollHeight; }
async function getActiveTab() { const [t] = await chrome.tabs.query({ active: true, currentWindow: true }); return t; }

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("run").addEventListener("click", onRun);
});

function scoreClickable(text) {
  const t = (text || "").toLowerCase();
  let s = 0;
  if (!t) return s;
  if (t.length <= 3) s -= 2;                 // very short labels are often noise
  if (t.includes("appointment")) s += 20;
  if (t.includes("book")) s += 12;
  if (t.includes("login") || t.includes("sign in")) s += 10;
  if (t.includes("search")) s += 4;
  if (t.includes("healthier sg")) s += 3;
  if (t.includes("payments")) s += 3;
  if (t.includes("results")) s += 2;
  // small bias to longer-but-reasonable labels
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
  // snapshot.buttons comes from content.js (a, button, [role=button])
  const raw = Array.isArray(snapshot?.buttons) ? snapshot.buttons : [];
  // keep only items with some label; normalize and score
  const shaped = raw
    .map(b => ({ text: (b.text || "").trim(), selector: b.selector || "" }))
    .filter(b => b.selector && b.text)
    .map(b => ({ ...b, score: scoreClickable(b.text) }));

  const deduped = dedupeBySelector(shaped);
  // rank by score desc, then shorter selector (more specific but not too long)
  deduped.sort((a, b) => (b.score - a.score) || (a.selector.length - b.selector.length));
  return deduped;
}

async function onRun() {
  const tab = await getActiveTab();
  if (!tab) return log("No active tab");
  if (!/^https?:\/\//.test(tab.url || "")) return log("Open a normal webpage first.");

  // inject content to take a snapshot
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });

  // backend health
  const h = await fetch(`${API}/health`).then(r => r.json()).catch(e => ({ error: String(e) }));
  log("health: " + JSON.stringify(h));
  if (!h || h.ok !== true) return log("Backend not healthy");

  // snapshot current page (structure sent to /agent/run)
  const snap = await chrome.tabs.sendMessage(tab.id, { type: "RUN_TOOL", tool: "get_page_state", args: {} })
    .catch(e => ({ ok: false, data: { error: String(e) } }));
  if (!snap?.ok) {
    log("snapshot failed: " + JSON.stringify(snap));
    return;
  }
  const page_state = snap.data;
  const goal = (document.getElementById("goal")?.value || "Find the Book Appointment button").trim();

  // send the full snapshot to the backend; the LLM normaliser will use everything
  log(`Snapshot summary: buttons=${(page_state.buttons||[]).length}, inputs=${(page_state.inputs||[]).length}`);
  const body = { goal, page_state };
  log(">> /agent/run body (with clickables_preview): " + JSON.stringify(body, null, 0));

  const res = await fetch(`${API}/agent/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });

  if (!res.ok) {
    const txt = await res.text().catch(() => "<no body>");
    log(`HTTP ${res.status}: ${txt}`);
    return;
  }

  const payload = await res.json();
  log("<< /agent/run response received.");

  // Pretty-print the agent transcript
  const msgs = payload?.messages || [];
  for (const m of msgs) {
    const t = m.type || "Message";
    if (m.tool_calls && Array.isArray(m.tool_calls) && m.tool_calls.length) {
      for (const tc of m.tool_calls) {
        log(`${t}: TOOL ${tc.name} ${JSON.stringify(tc.args)}`);
      }
    } else {
      log(`${t}: ${typeof m.content === "string" ? m.content : JSON.stringify(m.content)}`);
    }
  }

  // === NEW: execute EXECUTION_PLAN in the page ===
  const planMsg = msgs.find(m => m.name === "EXECUTION_PLAN");
  if (planMsg?.content) {
    try {
      const plan = JSON.parse(planMsg.content);
      const steps = Array.isArray(plan.steps) ? plan.steps : [];
      log(`EXECUTION_PLAN: ${steps.length} step(s)`);

      // helper to run one tool via content.js
      const runTool = (tool, args) =>
        chrome.tabs.sendMessage(tab.id, { type: "RUN_TOOL", tool, args });

      for (const step of steps) {
        const { tool, args } = step || {};
        if (!tool) continue;
        if (tool === "done" || tool === "fail") {
          log(`** ${tool.toUpperCase()}: ${JSON.stringify(args || {})}`);
          break;
        }
        log(`>> RUN_TOOL ${tool} ${JSON.stringify(args || {})}`);
        const obs = await runTool(tool, args || {});
        log(`.. OBS ${tool}: ${JSON.stringify(obs || {})}`);
        // small pacing between actions
        await new Promise(r => setTimeout(r, 200));
      }
    } catch (e) {
      log(`EXECUTION_PLAN parse/exec error: ${e?.message || String(e)}`);
    }
  }

  // Optional: surface 'done' summary if present
  const last = msgs[msgs.length - 1];
  if (last?.name === "done") {
    try {
      const info = JSON.parse(last.content || "{}");
      if (info?.reason) log(`** DONE: ${info.reason}`);
    } catch { }
  }
}
