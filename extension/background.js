// Relay to the local minuta server; owns the toolbar icon and the popup.
const DEFAULT_PORT = 47321;
const TOKEN_HEADER = 'X-Minuta-Token';
const MAX_PENDING = 2200; // ~3 h of audio kept while the server does not answer (~120 MB)
const TAB_STALE_MS = 20000;
// A page problem must last this long before it paints the icon or notifies: while starting or
// switching state Meet goes through odd moments (mic button not there yet) that are not failures.
const GRACE_MS = 4000;

let settings = { port: DEFAULT_PORT, token: null };
let queue = Promise.resolve();
let pending = []; // messages that could not be delivered
let dropped = 0;
let serverOk = true;
let authOk = true; // false when the server refuses our token: the extension must be paired
let latest = null; // last finished meeting, for the popup
// Recording mode from the minuta app (auto | ask | manual); null until it answered.
let serverMode = null;
let modeKnown = false; // the app was asked at least once since the service worker started
const MODES = ['auto', 'ask', 'manual'];
const asked = new Map(); // tabId -> notification id of the open "Record this call?" question
const tabs = new Map(); // tabId -> { s: last page state, seen }
const ports = new Set();
const notified = new Set(); // notifications already shown: `${session}:${key}`
const since = new Map(); // page problem -> when it appeared
let lastIcon = '';
let loaded = false; // settings read from storage: until then "not paired" is unknown, not a problem

const ready = chrome.storage.local.get(['port', 'token']).then((v) => {
  settings = { port: Number(v.port) || DEFAULT_PORT, token: v.token || null };
}).catch(() => {}).finally(() => { loaded = true; });

const base = () => `http://127.0.0.1:${settings.port}`;
const paired = () => !!settings.token;

// --- State -------------------------------------------------------------------
function target() {
  let best = null;
  for (const [tabId, t] of tabs) {
    if (Date.now() - t.seen > TAB_STALE_MS) { tabs.delete(tabId); continue; }
    const rank = t.s.recording ? 3 : t.s.inCall ? 2 : 1;
    if (!best || rank > best.rank || (rank === best.rank && t.seen > best.t.seen)) best = { tabId, t, rank };
  }
  return best;
}

// Problems by severity. `serious` paints the icon amber and shows a browser notification.
function problems(s) {
  const out = [];
  if (loaded && (!paired() || !authOk)) {
    out.push({ key: 'pairing', serious: true,
      text: s && s.recording ? 'Not paired with minuta: audio is kept in memory. Open this popup and enter the pairing code.'
        : 'Not paired with minuta. Run `minuta pair` and enter the code here.' });
  } else if (!serverOk) {
    out.push({ key: 'server', serious: true,
      text: s && s.recording ? 'The minuta app is not answering: audio is kept in memory until it is back.'
        : 'The minuta app is not answering.' });
  }
  if (dropped) out.push({ key: 'lost', serious: true, text: `${dropped} audio chunks were lost: the app was unreachable for too long.` });
  if (!s || !s.recording) return out;
  const page = [];
  if (s.ctxState && s.ctxState !== 'running') page.push({ key: 'context', serious: true, text: `Recording audio is ${s.ctxState}.` });
  if (s.captureError) page.push({ key: 'capture', serious: true, text: s.captureError });
  if (s.remoteBroken) page.push({ key: 'remote', serious: true, text: "The others' voices are arriving but are not being recorded." });
  if (!s.localTrack) page.push({ key: 'mic', serious: true, text: 'No microphone found: your voice is not recorded.' });
  else if (s.muted === null) page.push({ key: 'mute', serious: true, text: "Cannot find Meet's microphone button: your voice is not recorded." });
  const t = Date.now();
  for (const key of [...since.keys()]) if (!page.some((p) => p.key === key)) since.delete(key);
  for (const p of page) {
    if (!since.has(p.key)) since.set(p.key, t);
    if (t - since.get(p.key) >= GRACE_MS) out.push(p);
  }
  if (s.remoteUnverified && !s.remoteBroken) {
    out.push({ key: 'silence', serious: false, text: 'Nothing heard from the others for a minute. If nobody is talking, that is fine.' });
  }
  return out;
}

// Without an answer from the app (not running, not paired) nothing is recorded by itself: as in ask.
const mode = () => (serverOk && authOk && paired() && MODES.includes(serverMode) ? serverMode : 'ask');

function view() {
  const best = target();
  const s = best ? best.t.s : null;
  return { serverOk, paired: paired() && authOk, port: settings.port, pending: pending.length,
    tabId: best ? best.tabId : null, state: s, problems: problems(s), latest, mode: mode(),
    modeFromApp: mode() === serverMode };
}

// --- Icon --------------------------------------------------------------------
function iconPaths(name) {
  return { 16: `icons/${name}-16.png`, 32: `icons/${name}-32.png`, 48: `icons/${name}-48.png`, 128: `icons/${name}-128.png` };
}

function paint() {
  const v = view();
  const s = v.state;
  const serious = v.problems.find((p) => p.serious);
  let icon = 'idle';
  let badge = '';
  let color = '#6b7280';
  let title = 'minuta: waiting for a Meet call';
  if (serious) {
    icon = 'warn'; badge = '!'; color = '#d97706';
    title = `minuta: ${serious.text}`;
  } else if (s && s.recording && s.paused) {
    icon = 'paused'; badge = 'II'; color = '#475569';
    title = 'minuta: paused';
  } else if (s && s.recording) {
    icon = 'rec'; badge = 'REC'; color = '#dc2626';
    title = 'minuta: recording';
  } else if (s && s.awaiting) {
    icon = 'paused'; badge = '?'; color = '#2563eb';
    title = 'minuta: record this call? Choose in the notification or here';
  } else if (s && s.skipped) {
    title = 'minuta: this call is not being recorded';
  } else if (s && s.manualIdle) {
    title = 'minuta: not recording (manual mode). Click to record this call';
  }
  const key = [icon, badge, title].join('|');
  if (key === lastIcon) return;
  lastIcon = key;
  chrome.action.setIcon({ path: iconPaths(icon) }).catch(() => {});
  chrome.action.setBadgeText({ text: badge });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

function notify(v) {
  const session = (v.state && v.state.session) || 'general';
  for (const p of v.problems) {
    const id = `${session}:${p.key}`;
    if (!p.serious || notified.has(id)) continue;
    notified.add(id);
    chrome.notifications.create(id, {
      type: 'basic', iconUrl: 'icons/warn-128.png', title: 'minuta', message: p.text, priority: 2,
    }, () => void chrome.runtime.lastError);
  }
  // A problem that went away may notify again if it comes back.
  const active = new Set(v.problems.filter((p) => p.serious).map((p) => `${session}:${p.key}`));
  for (const id of [...notified]) if (id.startsWith(`${session}:`) && !active.has(id)) notified.delete(id);
}

// Ask mode: one notification per call with Record / Don't record. No answer: nothing is recorded.
function askToRecord() {
  if (!modeKnown) return;
  for (const [tabId, t] of tabs) {
    const id = asked.get(tabId);
    const waiting = !!(t.s && t.s.awaiting && t.s.mode === 'ask');
    if (waiting && !id) {
      const nid = `ask:${tabId}:${Date.now()}`;
      asked.set(tabId, nid);
      chrome.notifications.create(nid, {
        type: 'basic', iconUrl: 'icons/idle-128.png', title: 'minuta: record this call?',
        message: `${t.s.title || t.s.code || 'Google Meet'}. Nothing is recorded until you choose Record.`,
        buttons: [{ title: 'Record' }, { title: "Don't record" }], requireInteraction: true, priority: 2,
      }, () => void chrome.runtime.lastError);
    } else if (!waiting && id) {
      asked.delete(tabId);
      chrome.notifications.clear(id, () => void chrome.runtime.lastError);
    }
  }
}

chrome.notifications.onButtonClicked.addListener((id, index) => {
  for (const [tabId, nid] of asked) {
    if (nid !== id) continue;
    sendCtl(tabId, index === 0 ? 'record' : 'skip');
    asked.delete(tabId);
    chrome.notifications.clear(id, () => void chrome.runtime.lastError);
  }
});

// Each Meet tab learns the mode from here; a tab that has another one is told again.
function pushMode() {
  if (!modeKnown) return;
  const m = mode();
  for (const [tabId, t] of tabs) if (t.s && t.s.mode !== m) sendCtl(tabId, 'mode', { mode: m });
}

function refresh() {
  paint();
  const v = view();
  notify(v);
  pushMode();
  askToRecord();
  for (const port of ports) port.postMessage({ type: 'view', view: v });
}

// --- Server --------------------------------------------------------------------
class AuthError extends Error {}

async function send(msg) {
  if (!paired()) throw new AuthError('not paired');
  const res = await fetch(`${base()}/${msg.type}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', [TOKEN_HEADER]: settings.token },
    body: JSON.stringify(msg),
  });
  if (res.status === 401 || res.status === 403) throw new AuthError(`HTTP ${res.status}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

function enqueue(msg) {
  queue = queue.then(async () => {
    await ready;
    const batch = [...pending, msg];
    pending = [];
    const before = `${serverOk}|${authOk}`;
    for (let i = 0; i < batch.length; i++) {
      try {
        await send(batch[i]);
        serverOk = true;
        authOk = true;
      } catch (e) {
        if (e instanceof AuthError) { authOk = false; serverOk = true; } else serverOk = false;
        pending = batch.slice(i).filter((m) => m.type !== 'heartbeat' && m.type !== 'ping');
        if (pending.length > MAX_PENDING) {
          dropped += pending.length - MAX_PENDING;
          pending = pending.slice(pending.length - MAX_PENDING);
        }
        break;
      }
    }
    if (serverOk && authOk) dropped = 0;
    if (before !== `${serverOk}|${authOk}`) refresh();
  });
  return queue;
}

async function getJson(path) {
  const res = await fetch(`${base()}${path}`, { headers: settings.token ? { [TOKEN_HEADER]: settings.token } : {} });
  if (res.status === 401 || res.status === 403) throw new AuthError(`HTTP ${res.status}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function health() {
  await ready;
  try {
    const h = await getJson('/health');
    serverOk = !!(h && h.app === 'minuta');
    if (serverOk && paired()) {
      enqueue({ src: 'minuta', type: 'ping' }); // also flushes anything pending
      try {
        const st = await getJson('/status');
        latest = st.latest || null;
        serverMode = st.recording_mode || null;
        authOk = true;
      } catch (e) {
        if (e instanceof AuthError) authOk = false;
        serverMode = null;
      }
    } else {
      serverMode = null;
    }
  } catch (_) {
    serverOk = false;
    serverMode = null;
  }
  modeKnown = true;
  refresh();
}

async function setMode(m) {
  if (!MODES.includes(m)) return { ok: false, error: 'unknown mode' };
  try {
    const res = await fetch(`${base()}/mode`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', [TOKEN_HEADER]: settings.token || '' },
      body: JSON.stringify({ mode: m }),
    });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    serverMode = (await res.json()).recording_mode || m;
    refresh();
    return { ok: true };
  } catch (_) {
    return { ok: false, error: 'minuta is not answering' };
  }
}

async function pair(code, port) {
  await ready;
  const p = Number(port) || settings.port;
  try {
    const res = await fetch(`http://127.0.0.1:${p}/pair`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.token) return { ok: false, error: body.error || `HTTP ${res.status}` };
    settings = { port: p, token: body.token };
    await chrome.storage.local.set(settings);
    authOk = true;
    await health();
    return { ok: true };
  } catch (_) {
    return { ok: false, error: `minuta is not answering on port ${p}` };
  }
}

async function setPort(port) {
  const p = Number(port);
  if (!Number.isInteger(p) || p < 1024 || p > 65535) return { ok: false, error: 'invalid port' };
  settings = { ...settings, port: p };
  await chrome.storage.local.set({ port: p });
  await health();
  return { ok: true };
}

// --- Messages from Meet tabs ---------------------------------------------------
const STATE_TYPES = new Set(['heartbeat', 'state']);

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || msg.src !== 'minuta') return;
  const tabId = sender.tab && sender.tab.id;
  if (msg.type === 'level') {
    for (const port of ports) port.postMessage({ type: 'level', tabId, level: msg });
    return;
  }
  if (tabId != null) {
    const t = tabs.get(tabId) || { s: {}, seen: 0 };
    if (STATE_TYPES.has(msg.type)) t.s = msg;
    if (msg.type === 'start') t.s = { ...t.s, recording: true, session: msg.session, title: msg.title, paused: false, elapsedMs: 0 };
    if (msg.type === 'end' || msg.type === 'discard') t.s = { ...t.s, recording: false, paused: false };
    t.seen = Date.now();
    tabs.set(tabId, t);
  }
  if (msg.type !== 'state') enqueue(msg);
  // The server finishes the meeting a moment later: refresh "Last" for the popup.
  if (msg.type === 'end') setTimeout(() => health(), 3000);
  refresh();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabs.delete(tabId)) refresh();
  const id = asked.get(tabId);
  if (id) { asked.delete(tabId); chrome.notifications.clear(id, () => void chrome.runtime.lastError); }
});

// --- Popup ---------------------------------------------------------------------
let liveTimer = null;

function sendCtl(tabId, action, extra = {}) {
  if (tabId == null) return;
  chrome.tabs.sendMessage(tabId, { src: 'minuta-ctl', action, ...extra }).catch(() => {});
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'popup') return;
  ports.add(port);
  port.postMessage({ type: 'view', view: view() });
  const pulse = () => { const v = view(); if (v.state && v.state.recording) sendCtl(v.tabId, 'live'); };
  pulse();
  if (!liveTimer) liveTimer = setInterval(pulse, 1000);
  port.onMessage.addListener(async (m) => {
    if (!m) return;
    if (m.action === 'pair') {
      port.postMessage({ type: 'pair', result: await pair(String(m.code || ''), m.port) });
    } else if (m.action === 'port') {
      port.postMessage({ type: 'port', result: await setPort(m.port) });
    } else if (m.action === 'mode') {
      port.postMessage({ type: 'mode', result: await setMode(m.mode) });
    } else if (typeof m.action === 'string') {
      sendCtl(view().tabId, m.action);
    }
  });
  port.onDisconnect.addListener(() => {
    ports.delete(port);
    if (!ports.size && liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  });
  health();
});

chrome.alarms.create('health', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => health());
paint();
health();
