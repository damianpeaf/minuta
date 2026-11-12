// Isolated world: passes page.js messages to the service worker and the controls back to page.js.
window.addEventListener('message', (e) => {
  if (e.source !== window || !e.data || e.data.src !== 'minuta') return;
  try {
    chrome.runtime.sendMessage(e.data).catch(() => {});
  } catch (_) {
    // The extension was reloaded and this script is orphaned: Meet keeps working, just not recorded.
  }
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.src === 'minuta-ctl') window.postMessage(msg, '*');
});
