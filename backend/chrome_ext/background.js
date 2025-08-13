// background.js
chrome.runtime.onInstalled.addListener(() => {});

function htmlPreview(html, url) {
  try {
    const title = (html.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [,""])[1].trim();
    const mdesc = (html.match(/<meta[^>]+name=["']description["'][^>]*content=["']([^"']+)["'][^>]*>/i) || [,""])[1].trim();
    const snippet = (mdesc || html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 200)).trim();
    return { url, title, snippet };
  } catch {
    return { url, title: "", snippet: "" };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "SCOUT_URLS" && Array.isArray(msg.urls)) {
      try {
        const previews = [];
        for (const u of msg.urls.slice(0, 8)) {
          try {
            const r = await fetch(u, { method: "GET" });
            const html = await r.text();
            previews.push(htmlPreview(html, u));
          } catch {
            previews.push({ url: u, title: "", snippet: "" });
          }
        }
        sendResponse({ ok: true, data: previews });
      } catch (e) {
        sendResponse({ ok: false, data: [], error: String(e) });
      }
      return;
    }
    sendResponse({ ok: false, data: [], error: "bad request" });
  })();
  return true;
});
