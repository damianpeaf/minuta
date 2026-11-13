// Toolbar popup: live state of the recording and its controls. Keeps nothing: everything comes
// from the service worker.
const $ = (id) => document.getElementById(id);
const port = chrome.runtime.connect({ name: 'popup' });

let view = null;
let elapsedBase = null; // { ms, at }
const shown = { local: 0, remote: 0 };

function fmtClock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(h ? 2 : 1, '0');
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// Peak from 0 to 1 -> bar width on a decibel scale (-60 dB = empty).
function width(peak) {
  if (!peak || peak <= 0) return 0;
  const db = 20 * Math.log10(Math.min(peak, 1));
  return Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
}

function setBar(which, peak, instant = false) {
  // Rises at once and falls slowly, like a VU meter; paused or muted empties it at once.
  const target = width(peak);
  shown[which] = instant || target >= shown[which] ? target : shown[which] * 0.7 + target * 0.3;
  $(`${which}-bar`).style.width = `${shown[which].toFixed(1)}%`;
}

function render() {
  const s = view && view.state;
  const recording = !!(s && s.recording);
  const serious = view ? view.problems.filter((p) => p.serious) : [];

  let status = 'Waiting for a Meet call';
  if (recording && s.paused) status = 'Paused';
  else if (recording) status = 'Recording';
  else if (s && s.skipped) status = 'This call is not being recorded';
  else if (s && s.awaiting) status = 'Record this call?';
  else if (s && s.manualIdle) status = 'Not recording: click Record this call';
  else if (s && s.inCall) status = 'In a call, about to record';
  $('status').textContent = status;
  $('meeting').textContent = s && (s.inCall || recording) ? (s.title || s.code || 'Google Meet') : '';
  $('dot').className = `dot${serious.length ? ' warn' : recording && !s.paused ? ' rec' : ''}`;

  $('problems').replaceChildren(...(view ? view.problems : []).map((p) => {
    const li = document.createElement('li');
    li.textContent = p.text;
    if (p.serious) li.className = 'serious';
    return li;
  }));

  $('pairing').hidden = !view || view.paired;
  $('meters').hidden = !recording;
  $('controls').hidden = !recording;
  $('skipped').hidden = !(s && (s.skipped || s.awaiting || s.manualIdle));
  $('skip').hidden = !(s && s.awaiting);
  if (view && document.activeElement !== $('mode')) $('mode').value = view.mode;
  $('mode').disabled = !(view && view.serverOk && view.paired);
  if (recording) {
    $('pause').textContent = s.paused ? 'Resume' : 'Pause';
    $('local-note').textContent = s.paused ? 'paused' : s.muted === true ? 'muted in Meet'
      : s.muted === null ? 'not recorded' : '';
    $('remote-note').textContent = s.paused ? 'paused' : s.remoteBroken ? 'not recorded' : '';
    const ms = s.elapsedMs || 0;
    if (elapsedBase === null || Math.abs(elapsedBase.ms + (Date.now() - elapsedBase.at) - ms) > 3000) {
      elapsedBase = { ms, at: Date.now() };
    }
    if (s.paused) { setBar('local', 0, true); setBar('remote', 0, true); }
  } else {
    elapsedBase = null;
    $('confirm').hidden = true;
    $('discard').hidden = false;
  }

  const last = view && view.latest;
  if (last) {
    let text = `Last: ${last.title || last.id}`;
    if (last.status === 'error') text += ' · could not be saved';
    else if (last.status !== 'ready') text += ' · saving…';
    else if (last.transcribed) text += ' · transcribed';
    if (last.warning) text += ` · ${last.warning}`;
    $('last').textContent = text;
  }

  tickClock();
  $('server').textContent = view && view.serverOk ? `minuta on port ${view.port}` : 'minuta is not answering';
  if (view && document.activeElement !== $('port')) $('port').value = String(view.port);
}

function tickClock() {
  $('clock').textContent = elapsedBase ? fmtClock(elapsedBase.ms + (Date.now() - elapsedBase.at)) : '';
}

port.onMessage.addListener((m) => {
  if (m.type === 'view') {
    view = m.view;
    render();
    // No live levels yet: show the peaks of the last heartbeat.
    const s = view.state;
    if (s && s.recording && !s.paused && s.localLevel != null) { setBar('local', s.localLevel); setBar('remote', s.remoteLevel); }
  } else if (m.type === 'level' && view && m.tabId === view.tabId) {
    const off = m.level.paused;
    setBar('local', off || m.level.muted !== false ? 0 : m.level.local, off || m.level.muted !== false);
    setBar('remote', off ? 0 : m.level.remote, off);
    elapsedBase = { ms: m.level.elapsedMs, at: Date.now() };
  } else if (m.type === 'pair') {
    $('pair').disabled = false;
    $('pair-error').hidden = m.result.ok;
    $('pair-error').textContent = m.result.ok ? '' : m.result.error;
    if (m.result.ok) $('code').value = '';
  } else if (m.type === 'mode') {
    $('mode-error').hidden = m.result.ok;
    $('mode-error').textContent = m.result.ok ? '' : m.result.error;
  } else if (m.type === 'port') {
    if (m.result.ok) $('settings').hidden = true;
  }
});

const act = (action, extra = {}) => port.postMessage({ action, ...extra });
$('pause').addEventListener('click', () => act(view && view.state && view.state.paused ? 'resume' : 'pause'));
$('finish').addEventListener('click', () => act('finish'));
$('record').addEventListener('click', () => act('record'));
$('skip').addEventListener('click', () => act('skip'));
$('mode').addEventListener('change', () => act('mode', { mode: $('mode').value }));
$('discard').addEventListener('click', () => { $('confirm').hidden = false; $('discard').hidden = true; });
$('discard-no').addEventListener('click', () => { $('confirm').hidden = true; $('discard').hidden = false; });
$('discard-yes').addEventListener('click', () => { act('discard'); $('confirm').hidden = true; $('discard').hidden = false; });
$('pair').addEventListener('click', () => {
  $('pair').disabled = true;
  act('pair', { code: $('code').value.trim(), port: $('port').value.trim() });
});
$('code').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('pair').click(); });
$('settings-toggle').addEventListener('click', () => { $('settings').hidden = !$('settings').hidden; });
$('save-port').addEventListener('click', () => act('port', { port: $('port').value.trim() }));

// Everything else (meetings, transcripts, destinations, settings) is on the minuta page.
$('open-app').addEventListener('click', () => {
  chrome.tabs.create({ url: chrome.runtime.getURL('app.html') });
  window.close();
});

setInterval(tickClock, 500);
