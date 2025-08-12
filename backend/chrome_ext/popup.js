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

// ---------- Local scoring helpers ----------
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

function scorePreview({ title, snippet }, thing) {
  const t = (title + " " + snippet).toLowerCase();
  const verbs = /\b(new|create|add|book|apply|start|begin|schedule|register)\b/;
  const s = (verbs.test(t) ? 1 : 0) + (t.includes((thing || "").toLowerCase()) ? 1 : 0);
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

  const goal = (document.getElementById("goal")?.value || "Find the Book Appointment button").trim();

  // Extract the 'thing' from the goal (generic create/book/apply detector)
  const CREATE_VERBS = /\b(new|create|add|book|apply|start|begin|schedule|register)\b/i;
  function extractThingFromGoal(g) {
    const m = g.match(new RegExp(CREATE_VERBS.source + "\\s+(?:a|an|the)?\\s*([a-z0-9 \\-/]+)", "i"));
    if (m) {
      return m[1].trim().replace(/\b(on|for|to|at|in)\b.*$/i, "").trim();
    }
    return "";
  }
  const thing = extractThingFromGoal(goal);

  // helper to run one tool via content.js
  const runTool = (tool, args) =>
    chrome.tabs.sendMessage(tab.id, { type: "RUN_TOOL", tool, args });

  let hops = 0;
  let finished = false;

  while (hops < 12 && !finished) {
    // 1) snapshot current page
    const snap = await runTool("get_page_state", {});
    if (!snap?.ok) { log("snapshot failed: " + JSON.stringify(snap)); return; }
    const page_state = snap.data;
    log(`Snapshot: url=${page_state.url} buttons=${(page_state.buttons || []).length} links=${(page_state.links || []).length}`);

    // 2) ask backend for this TURN
    const body = { goal, page_state };
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

    // pretty-print tool calls (optional)
    for (const m of msgs) {
      const t = m.type || "Message";
      if (Array.isArray(m.tool_calls) && m.tool_calls.length) {
        for (const tc of m.tool_calls) log(`${t}: TOOL ${tc.name} ${JSON.stringify(tc.args)}`);
      }
    }

    // plan for this turn
    const planMsg = msgs.find(m => m.name === "EXECUTION_PLAN");
    const steps = planMsg?.content ? (JSON.parse(planMsg.content).steps || []) : [];
    log(`EXECUTION_PLAN: ${steps.length} step(s)`);

    // 3) execute plan; break early if we navigate
    let navigated = false;
    for (const step of steps) {
      const { tool, args } = step || {};
      if (!tool) continue;

      // stop on terminal
      if (tool === "done" || tool === "fail") {
        log(`** ${tool.toUpperCase()}: ${JSON.stringify(args || {})}`);
        finished = true;
        break;
      }

      // map 'goto' to extension 'nav' (if backend emits goto)
      if (tool === "goto") {
        log(`>> RUN_TOOL nav ${JSON.stringify(args || {})}`);
        const navObs = await runTool("nav", args || {});
        log(`.. OBS nav: ${JSON.stringify(navObs || {})}`);
        navigated = true;
        log(".. waiting for load…");
        await runTool("wait_for_load", { timeout: 25000 });
        await runTool("wait_for_idle", { quietMs: 800, timeout: 6000 });
        break;
      }

      // SPECIAL HANDLING: 'find' with multiple candidates → scout + score → click best
      if (tool === "find") {
        log(`>> RUN_TOOL find ${JSON.stringify(args || {})}`);
        const obs = await runTool("find", args || {});
        log(`.. OBS find: ${JSON.stringify(obs || {})}`);

        const matches = obs?.data?.matches || [];
        if (matches.length > 1) {
          // Try to peek linked pages (when hrefs exist); cap for performance
          const urls = matches.map(m => m.href).filter(Boolean).slice(0, 8);
          if (urls.length && thing) {
            const previews = await scoutUrls(urls);
            const scored = previews.map((p, i) => ({
              ...p,
              selector: matches[i].selector,
              text: matches[i].text || "",
              score: scorePreview(p, thing)
            }));
            // fallback: if equal scores, prefer better on-page label
            scored.sort((a, b) => (b.score - a.score) ||
              (scoreClickable(b.text) - scoreClickable(a.text)) ||
              (a.selector.length - b.selector.length));
            const best = scored[0];
            if (best?.selector) {
              log(`Auto-select best candidate: ${best.title || best.url} (score ${best.score})`);
              const clickObs = await runTool("click", { selector: best.selector });
              log(`.. OBS click(best): ${JSON.stringify(clickObs || {})}`);
              // if it navigated (href), wait and break turn
              if (clickObs?.ok && (clickObs.data?.href || clickObs.data?.navigating)) {
                navigated = true;
                log(".. waiting for load…");
                await runTool("wait_for_load", { timeout: 25000 });
                await runTool("wait_for_idle", { quietMs: 800, timeout: 6000 });
                break;
              }
              // otherwise just continue to next step
              await new Promise(r => setTimeout(r, 200));
              continue;
            }
          }
        }

        // If not multi-match or no scouting possible, just proceed normally
        await new Promise(r => setTimeout(r, 200));
        continue;
      }

      // default path: run tool as-is
      log(`>> RUN_TOOL ${tool} ${JSON.stringify(args || {})}`);
      const obs = await runTool(tool, args || {});
      log(`.. OBS ${tool}: ${JSON.stringify(obs || {})}`);

      // If this step triggers navigation, pause the turn and loop.
      if (tool === "nav" || (obs?.ok && (obs.data?.navigating || obs.data?.href))) {
        navigated = true;
        log(".. waiting for load…");
        await runTool("wait_for_load", { timeout: 25000 });
        await runTool("wait_for_idle", { quietMs: 800, timeout: 6000 });
        break;
      }

      await new Promise(r => setTimeout(r, 200));
    }

    // check final message for 'done'
    const last = msgs[msgs.length - 1];
    if (last?.name === "done") {
      try {
        const info = JSON.parse(last.content || "{}");
        if (info?.reason) log(`** DONE: ${info.reason}`);
      } catch { /* ignore */ }
      finished = true;
    }

    hops += 1;
    if (!navigated && !finished) {
      log("No navigation this turn; stopping to avoid loop.");
      break;
    }
  }
}
