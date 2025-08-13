const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function nodeToPath(el) {
  if (!el) return null;
  if (el.id) return `#${CSS.escape(el.id)}`;
  const parts = [];
  let n = el;
  while (n && n.nodeType === 1 && parts.length < 6) {
    const name = n.tagName.toLowerCase();
    const idx = Array.from(n.parentElement?.children || []).indexOf(n) + 1;
    parts.unshift(`${name}:nth-child(${idx})`);
    n = n.parentElement;
  }
  return parts.join(">");
}

function snapshot() {
  const buttons = [...document.querySelectorAll("a,button,[role='button']")]
    .slice(0, 400).map(b => ({
      text: (b.innerText || "").trim().slice(0, 160),
      selector: nodeToPath(b)
    }));
  const links = [...document.querySelectorAll("a[href]")]
    .slice(0, 400).map(a => ({
      text: (a.innerText || "").trim().slice(0, 200),
      selector: nodeToPath(a),
      href: a.href
    }));
  const inputs = [...document.querySelectorAll("input,textarea,select")]
    .slice(0, 200).map(i => ({
      name: i.name || i.id || i.placeholder || i.ariaLabel || "",
      selector: nodeToPath(i)
    }));
  const raw_html = document.documentElement.outerHTML;
  const nav_links = [...document.querySelectorAll('nav a,[role="navigation"] a')].slice(0, 200)
    .map(a => ({ text: a.innerText.trim(), selector: nodeToPath(a), href: a.href }));
  const breadcrumbs = [...document.querySelectorAll('[aria-label*="breadcrumb" i] a')].slice(0, 20)
    .map(a => ({ text: a.innerText.trim(), selector: nodeToPath(a), href: a.href }));
  const headings = [...document.querySelectorAll('h1,h2,[role="heading"]')].slice(0, 50)
    .map(h => ({ text: (h.innerText || "").trim().slice(0, 200), selector: nodeToPath(h) }));
  return { url: location.href, title: document.title, buttons, links, inputs, nav_links, breadcrumbs, headings, raw_html };
}

function extractContains(q) {
  // supports a:contains('Text') or :contains("Text")
  const m = q.match(/:contains\((['"])(.*?)\1\)/i);
  return m ? m[2] : null;
}

async function findImpl({ query, max = 10 }) {
  const raw = (query || "").trim();
  const contains = extractContains(raw);
  let textQuery = contains || raw.replace(/^a:/i, "").trim();

  const all = [...document.querySelectorAll("a,button,[role='button']")];
  const scored = all.map(el => {
    const text = (el.innerText || "").trim();
    let score = 0;
    const t = text.toLowerCase();
    const q = (textQuery || "").toLowerCase();
    if (q && t.includes(q)) score += 10;
    if (/appointment/.test(t)) score += 3;
    return { el, text, score };
  }).filter(x => x.score > 0);

  scored.sort((a, b) => b.score - a.score);
  const top = scored.slice(0, max).map(x => ({
    text: x.text,
    selector: nodeToPath(x.el),
    href: (x.el.tagName === "A" && x.el.href) ? x.el.href : null
  }));

  // Align with backend: {matches,total}
  return { matches: top, total: top.length };
}

function robustClick(el) {
  try { el.scrollIntoView({ block: "center", inline: "center" }); } catch {}
  const rect = el.getBoundingClientRect();
  const cx = rect.left + Math.max(1, Math.min(rect.width / 2, rect.width - 1));
  const cy = rect.top + Math.max(1, Math.min(rect.height / 2, rect.height - 1));
  el.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: cx, clientY: cy }));
  el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, clientX: cx, clientY: cy, button: 0 }));
  el.dispatchEvent(new MouseEvent("mouseup",   { bubbles: true, clientX: cx, clientY: cy, button: 0 }));
  el.click();
}

async function clickImpl({ selector, text, query }) {
  let el = selector ? document.querySelector(selector) : null;
  if (!el && (text || query)) {
    const needle = (text || query || "").toLowerCase();
    el = [...document.querySelectorAll("a,button,[role='button']")]
      .find(e => (e.innerText || "").trim().toLowerCase().includes(needle));
  }
  if (!el) {
    // Include selector in error so the backend can detect "same selector failed twice"
    return { ok: false, selector: selector || null, error: "element not found" };
  }
  robustClick(el);
  const href = (el.tagName === "A" && el.href) ? el.href : null;
  return { ok: true, selector: nodeToPath(el), href, navigating: !!href };
}

async function typeImpl({ selector, text, value }) {
  // Accept both `text` (backend) and `value` (old callers)
  const val = (text !== undefined) ? text : value;
  const el = document.querySelector(selector);
  if (!el) return { ok: false, selector, error: "input not found" };
  el.focus();
  try { el.value = ""; el.dispatchEvent(new Event("input", { bubbles: true })); } catch {}
  try { el.value = val ?? ""; el.dispatchEvent(new Event("input", { bubbles: true })); } catch {}
  try { el.dispatchEvent(new Event("change", { bubbles: true })); } catch {}
  return { ok: true, selector, typed: val ?? "" };
}

async function waitForImpl({ selector, timeout = 15000 }) {
  const t0 = performance.now();
  while (performance.now() - t0 < timeout) {
    const el = document.querySelector(selector);
    if (el) return { ok: true, selector };
    await sleep(200);
  }
  return { ok: false, selector, error: "timeout" };
}

async function navImpl({ url }) {
  location.href = url;
  return { navigating: url };
}

async function waitForLoadImpl({ timeout = 20000 }) {
  const t0 = performance.now();
  while (document.readyState !== "complete" && performance.now() - t0 < timeout) {
    await sleep(100);
  }
  return { state: document.readyState, url: location.href };
}

async function waitForIdleImpl({ quietMs = 600, timeout = 8000 }) {
  // crude "network idle": wait until DOM is complete and nothing changes for quietMs
  const tEnd = performance.now() + timeout;
  let last = document.body?.innerHTML?.length || 0;
  while (performance.now() < tEnd) {
    await sleep(quietMs);
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

// Optional: some models emit "wait" (milliseconds)
async function waitImpl({ ms = 500 }) {
  await sleep(ms);
  return { waited: ms };
}

const TOOL_IMPL = {
  get_page_state: async () => snapshot(),
  find: findImpl,
  click: clickImpl,
  type: typeImpl,
  wait_for: waitForImpl,
  wait: waitImpl,              // NEW (optional)
  nav: navImpl,
  wait_for_load: waitForLoadImpl,
  wait_for_idle: waitForIdleImpl,
  back: backImpl,
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg?.type === "PING") {
        sendResponse({ ok: true, pong: true });
        return;
      }
      if (msg?.type === "RUN_TOOL") {
        const fn = TOOL_IMPL[msg.tool];
        if (!fn) { sendResponse({ ok: false, data: { error: "unknown tool" } }); return; }
        const data = await fn(msg.args || {});
        // Normalize click/type/wait_for returns to { ok, data: {...} }
        if (msg.tool === "click" || msg.tool === "type" || msg.tool === "wait_for") {
          if (data && data.ok === false) { sendResponse({ ok: false, data }); return; }
          sendResponse({ ok: true, data });
          return;
        }
        sendResponse({ ok: true, data });
      } else {
        sendResponse({ ok: false, data: { error: "bad request" } });
      }
    } catch (e) {
      // Include selector if present in args to help backend logs
      const sel = (msg?.args && (msg.args.selector || null)) || null;
      sendResponse({ ok: false, data: { selector: sel, error: e?.message || String(e) } });
    }
  })();
  return true;
});

console.log("[content] loaded");
