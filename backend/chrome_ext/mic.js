(async () => {
  try {
    // Trigger the browser permission prompt in a normal tab
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    // Immediately stop tracks; we only needed the grant.
    stream.getTracks().forEach(t => t.stop());

    // Notify the extension that mic is granted
    chrome.runtime.sendMessage({ type: "MIC_PERMISSION_GRANTED" });
  } catch (e) {
    chrome.runtime.sendMessage({ type: "MIC_PERMISSION_DENIED", error: String(e && e.message || e) });
  } finally {
    // Close the tab after a short delay so the message can deliver
    setTimeout(() => window.close(), 300);
  }
})();
