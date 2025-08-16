// content.js (revised)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ────────────────────────────── Visibility helpers ────────────────────────── */

function isElementVisible(el) {
  if (!el || el.nodeType !== 1) return false;
  // Skip hidden via attribute
  if (el.hasAttribute("hidden")) return false;
  if (el.getAttribute("aria-hidden") === "true") return false;

  // Skip disabled controls
  if (el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true") return false;

  // Computed style checks
  const cs = window.getComputedStyle(el);
  if (!cs) return true;
  if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity || "1") === 0) return false;

  // Tiny offscreen elements are often not useful; keep conservative threshold
  const rect = el.getBoundingClientRect?.();
  if (!rect) return true;
  if (rect.width < 2 || rect.height < 2) return false;

  return true;
}

/* ────────────────────────────── Selectors & Snapshot ──────────────────────── */

function nodeToPath(el) {
  if (!el || el.nodeType !== 1) return null;
  if (el.id) return `#${CSS.escape(el.id)}`;

  const parts = [];
  let n = el;
  // Keep paths reasonably short; prefer structure over brittleness
  while (n && n.nodeType === 1 && parts.length < 6) {
    const name = n.tagName.toLowerCase();
    const parent = n.parentElement;
    if (!parent) {
      parts.unshift(name);
      break;
    }
    // nth-child is robust enough for mixed UIs
    const idx = Array.prototype.indexOf.call(parent.children, n) + 1;
    parts.unshift(`${name}:nth-child(${idx})`);
    n = parent;
  }
  return parts.join(">");
}

function safeText(el) {
  const t = (el?.innerText || el?.textContent || "").trim();
  return t.replace(/\s+/g, " ");
}

function dedupeBySignature(arr, makeSig) {
  const seen = new Set();
  const out = [];
  for (const item of arr) {
    const sig = makeSig(item);
    if (!sig || seen.has(sig)) continue;
    seen.add(sig);
    out.push(item);
  }
  return out;
}

function snapshot() {
  const q = (sel, limit = 200) => Array.from(document.querySelectorAll(sel)).slice(0, limit);
  const MAX_BUTTONS = 400;
  const MAX_LINKS = 400;
  const MAX_INPUTS = 200;
  const MAX_HEADINGS = 50;
  const MAX_TEXTS = 300;

  // Buttons (including anchors that behave like buttons)
  let buttons = q("a,button,[role='button']", MAX_BUTTONS)
    .filter(isElementVisible)
    .map((b) => ({
      text: safeText(b).slice(0, 160),
      selector: nodeToPath(b),
    }))
    .filter((b) => b.selector && b.text);

  buttons = dedupeBySignature(buttons, (x) => `${x.text}|||${x.selector}`).slice(0, MAX_BUTTONS);

  // Links (true anchors)
  let links = q("a[href]", MAX_LINKS)
    .filter(isElementVisible)
    .map((a) => ({
      text: safeText(a).slice(0, 200),
      selector: nodeToPath(a),
      href: a.href,
    }))
    .filter((a) => a.selector && a.href);

  links = dedupeBySignature(links, (x) => `${x.text}|||${x.href}`).slice(0, MAX_LINKS);

  // Inputs
  let inputs = q("input,textarea,select", MAX_INPUTS)
    .filter(isElementVisible)
    .map((i) => ({
      name:
        i.name ||
        i.id ||
        i.placeholder ||
        i.getAttribute("aria-label") ||
        "",
      placeholder: i.placeholder || "",
      ariaLabel: i.getAttribute("aria-label") || "",
      selector: nodeToPath(i),
    }))
    .filter((i) => i.selector);

  inputs = dedupeBySignature(inputs, (x) => x.selector).slice(0, MAX_INPUTS);

  // Headings
  let headings = q("h1,h2,[role='heading']", MAX_HEADINGS)
    .filter(isElementVisible)
    .map((h) => ({
      text: safeText(h).slice(0, 200),
      selector: nodeToPath(h),
    }))
    .filter((h) => h.selector && h.text);

  headings = dedupeBySignature(headings, (x) => `${x.text}|||${x.selector}`).slice(0, MAX_HEADINGS);

  // Generic short texts (for report_name like “Full Blood Count”)
  // Collect from spans/divs/paragraphs/headings; keep only shortish, visible strings.
  let texts = Array.from(document.querySelectorAll("span,div,p,h1,h2,h3"))
    .filter(isElementVisible)
    .map((el) => safeText(el))
    .filter((t) => t && t.length <= 120) // short labels only
    .map((t) => ({ text: t.slice(0, 120) }));

  texts = dedupeBySignature(texts, (x) => x.text.toLowerCase()).slice(0, MAX_TEXTS);

  // Navigation helpers (optional but nice to have)
  let nav_links = q('nav a,[role="navigation"] a', 200)
    .filter(isElementVisible)
    .map((a) => ({
      text: safeText(a),
      selector: nodeToPath(a),
      href: a.href,
    }))
    .filter((a) => a.selector && a.href);
  nav_links = dedupeBySignature(nav_links, (x) => `${x.text}|||${x.href}`).slice(0, 200);

  let breadcrumbs = q('[aria-label*="breadcrumb" i] a', 20)
    .filter(isElementVisible)
    .map((a) => ({
      text: safeText(a),
      selector: nodeToPath(a),
      href: a.href,
    }))
    .filter((a) => a.selector && a.href);
  breadcrumbs = dedupeBySignature(breadcrumbs, (x) => `${x.text}|||${x.href}`).slice(0, 20);

  // IMPORTANT: Do NOT include full raw_html; it easily explodes token budgets.
  // If you really need it for debugging:
  // const raw_html = (window.__DEBUG_SNAPSHOT__ ? (document.documentElement?.outerHTML || "") : undefined);

  return {
    url: location.href,
    title: document.title,
    buttons,
    links,
    inputs,
    nav_links,
    breadcrumbs,
    headings,
    texts, // <-- enables report_name extraction in subgraph
    // raw_html,
  };
}

/* ────────────────────────────── Tools: find / click / type ─────────────────────── */

function extractContains(q) {
  // supports a:contains('Text') or :contains("Text")
  const m = (q || "").match(/:contains\((['"])(.*?)\1\)/i);
  return m ? m[2] : null;
}

function textCandidates(el) {
  const out = [];
  const t = safeText(el);
  if (t) out.push(t.toLowerCase());
  const aria = (el.getAttribute?.("aria-label") || "").trim();
  if (aria) out.push(aria.toLowerCase());
  const title = (el.getAttribute?.("title") || "").trim();
  if (title) out.push(title.toLowerCase());
  return out;
}

async function findImpl({ query, max = 10 }) {
  const raw = (query || "").trim();
  const contains = extractContains(raw);
  const textQuery = (contains || raw.replace(/^a:/i, "").trim()).toLowerCase();

  if (!textQuery) return { matches: [], total: 0 };

  const all = Array.from(document.querySelectorAll("a,button,[role='button']"))
    .filter(isElementVisible);

  const scored = [];

  for (const el of all) {
    const texts = textCandidates(el);
    if (!texts.length) continue;

    let score = 0;
    for (const t of texts) {
      if (t === textQuery) score += 20;       // exact
      if (t.includes(textQuery)) score += 10; // substring
      if (t.startsWith(textQuery)) score += 3;// prefix bias
    }

    // Nudge for common intents (tiny)
    const st = texts.join(" ");
    if (/\bappointment\b/i.test(st)) score += 2;
    if (/\blab\b|\breport\b|\bresult\b/i.test(st)) score += 2;

    if (score > 0) {
      scored.push({ el, text: safeText(el), score });
    }
  }

  scored.sort((a, b) => b.score - a.score || a.text.length - b.text.length);

  const top = scored.slice(0, Math.max(1, Math.min(50, max))).map((x) => {
    const el = x.el;
    return {
      text: x.text,
      selector: nodeToPath(el),
      href: el.tagName === "A" ? el.href : null,
    };
  });

  return { matches: top, total: top.length };
}

function robustClick(el) {
  if (!el || el.nodeType !== 1) return;
  try {
    el.scrollIntoView({ block: "center", inline: "center" });
  } catch {}
  const rect = el.getBoundingClientRect();
  const cx = rect.left + Math.max(1, Math.min(rect.width / 2, rect.width - 1));
  const cy = rect.top + Math.max(1, Math.min(rect.height / 2, rect.height - 1));
  // Fire a simple mouse sequence; el.click() as final fallback
  el.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: cx, clientY: cy }));
  el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, clientX: cx, clientY: cy, button: 0 }));
  el.dispatchEvent(new MouseEvent("mouseup",   { bubbles: true, clientX: cx, clientY: cy, button: 0 }));
  try { el.click(); } catch {}
}

async function clickImpl({ selector, text, query }) {
  let el = selector ? document.querySelector(selector) : null;

  if (!el && (text || query)) {
    const needle = (text || query || "").toLowerCase().trim();
    if (needle) {
      el = Array.from(document.querySelectorAll("a,button,[role='button']"))
        .filter(isElementVisible)
        .find((e) => {
          const t = safeText(e).toLowerCase();
          const aria = (e.getAttribute?.("aria-label") || "").toLowerCase();
          return t.includes(needle) || aria.includes(needle);
        });
    }
  }

  if (!el) {
    return { ok: false, selector: selector || null, error: "element not found" };
  }

  // Avoid clicking disabled/hidden controls
  const disabled = el.hasAttribute?.("disabled") || el.getAttribute?.("aria-disabled") === "true";
  const style = window.getComputedStyle?.(el);
  const hidden = style && (style.visibility === "hidden" || style.display === "none");
  if (disabled || hidden) {
    return { ok: false, selector: nodeToPath(el), error: "element disabled or hidden" };
  }

  robustClick(el);
  const href = el.tagName === "A" ? (el.href || null) : null;

  // If anchor opens in new tab, still return navigate_to to let the orchestrator handle navigation policy
  const navigating = !!href;

  return {
    ok: true,
    selector: nodeToPath(el),
    href,
    navigate_to: href,
    navigating,
  };
}

async function typeImpl({ selector, text, value }) {
  const val = text !== undefined ? text : value;
  const el = selector ? document.querySelector(selector) : null;
  if (!el) return { ok: false, selector, error: "input not found" };

  // Focus & set value with input/change events
  el.focus();
  try { el.value = ""; el.dispatchEvent(new Event("input", { bubbles: true })); } catch {}
  try { el.value = val ?? ""; el.dispatchEvent(new Event("input", { bubbles: true })); } catch {}
  try { el.dispatchEvent(new Event("change", { bubbles: true })); } catch {}
  return { ok: true, selector, typed: val ?? "" };
}

/* ────────────────────────────── Wait / Nav helpers ─────────────────────────── */

async function waitForImpl({ selector, timeout = 15000 }) {
  if (!selector) return { ok: false, selector, error: "missing selector" };
  const start = performance.now();
  while (performance.now() - start < Math.max(0, timeout)) {
    const el = document.querySelector(selector);
    if (el && isElementVisible(el)) return { ok: true, selector };
    await sleep(200);
  }
  return { ok: false, selector, error: "timeout" };
}

async function navImpl({ url }) {
  if (!url) return { navigating: null };
  location.href = url;
  return { navigating: url };
}

async function waitForLoadImpl({ timeout = 20000 }) {
  const start = performance.now();
  while (document.readyState !== "complete" && performance.now() - start < Math.max(0, timeout)) {
    await sleep(100);
  }
  return { state: document.readyState, url: location.href };
}

async function waitForIdleImpl({ quietMs = 600, timeout = 8000 }) {
  // crude "network idle": DOM complete + no innerHTML length changes for quietMs
  const tEnd = performance.now() + Math.max(0, timeout);
  let last = document.body?.innerHTML?.length || 0;
  while (performance.now() < tEnd) {
    await sleep(Math.max(100, quietMs));
    const now = document.body?.innerHTML?.length || 0;
    if (document.readyState === "complete" && now === last) return { idle: true };
    last = now;
  }
  return { idle: false };
}

async function backImpl() {
  history.back();
  return { navigating: true };
}

// Accept seconds or ms; clamp to <= 60s for safety
async function waitImpl({ ms, seconds }) {
  let durationMs = 500;
  if (typeof seconds === "number") {
    durationMs = Math.max(0, Math.min(60, seconds)) * 1000;
  } else if (typeof ms === "number") {
    durationMs = Math.max(0, ms);
  }
  await sleep(durationMs);
  return { waited: durationMs };
}

/* ────────────────────────────── Tool Dispatch ─────────────────────────────── */

const TOOL_IMPL = Object.freeze({
  get_page_state: async () => snapshot(),
  find: findImpl,
  click: clickImpl,
  type: typeImpl,
  wait_for: waitForImpl,
  wait: waitImpl, // accepts {seconds} or {ms}
  nav: navImpl,
  wait_for_load: waitForLoadImpl,
  wait_for_idle: waitForIdleImpl,
  back: backImpl,
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg?.type === "PING") {
        sendResponse({ ok: true, pong: true });
        return;
      }
      if (msg?.type === "RUN_TOOL") {
        const tool = msg.tool;
        const fn = TOOL_IMPL[tool];
        if (!fn) {
          sendResponse({ ok: false, data: { error: "unknown tool" } });
          return;
        }
        const data = await fn(msg.args || {});

        // Normalize returns so the popup has a consistent {ok,data} envelope
        if (tool === "click" || tool === "type" || tool === "wait_for") {
          if (data && data.ok === false) {
            sendResponse({ ok: false, data });
            return;
          }
          sendResponse({ ok: true, data });
          return;
        }
        sendResponse({ ok: true, data });
        return;
      }
      sendResponse({ ok: false, data: { error: "bad request" } });
    } catch (e) {
      const sel = (msg?.args && (msg.args.selector || null)) || null;
      sendResponse({ ok: false, data: { selector: sel, error: e?.message || String(e) } });
    }
  })();
  return true; // keep the message channel open for async
});

console.log("[content] loaded (revised)");
