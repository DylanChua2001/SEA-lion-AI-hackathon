// popup.js
const API = "http://127.0.0.1:8001";      // llm_api (agent/run, health, bridge)
const CHAT_API = "http://127.0.0.1:8003";  // chat_api (/chat/simple)
const TTS_API = "http://127.0.0.1:8000";   // tts_backend (/speak)
const LAB_URL_TOKEN = "/lab-test-reports/lab"; // used to confirm arrival

/* ------------------------- Language detection (prompt-based) ------------------------- */
// Return both a human label (for translate_to) and a TTS code (for lang).
function detectLangFromPrompt(text = "") {
  const s = String(text || "").trim();

  // Script-based (reliable)
  if (/[一-鿿㐀-䶵]/u.test(s)) return { label: "Chinese", tts: "zh" };   // CJK
  if (/[\u0B80-\u0BFF]/u.test(s)) return { label: "Tamil", tts: "ta" }; // Tamil

  // Malay heuristics (Latin)
  const lower = s.toLowerCase();
  const malayHints = ["sila", "klik", "log masuk", "kemudian", "sekali lagi", "dengan", "dan", "anda"];
  if (malayHints.some(h => lower.includes(h))) return { label: "Malay", tts: "ms" };

  // Default English
  return { label: "English", tts: "en" };
}

// Session-preferred language (used only for TTS convenience)
let PREFERRED_LANG = { label: "English", tts: "en" };

/* ------------------------- TTS helpers ------------------------- */

async function speakText(text) {
  try {
    const body = {
      text,
      translate_to: PREFERRED_LANG.label,
      lang: PREFERRED_LANG.tts,
    };

    const res = await fetch(`${TTS_API}/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error("TTS request failed");

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);

    const player = document.getElementById("tts-player");
    if (player) {
      player.src = url;
      player.style.display = "block";
      player.play().catch(() => {}); // autoplay may require user gesture
    }
  } catch (e) {
    console.error("speakText error", e);
  }
}

/* ------------------------- Logging ------------------------- */

function log(m) {
  const el = document.getElementById("log");
  if (el) {
    el.textContent += m + "\n";
    el.scrollTop = el.scrollHeight;
  }

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

/* ------------------------- Intent routing ------------------------- */
/**
 * Route to the agent ONLY for HealthHub record actions:
 *   - appointments
 *   - lab results / reports
 *   - immunisations / vaccines
 *   - payments / bills
 *
 * Multilingual (English, Malay, Chinese, Tamil) hardcoded keyword detection.
 * Everything else goes to "chat".
 */
function decideIntent(rawGoal = "") {
  const t = (rawGoal || "").toLowerCase().trim();
  const { label } = detectLangFromPrompt(rawGoal);

  // Brand hint (still in English)
  const mentionsHealthHub = /\bhealthhub\b/.test(t);

  // ---------------- EN / MALAY (Latin) ----------------
  // Verbs indicating action
  const verbsLatin = /(view|see|check|show|get|open|read|look up|access|pay|make payment|settle|see payment|see bill|bayar|buat pembayaran|lihat|semak|buka|akses)/;

  // Nouns for target record types
  const nounsLatin = /(appointment(s)?|temujanji|lab( results?| report(s)?)?|makmal|result(s)?|laporan|keputusan|immuni[sz]ation(s)?|imunisasi|vaccin(e|ation)(s)?|vaksin|payment(s)?|pembayaran|bayaran|bill(s)?|bil|record(s)?|rekod)/;

  // ---------------- CHINESE ----------------
  // Simplified + Traditional variants included
  const zh = /(化验|化驗|检验|檢驗|报告|報告|结果|結果|预约|預約|挂号|掛號|免疫|疫苗|付款|缴费|繳費|账单|賬單|账款|賬款|健康|记录|記錄)/;

  // ---------------- TAMIL ----------------
  // Common terms (mix of native + loanwords)
  const ta = /(அப்பாயின்மெண்ட்|நியமனம்|முன்பதிவு|ஆய்வு|பரிசோதனை|அறிக்கை|மருத்துவ அறிக்கை|தடுப்பூசி|தடுப்பூசிகள்|இம்யூனைக்சன்|கட்டணம்|பணம்|பில்)/;

  // Shortcuts: strong signals even without verbs, across languages
  const shortcutLatin =
    /\b(lab|lab results?|appointment(s)?|immuni[sz]ation(s)?|vaccine(s)?|payment(s)?|bill(s)?)\b/.test(t) ||
    /\b(temujanji|imunisasi|vaksin|pembayaran|bayaran|bil|rekod|laporan|keputusan|makmal)\b/.test(t);

  const shortcutZH = zh.test(rawGoal);
  const shortcutTA = ta.test(rawGoal);

  // Primary decision:
  // - If Chinese or Tamil text contains those keywords -> agent
  if (label === "Chinese" && shortcutZH) return "healthhub_records";
  if (label === "Tamil" && shortcutTA) return "healthhub_records";

  // - If Malay/English text contains both a verb and a record noun -> agent
  if ((label === "Malay" || label === "English") && verbsLatin.test(t) && nounsLatin.test(t)) {
    return "healthhub_records";
  }

  // - If explicitly mentions HealthHub AND has nouns of interest -> agent
  if (mentionsHealthHub && (nounsLatin.test(t) || shortcutZH || shortcutTA)) {
    return "healthhub_records";
  }

  // - Strong shortcut triggers (any language) -> agent
  if (shortcutLatin || shortcutZH || shortcutTA) return "healthhub_records";

  // Otherwise -> chat
  return "chat";
}

/* ------------------------- Thread handling (auto or manual) ------------------------- */

async function loadStoredThread() {
  const { chat_thread_id, chat_thread_started } = await chrome.storage.local.get([
    "chat_thread_id",
    "chat_thread_started",
  ]);
  return { chat_thread_id, chat_thread_started };
}

async function saveStoredThread(id) {
  const started = new Date().toISOString();
  await chrome.storage.local.set({ chat_thread_id: id, chat_thread_started: started });
  return started;
}

async function resolveThreadIdFromUI() {
  const auto = document.getElementById("auto_thread").checked;
  const input = document.getElementById("thread_id").value.trim();

  if (!auto) {
    if (input.length === 0) {
      document.getElementById("thread_id").focus();
      throw new Error("Please enter a custom thread id or turn on Auto.");
    }
    return { thread_id: input, started: "(manual override)" };
  }

  const { chat_thread_id, chat_thread_started } = await loadStoredThread();
  if (chat_thread_id) return { thread_id: chat_thread_id, started: chat_thread_started || "—" };

  const id = crypto.randomUUID();
  const started = await saveStoredThread(id);
  return { thread_id: id, started };
}

function setThreadUiEnabled() {
  const auto = document.getElementById("auto_thread").checked;
  const input = document.getElementById("thread_id");
  input.disabled = auto;
}

async function initThreadUi() {
  document.getElementById("auto_thread").addEventListener("change", () => {
    setThreadUiEnabled();
    refreshThreadInfoPanel().catch(() => {});
  });

  const { chat_thread_id } = await loadStoredThread();
  const autoBox = document.getElementById("auto_thread");
  const input = document.getElementById("thread_id");

  if (chat_thread_id) {
    autoBox.checked = true;
    input.value = ""; // auto mode
  } else {
    autoBox.checked = true;
    input.value = "";
  }
  setThreadUiEnabled();
  await refreshThreadInfoPanel();
}

/* ------------------------- Info panel ------------------------- */

let _localMsgCount = 0;

async function refreshThreadInfoPanel(lastReplyText) {
  const auto = document.getElementById("auto_thread").checked;
  const input = document.getElementById("thread_id").value.trim();
  let threadId = "(manual required)";
  let started = "—";

  if (!auto && input) {
    threadId = input;
    started = "(manual override)";
  } else if (auto) {
    const { chat_thread_id, chat_thread_started } = await loadStoredThread();
    if (chat_thread_id) {
      threadId = chat_thread_id;
      started = chat_thread_started || "—";
    } else {
      threadId = "(will be created on first use)";
    }
  }

  const $thread = document.getElementById("info_thread");
  const $started = document.getElementById("info_started");
  const $count = document.getElementById("info_msgcount");
  const $last = document.getElementById("info_last");
  if ($thread) $thread.textContent = threadId;
  if ($started) $started.textContent = started;
  if ($count) $count.textContent = String(_localMsgCount);
  if ($last && typeof lastReplyText === "string" && lastReplyText.trim()) {
    $last.textContent = lastReplyText.trim().slice(0, 200);
  }
}

/* ------------------------- Health pills ------------------------- */

async function pingHealth(url, elId) {
  const el = document.getElementById(elId);
  try {
    const r = await fetch(`${url}/health`, { method: "GET" });
    const ok = r.ok;
    if (el) {
      el.textContent = (elId === "agent_health" ? "agent" : "chat") + ": " + (ok ? "ok" : "down");
      el.className = "pill " + (ok ? "ok" : "bad");
    }
  } catch {
    if (el) {
      el.textContent = (elId === "agent_health" ? "agent" : "chat") + ": down";
      el.className = "pill bad";
    }
  }
}

/* ------------------------- Background snapshots ------------------------- */

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
  await postSnapshot(page_state).catch(() => {});

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
      // Speak only the backend-provided TTS string (if present) for DONE — in the user's prompt language
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
        await chrome.tabs.update(tabId, { url: lastFindTop.href }).catch(() => {});
        obs = { ok: true, data: { navigating: lastFindTop.href } };
      }
    }
    if (execTool === "click" && obs?.ok && !obs.data?.navigating && lastFindTop?.href) {
      log(`.. CLICK no nav; forcing nav to ${lastFindTop.href}`);
      await chrome.tabs.update(tabId, { url: lastFindTop.href }).catch(() => {});
      obs = { ok: true, data: { navigating: lastFindTop.href } };
    }
    if (execTool === "click" && obs?.ok && obs.data?.navigate_to) {
      await chrome.tabs.update(tabId, { url: obs.data.navigate_to }).catch(() => {});
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
  } catch {}

  return { ok: true };
}

/* ------------------------- Chat API ------------------------- */

async function sendSimpleChat(prompt) {
  const { thread_id } = await resolveThreadIdFromUI();
  const res = await fetch(`${CHAT_API}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id, prompt }),
  });
  if (!res.ok) throw new Error(`chat/simple HTTP ${res.status}`);
  return res.json(); // { thread_id, reply }
}

/* ------------------------- Main: INTENT-ROUTED handler ------------------------- */

async function onRun() {
  const tab = await getActiveTab();
  if (!tab) return log("No active tab");

  // Read goal from textbox
  const rawGoal = (document.getElementById("goal")?.value || "").trim() || "view lab results";
  const goal = sanitizeGoal(rawGoal);

  // Decide routing BEFORE touching tabs
  const intent = decideIntent(rawGoal); // pass raw for multilingual detection

  // Preferred TTS voice (used only for /speak)
  PREFERRED_LANG = detectLangFromPrompt(rawGoal);

  if (intent === "healthhub_records") {
    // === Agent path: only for HealthHub records ===
    await chrome.runtime.sendMessage({ type: "START_TRACK_TAB", tabId: tab.id }).catch(() => {});

    await chrome.tabs.update(tab.id, { url: "https://www.healthhub.sg/" });
    await waitForTabNavigation(tab.id);

    await injectTools(tab.id);
    if (!/^https?:\/\//.test(tab.url || "")) return log("Open a normal webpage first.");
    const h = await fetch(`${API}/health`).then(r => r.json()).catch(e => ({ error: String(e) }));
    log("health: " + JSON.stringify(h));
    if (!h || h.ok !== true) return log("Backend not healthy");

    log("Sending to backend (one-shot): initiating pass 1 (navigate)");
    await runOnce({ tabId: tab.id, goal, label: "pass1:navigate" });

    await injectTools(tab.id);
    await runTool(tab.id, "wait_for_idle", { quietMs: 700, timeout: 8000 });
    const seeded = await seedBridgeWithCurrent(tab.id);

    const needLogin = looksLikeSingpass(seeded) || !looksLoggedIn(seeded);
    if (needLogin) {
      log("*** Please log in with Singpass in the tab, then click ‘Run Agent’ again.");
      _localMsgCount += 1;
      await refreshThreadInfoPanel();
      return;
    }

    const nowUrl = (seeded?.url || "").toLowerCase();
    const onLabPage = nowUrl.includes(LAB_URL_TOKEN);
    log(onLabPage
      ? "Auto two-step: detected lab page. Starting pass 2 (read/extract)…"
      : "Auto two-step: running pass 2; router will choose the correct reader…");

    await runOnce({ tabId: tab.id, goal, label: "pass2:read" });

    _localMsgCount += 1;
    await refreshThreadInfoPanel();
  } else {
    // === Simple chat path: do NOT call agent/run ===
    try {
      const r = await sendSimpleChat(rawGoal);
      log(`** Chat reply: ${r.reply}`);
      speakText(r.reply); // optional TTS in prompt language

      _localMsgCount += 1;
      await refreshThreadInfoPanel(r.reply);
    } catch (e) {
      log(`Chat error: ${e.message || e}`);
    }
  }
}

/* ------------------------- DOM Ready ------------------------- */

document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("run").addEventListener("click", onRun);
  await initThreadUi();
  // Show quick health on load
  pingHealth(API, "agent_health");
  pingHealth(CHAT_API, "chat_health");
});
