chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "scrape-page",
    title: "Scrape this page",
    contexts: ["all"]
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "scrape-page") {
    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        // Collect all text
        const bodyText = document.body.innerText;
        // Collect all links
        const links = Array.from(document.querySelectorAll('a')).map(a => ({
          text: a.innerText,
          href: a.href
        }));
        // Collect all images
        const images = Array.from(document.querySelectorAll('img')).map(img => img.src);

        // Put everything into an object
        const pageData = {
          url: window.location.href,
          title: document.title,
          bodyText: bodyText,
          links: links,
          images: images,
        };

        // Option 1: Download as JSON file
        const blob = new Blob([JSON.stringify(pageData, null, 2)], {type: 'application/json'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = (document.title.replace(/\s+/g, '_').substring(0, 50) || 'scraped') + '.json';
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);

        // Option 2: Print in console (comment out above block if you want)
        // console.log(pageData);

        // Option 3: POST to your API (uncomment below, edit API URL)
        /*
        fetch("https://your-backend-url/scrape", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(pageData)
        });
        */
      }
    });
  }
});
