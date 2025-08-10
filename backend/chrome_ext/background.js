// background.js
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "scrape-page",
    title: "Scrape this page",
    contexts: ["all"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "scrape-page" || !tab?.id) return;

  // Run in the page context
  await chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: false }, // set true if sites use iframes
    world: "ISOLATED",
    func: async () => {
      // small helper
      const wait = (ms) => new Promise(r => setTimeout(r, ms));

      // 1) wait for SPA content to render
      if (document.readyState !== "complete") {
        await wait(1500);
      }
      await wait(1000); // extra settle time

      // 2) pick best title
      const ogTitle = document.querySelector('meta[property="og:title"]')?.content;
      const title = (document.title || ogTitle || "").trim();

      // 3) grab main text using heuristics
      const pickText = (el) => (el ? el.innerText.trim() : "");
      const candidates = [
        "main",
        "article",
        "[role='main']",
        ".content",
        ".post",
        ".article",
        ".entry-content",
        "#content"
      ];

      let bodyText = "";
      for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (el && el.innerText && el.innerText.trim().length > 300) {
          bodyText = pickText(el);
          break;
        }
      }
      if (!bodyText) {
        bodyText = pickText(document.body);
      }

      // 4) collect links and images (dedup + absolute URLs)
      const toAbs = (u) => {
        try { return new URL(u, location.href).href; } catch { return u; }
      };

      const links = Array.from(document.querySelectorAll("a"))
        .map(a => ({ text: (a.innerText || "").trim(), href: toAbs(a.getAttribute("href") || "") }))
        .filter(l => l.href)
        .slice(0, 500);

      const images = Array.from(document.querySelectorAll("img"))
        .map(img => toAbs(img.getAttribute("src") || ""))
        .filter(Boolean)
        .slice(0, 200);

      // 5) minimum quality threshold
      const MIN_LEN = 200; // tweak as you like
      if (!title && (!bodyText || bodyText.length < MIN_LEN)) {
        return { ok: false, reason: "Low-quality capture (empty/too short). Try after page fully loads." };
      }

      // 6) build payload
      const pageData = {
        url: window.location.href,
        title,
        bodyText,
        links,
        images
      };

      // 7) send to your API
      try {
        const res = await fetch("http://localhost:8002/scrape", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Api-Key": "dev-key-123"
          },
          body: JSON.stringify(pageData)
        });
        const json = await res.json();
        return { ok: res.ok, status: res.status, json };
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    }
  });
});
