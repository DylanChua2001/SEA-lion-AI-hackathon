const sleep = (ms) => new Promise(r=>setTimeout(r,ms));

function nodeToPath(el){
  if (!el) return null;
  if (el.id) return `#${CSS.escape(el.id)}`;
  const parts=[]; let n=el;
  while (n && n.nodeType===1 && parts.length<6){
    const name=n.tagName.toLowerCase();
    const idx = Array.from(n.parentElement?.children || []).indexOf(n)+1;
    parts.unshift(`${name}:nth-child(${idx})`);
    n=n.parentElement;
  }
  return parts.join(">");
}

function snapshot(){
  const buttons=[...document.querySelectorAll("a,button,[role='button']")]
    .slice(0,400).map(b=>({text:b.innerText.trim().slice(0,160), selector: nodeToPath(b)}));
  const inputs=[...document.querySelectorAll("input,textarea,select")]
    .slice(0,200).map(i=>({name:i.name||i.id||i.placeholder||i.ariaLabel||"", selector: nodeToPath(i)}));
  const raw_html = document.documentElement.outerHTML; // add this
  return { url: location.href, title: document.title, buttons, inputs, raw_html };
}

function extractContains(q){
  // supports a:contains('Text') or :contains("Text")
  const m = q.match(/:contains\((['"])(.*?)\1\)/i);
  return m ? m[2] : null;
}

async function findImpl({ query, max=10 }){
  const raw = (query || "").trim();
  const contains = extractContains(raw);
  let textQuery = contains || raw.replace(/^a:/i, "").trim(); // fallback to raw as text

  const all = [...document.querySelectorAll("a,button,[role='button']")];
  const scored = all.map(el => {
    const text = (el.innerText || "").trim();
    let score = 0;
    const t = text.toLowerCase();
    const q = (textQuery || "").toLowerCase();
    if (q && t.includes(q)) score += 10;
    if (/appointment/.test(t)) score += 3; // small bias towards appointments
    return { el, text, score };
  }).filter(x => x.score > 0);

  scored.sort((a,b)=>b.score - a.score);
  const top = scored.slice(0, max).map(x => ({ text: x.text, selector: nodeToPath(x.el) }));
  return { candidates: top };
}

async function clickImpl({ selector, text, query }){
  let el = selector ? document.querySelector(selector) : null;
  if (!el && (text || query)){
    const needle = (text || query || "").toLowerCase();
    el = [...document.querySelectorAll("a,button,[role='button']")]
      .find(e => (e.innerText || "").trim().toLowerCase().includes(needle));
  }
  if (!el) throw new Error("element not found");
  el.click();
  return { clicked: nodeToPath(el) };
}


async function typeImpl({ selector, value }){
  const el = document.querySelector(selector);
  if (!el) throw new Error("input not found");
  el.focus(); el.value=""; el.dispatchEvent(new Event("input",{bubbles:true}));
  el.value = value ?? "";
  el.dispatchEvent(new Event("input",{bubbles:true}));
  return { typed: value };
}

async function waitForImpl({ selector, timeout=15000 }){
  const t0 = performance.now();
  while (performance.now()-t0 < timeout){
    const el = document.querySelector(selector);
    if (el) return { ok: true };
    await sleep(200);
  }
  throw new Error("timeout");
}

async function navImpl({ url }){
  location.href = url;
  return { navigating: url };
}

const TOOL_IMPL = {
  get_page_state: async ()=> snapshot(),
  find: findImpl,
  click: clickImpl,
  type: typeImpl,
  wait_for: waitForImpl,
  nav: navImpl,
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg?.type === "RUN_TOOL"){
        const fn = TOOL_IMPL[msg.tool];
        if (!fn) return sendResponse({ ok:false, data:{error:"unknown tool"} });
        const data = await fn(msg.args || {});
        sendResponse({ ok:true, data });
      } else {
        sendResponse({ ok:false, data:{error:"bad request"} });
      }
    } catch (e){
      sendResponse({ ok:false, data:{error: e?.message || String(e)} });
    }
  })();
  return true;
});

console.log("[content] loaded");
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "PING") { sendResponse({ ok:true, pong:true }); return; }
  // … your existing RUN_TOOL handler …
  return true;
});
