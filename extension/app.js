// The minuta page: everything the `minuta` menu does, against the same local server. Every text
// comes from the server (src/minuta/i18n.py) and every action is an endpoint of src/minuta/api.py:
// this file only draws. Data is written with textContent, never as HTML.
const DEFAULT_PORT = 47321;
const TOKEN_HEADER = 'X-Minuta-Token';
const $ = (id) => document.getElementById(id);

let settings = { port: DEFAULT_PORT, token: null };
let strings = {};
let lang = 'en';
let destinationsData = null;
let formState = null; // { type, name, answers, fields }

// --- Texts -------------------------------------------------------------------------
// Python's str.format subset the strings use: {name}, {name!r}, {name:.2f}.
function t(key, vars = {}) {
  const text = strings[key];
  if (text === undefined) return key;
  return text.replace(/\{(\w+)(!r)?(?::\.(\d+)f)?\}/g, (m, name, repr, digits) => {
    if (!(name in vars)) return m;
    let v = vars[name];
    if (digits !== undefined) v = Number(v).toFixed(Number(digits));
    return repr ? `'${v}'` : String(v);
  });
}

function translatePage() {
  document.documentElement.lang = lang;
  for (const node of document.querySelectorAll('[data-t]')) {
    const key = node.dataset.t;
    if (strings[key] !== undefined) node.textContent = t(key);
  }
  document.title = 'minuta';
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'text') node.textContent = v;
    else if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function when(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(lang, { dateStyle: 'medium', timeStyle: 'short' }).format(d);
}

// Same shape as ui.duration in the terminal: '48 s', '12 min', '1 h 05 min'.
function duration(seconds) {
  if (!seconds) return '-';
  const s = Math.round(Number(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function notice(text, kind = '') {
  const n = $('notice');
  n.textContent = text || '';
  n.className = `notice ${kind}`;
  n.hidden = !text;
}

// --- Server --------------------------------------------------------------------------
class HttpError extends Error {
  constructor(status, body) {
    super((body && body.error) || `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
  }
}

async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (settings.token) headers[TOKEN_HEADER] = settings.token;
  let res;
  try {
    res = await fetch(`http://127.0.0.1:${settings.port}${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    showOffline();
    throw new HttpError(0, { error: 'offline' });
  }
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (res.status === 401 || (res.status === 403 && data.error === 'extension not paired')) {
    showPair();
    throw new HttpError(res.status, data);
  }
  if (res.status >= 400) throw new HttpError(res.status, data);
  data.httpStatus = res.status;
  return data;
}

async function loadStrings() {
  const res = await fetch(`http://127.0.0.1:${settings.port}/i18n`);
  const data = await res.json();
  strings = data.strings || {};
  lang = data.language || 'en';
  translatePage();
}

// --- Views -----------------------------------------------------------------------------
const VIEWS = ['meetings', 'meeting', 'destinations', 'settings', 'status'];
let pollTimer = null;

function hideAll() {
  for (const id of [...VIEWS, 'offline', 'pair']) $(id).hidden = true;
}

function showOffline() {
  hideAll();
  $('offline-text').textContent = strings['web.offline'] ? t('web.offline', { port: settings.port })
    : `Nothing answers on 127.0.0.1:${settings.port}. Start minuta with: minuta service start`;
  $('offline').hidden = false;
}

function showPair() {
  hideAll();
  $('pair').hidden = false;
  $('pair-code').focus();
}

async function route() {
  clearTimeout(pollTimer);
  notice('');
  const [name, arg] = (location.hash.slice(1) || 'meetings').split('/');
  const view = VIEWS.includes(name) ? name : 'meetings';
  hideAll();
  for (const a of document.querySelectorAll('#nav a')) {
    const current = a.dataset.section === view || (view === 'meeting' && a.dataset.section === 'meetings');
    if (current) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  $(view).hidden = false;
  try {
    if (view === 'meetings') await showMeetings();
    else if (view === 'meeting') await showMeeting(decodeURIComponent(arg || ''));
    else if (view === 'destinations') await showDestinations();
    else if (view === 'settings') await showSettings();
    else if (view === 'status') await showStatus();
  } catch (e) {
    if (e instanceof HttpError && (e.status === 0 || e.status === 401 || e.status === 403)) return;
    notice(e.message, 'error');
  }
}

// --- Meetings ----------------------------------------------------------------------------
function statusWord(status) {
  return status ? t(`meeting.status.${status}`) : '-';
}

async function showMeetings() {
  const all = $('show-discarded').checked ? '?all=1' : '';
  const data = await api('GET', `/meetings${all}`);
  const rows = (data.meetings || []).slice().reverse();
  const body = $('meetings-table').querySelector('tbody');
  body.replaceChildren(...rows.map((m) => {
    const failed = [];
    for (const [name, stages] of Object.entries(m.destinations || {})) {
      if (Object.values(stages).includes('failed')) failed.push(name);
    }
    let state = statusWord(m.status);
    if (failed.length) state += ` · ${t('list.failed_to', { names: failed.join(', ') })}`;
    const link = el('a', { href: `#meeting/${encodeURIComponent(m.id)}`, text: m.title || m.id });
    return el('tr', {},
      el('td', { text: when(m.started_at) }),
      el('td', {}, link),
      el('td', { text: duration(m.duration_seconds) }),
      el('td', { text: state, class: failed.length ? 'warn' : '' }),
      el('td', { text: m.transcript ? t('list.transcribed') : t('list.not_transcribed'), class: m.transcript ? 'ok' : 'muted' }));
  }));
  $('meetings-table').hidden = !rows.length;
  $('meetings-empty').hidden = !!rows.length;
}

let currentMeeting = null;

async function showMeeting(id, keepProgress = false) {
  const m = await api('GET', `/meetings/${encodeURIComponent(id)}`);
  currentMeeting = m;
  $('meeting-title').textContent = m.title || m.id;
  const facts = [
    [t('list.col.when'), when(m.started_at)],
    [t('list.col.duration'), duration(m.duration_seconds)],
    [t('list.col.status'), statusWord(m.status)],
    [t('list.col.transcript'), m.transcript ? t('list.transcribed') : t('list.not_transcribed')],
  ];
  if (m.code) facts.push(['Meet', m.code]);
  if (m.warning) facts.push(['!', m.warning]);
  if (m.error) facts.push([t('retry.col.error'), m.error]);
  if (m.transcript_error && m.transcript_status === 'error') facts.push([t('retry.col.error'), m.transcript_error]);
  facts.push([t('status.data'), m.folder]);
  $('meeting-facts').replaceChildren(...facts.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  $('open-transcript').hidden = !m.files.length;
  $('open-result').textContent = '';

  const rows = [];
  for (const [stage, list] of Object.entries(m.deliveries)) {
    for (const d of list) {
      const good = d.status === 'ok' || d.status === 'skipped';
      rows.push(el('tr', {},
        el('td', { text: d.destination }),
        el('td', { text: t(`stage.${stage}`) }),
        el('td', { text: d.status + (d.error ? `: ${d.error}` : ''), class: good ? 'ok' : 'warn' }),
        el('td', { text: d.where || '-' })));
    }
  }
  $('deliveries-table').querySelector('tbody').replaceChildren(...rows);
  $('deliveries-table').hidden = !rows.length;
  $('deliveries-empty').hidden = !!rows.length;

  const tr = m.transcript_text;
  $('transcript').replaceChildren(...(tr ? tr.turns : []).map((turn) => el('li', {},
    el('span', { class: 'time', text: clock(turn.start) }), ' ',
    el('strong', { text: turn.speaker }), ': ',
    el('span', { text: turn.text }))));
  $('transcript-empty').hidden = !!(tr && tr.turns.length);
  if (tr) {
    const engine = tr.engine || {};
    const model = String(engine.model || '').split('/').pop();
    let meta = `${t('summary.engine')}: ${engine.name || '-'}${model ? ` (${model})` : ''}`;
    if (tr.cost_usd) meta += ` · ${t('summary.cost')}: US$${Number(tr.cost_usd).toFixed(4)}`;
    $('transcript-meta').textContent = meta;
  } else {
    $('transcript-meta').textContent = '';
  }

  $('transcribe-form').hidden = !m.transcribable;
  $('transcribe').textContent = m.transcript ? t('web.transcribe_again') : t('web.transcribe');
  $('confirm-spend').hidden = true;
  if (!keepProgress) $('transcribe-progress').hidden = true;
  if (m.transcribable) await loadEngines(m.id);
  const running = (await api('GET', '/jobs')).jobs.find((j) => j.meeting === m.id && j.kind === 'transcribe'
    && (j.state === 'queued' || j.state === 'running'));
  if (running) followJob(running, $('transcribe-progress'), () => showMeeting(m.id, true));
}

function engineLabel(o) {
  const name = o.engine === 'local' ? 'local' : o.model;
  const model = o.engine === 'local' && o.model ? ` (${o.model})` : '';
  const takes = o.seconds ? `, ${t('transcribe.takes', { time: duration(o.seconds) })}` : '';
  const price = o.paid ? t('transcribe.up_to', { usd: o.usd }) : t('transcribe.free');
  const missing = o.available === false ? ` - ${t('transcribe.not_installed')}` : '';
  return `${name}${model}: ${price}${takes}${missing}`;
}

async function loadEngines(id) {
  const data = await api('GET', `/meetings/${encodeURIComponent(id)}/engines`);
  $('engine').replaceChildren(...data.engines.map((o, i) => el('option', {
    value: String(i), text: engineLabel(o), disabled: o.available === false,
  })));
  $('engine').engines = data.engines;
}

async function startTranscription(confirm = false) {
  const o = $('engine').engines[Number($('engine').value)];
  const body = { engine: o.engine, model: o.engine === 'vercel' ? o.model : null, confirm };
  $('confirm-spend').hidden = true;
  try {
    const data = await api('POST', `/meetings/${encodeURIComponent(currentMeeting.id)}/transcribe`, body);
    followJob(data.job, $('transcribe-progress'), () => showMeeting(currentMeeting.id, true));
  } catch (e) {
    if (e.status === 409 && e.body.needs_confirmation) {
      const est = e.body.plan.estimate;
      $('confirm-spend-text').textContent = `${t('transcribe.estimate', {
        engine: e.body.plan.model || e.body.plan.engine, minutes: est.seconds / 60, usd: est.usd,
      })} ${t('transcribe.confirm', { usd: est.usd })}`;
      $('confirm-spend').hidden = false;
      $('confirm-yes').focus();
    } else {
      notice(e.message, 'error');
    }
  }
}

// A job's progress: the steps already done, the current one with a bar, then the result.
function followJob(job, box, onDone) {
  box.hidden = false;
  const label = el('p', { class: 'job-label' });
  const bar = el('progress', { max: '1' });
  const done = el('ul', { class: 'job-steps' });
  const cancel = el('button', { type: 'button', text: t('web.cancel') });
  cancel.addEventListener('click', () => api('POST', `/jobs/${job.id}/cancel`).catch(() => {}));
  box.replaceChildren(done, label, bar, cancel);
  bar.setAttribute('aria-label', t('web.progress'));
  const draw = (j) => {
    done.replaceChildren(...(j.done || []).map((text) => el('li', { text })));
    let text = j.message || t('web.waiting');
    if (j.state === 'queued' && j.position) text = t('transcribe.queued', { n: j.position });
    label.textContent = text;
    if (j.fraction === null || j.fraction === undefined) bar.removeAttribute('value');
    else bar.value = j.fraction;
  };
  const tick = async () => {
    let j;
    try { j = (await api('GET', `/jobs/${job.id}`)).job; } catch (e) { return; }
    draw(j);
    if (j.state === 'done' || j.state === 'error' || j.state === 'cancelled') {
      cancel.remove();
      bar.remove();
      label.textContent = j.state === 'done' ? t('web.job_done')
        : j.state === 'cancelled' ? t('transcribe.job_cancelled') : t('transcribe.failed', { error: j.error || '' });
      label.className = `job-label ${j.state === 'done' ? 'ok' : 'warn'}`;
      if (onDone) setTimeout(() => onDone(j), j.state === 'done' ? 600 : 3000);
      return;
    }
    pollTimer = setTimeout(tick, 600);
  };
  draw(job);
  pollTimer = setTimeout(tick, 300);
}

async function openMeeting(what) {
  const r = await api('POST', `/meetings/${encodeURIComponent(currentMeeting.id)}/open`, { what });
  $('open-result').textContent = r.opened ? t('summary.opened', { path: r.path }) : t('summary.cannot_open', { path: r.path });
}

async function retryAll() {
  const data = await api('POST', '/retry', {});
  if (!data.meetings) { notice(t('retry.none')); return; }
  followJob(data.job, $('retry-progress'), (j) => {
    if (j.state === 'done') {
      const rows = (j.result && j.result.rows) || [];
      const bad = rows.filter((r) => r.status !== 'ok' && r.status !== 'skipped');
      notice(bad.length ? bad.map((r) => `${r.title} · ${r.destination}: ${r.error || r.status}`).join(' | ') : t('web.job_done'),
        bad.length ? 'error' : '');
    }
    showMeetings();
  });
}

// --- Destinations ---------------------------------------------------------------------------
async function showDestinations() {
  const data = await api('GET', '/destinations');
  destinationsData = data;
  const body = $('destinations-table').querySelector('tbody');
  body.replaceChildren(...data.configured.map((d) => {
    const secrets = Object.entries(d.secrets || {}).map(([k, v]) => t(v ? 'web.secret_set' : 'web.secret_unset', { name: k }));
    const test = el('button', { type: 'button', text: t('web.test') });
    test.addEventListener('click', () => testDestination(d, false));
    const live = el('button', { type: 'button', text: t('web.test_live') });
    live.addEventListener('click', () => testDestination(d, true));
    const toggle = el('button', { type: 'button', text: d.enabled ? t('web.disable') : t('web.enable') });
    toggle.addEventListener('click', () => destinationAction(d.name, d.enabled ? 'disable' : 'enable'));
    const remove = el('button', { type: 'button', class: 'danger', text: t('web.remove') });
    remove.addEventListener('click', () => {
      if (window.confirm(t('web.remove_confirm', { name: d.name }))) destinationAction(d.name, 'remove');
    });
    const where = d.target || (d.type === 'local' ? t('dest.data_folder') : '-');
    return el('tr', {},
      el('td', { text: d.name }), el('td', { text: d.type }),
      el('td', { text: d.enabled ? t('dest.enabled') : t('dest.disabled'), class: d.enabled ? 'ok' : 'muted' }),
      el('td', { text: d.send.map((p) => t(`part.${p}`)).join(', ') }),
      el('td', { text: where }),
      el('td', { text: secrets.join(', ') || '-' }),
      el('td', { class: 'row-actions' }, test, live, toggle, remove));
  }));
  $('destinations-table').hidden = !data.configured.length;
  $('destinations-empty').hidden = !!data.configured.length;
  const type = $('dest-type').value;
  $('dest-type').replaceChildren(...data.types.map((x) => el('option', { value: x.type, text: `${x.type}: ${x.label}` })));
  if (type) $('dest-type').value = type;
  if (!$('dest-name').value) $('dest-name').value = $('dest-type').value;
  checkReplaces();
}

function checkReplaces() {
  const name = $('dest-name').value.trim();
  const exists = destinationsData && destinationsData.configured.some((d) => d.name === name);
  $('dest-replaces').hidden = !exists;
  $('dest-replaces').textContent = exists ? t('web.replaces', { name }) : '';
}

async function testDestination(d, live) {
  const name = d.name;
  $('dest-result').textContent = t(live ? 'dest.live' : 'dest.testing', { name });
  $('dest-result').className = '';
  const r = await api('POST', `/destinations/${encodeURIComponent(name)}/test`, { live });
  let text = `${name}: ${r.message}`;
  if (r.live) {
    if (!r.live.supported) text += ` · ${t('dest.no_live', { name, kind: d.type })}`;
    else if (r.live.ok) text += ` · ${t('dest.live_ok', { name, url: r.live.url || '' }).trim()}`;
    else text += ` · ${t('dest.live_failed', { name, error: r.live.error })}`;
  }
  $('dest-result').textContent = text;
  $('dest-result').className = r.ok ? 'ok' : 'warn';
}

async function destinationAction(name, action) {
  await api('POST', `/destinations/${encodeURIComponent(name)}/${action}`, {});
  const key = { enable: 'dest.enable_done', disable: 'dest.disable_done', remove: 'dest.removed' }[action];
  $('dest-result').textContent = t(key, { name });
  $('dest-result').className = 'ok';
  await showDestinations();
}

function fieldControl(f, value) {
  const id = `f-${f.key}`;
  const describedBy = [];
  const wrap = el('div', { class: 'field' });
  if (f.help) {
    wrap.append(el('p', { class: 'hint', id: `${id}-help`, text: f.help }));
    describedBy.push(`${id}-help`);
  }
  let input;
  if (f.kind === 'bool') {
    input = el('input', { type: 'checkbox', id });
    input.checked = value !== undefined ? !!value : !!f.default;
    wrap.append(el('label', { class: 'check', for: id }, input, ' ', el('span', { text: f.label })));
  } else {
    wrap.append(el('label', { for: id, text: f.label + (f.required ? '' : ` (${t('web.optional')})`) }));
    if (f.kind === 'select') {
      const choices = f.choices.filter((c) => !c.terminal);
      input = el('select', { id }, ...choices.map((c) => el('option', { value: c.value, text: c.label })));
      const initial = value !== undefined ? value : f.default;
      if (choices.some((c) => c.value === initial)) input.value = initial;
    } else {
      input = el('input', {
        id, type: f.kind === 'secret' ? 'password' : 'text', autocomplete: 'off', required: f.required,
        spellcheck: 'false',
      });
      input.value = value !== undefined ? value : (f.default !== undefined && f.default !== null ? f.default : '');
    }
    wrap.append(input);
    if (f.terminal_hint && f.choices && f.choices.some((c) => c.terminal)) {
      wrap.append(el('p', { class: 'hint', id: `${id}-terminal`, text: f.terminal_hint }));
      describedBy.push(`${id}-terminal`);
    }
  }
  const error = el('p', { class: 'error', id: `${id}-error`, role: 'alert', hidden: true });
  wrap.append(error);
  describedBy.push(`${id}-error`);
  input.setAttribute('aria-describedby', describedBy.join(' '));
  input.dataset.key = f.key;
  input.dataset.kind = f.kind;
  return wrap;
}

function drawFields(step) {
  formState.fields = step.fields;
  $('dest-fields').replaceChildren(...step.fields.map((f) => fieldControl(f, formState.answers[f.key])));
  for (const [key, message] of Object.entries(step.errors || {})) {
    const err = $(`f-${key}-error`);
    if (err) { err.textContent = message; err.hidden = false; $(`f-${key}`).setAttribute('aria-invalid', 'true'); }
  }
  $('dest-form').hidden = false;
  $('dest-start').hidden = true;
  const firstBad = Object.keys(step.errors || {})[0];
  const focus = firstBad ? $(`f-${firstBad}`) : $('dest-fields').querySelector('input, select');
  if (focus) focus.focus();
}

async function formStep() {
  const step = await api('POST', '/destinations/form', {
    type: formState.type, name: formState.name, answers: formState.answers,
  });
  if (step.complete) return addDestination();
  drawFields(step);
  $('dest-submit').textContent = t('web.next');
}

async function addDestination() {
  $('dest-result').textContent = t('dest.checking', { name: formState.name });
  $('dest-result').className = '';
  try {
    const r = await api('POST', '/destinations', {
      type: formState.type, name: formState.name, answers: formState.answers, force: $('dest-force').checked,
    });
    $('dest-result').textContent = t('dest.added', { name: formState.name, message: r.message });
    $('dest-result').className = 'ok';
    resetDestinationForm();
    await showDestinations();
  } catch (e) {
    if (e.body && e.body.fields) { drawFields(e.body); $('dest-result').textContent = ''; return; }
    $('dest-result').textContent = t('dest.check_failed', { name: formState.name, message: e.message });
    $('dest-result').className = 'warn';
  }
}

function resetDestinationForm() {
  formState = null;
  $('dest-form').hidden = true;
  $('dest-start').hidden = false;
  $('dest-fields').replaceChildren();
  $('dest-force').checked = false;
  $('dest-name').value = '';
  if (destinationsData) $('dest-name').value = $('dest-type').value;
}

// --- Settings -----------------------------------------------------------------------------------
let loadedSettings = null;

async function showSettings() {
  const s = await api('GET', '/settings');
  loadedSettings = s;
  const fill = (id, options, value) => {
    $(id).replaceChildren(...options.map(([v, label]) => el('option', { value: v, text: label })));
    $(id).value = value;
  };
  fill('s-recording_mode', s.recording_modes.map((m) => [m, t(`mode.${m}`)]), s.recording_mode);
  fill('s-transcription_mode', s.transcription_modes.map((m) => [m, t(`setup.mode.${m}`)]), s.transcription_mode);
  fill('s-engine', s.engines.map((e) => [e, t(`setup.engine.${e}`)]), s.engine);
  fill('s-local_model', s.local_models.map((m) => [m, m]), s.local_model);
  fill('s-vercel_model', s.vercel_models.map((m) => [m.model, `${m.name} (${m.model}), ~US$${m.usd_per_hour.toFixed(2)}/h`]), s.vercel_model);
  fill('s-language', [['en', 'English'], ['es', 'Español']], s.language);
  $('s-max_usd').value = s.max_usd;
  $('s-name').value = s.name;
  $('s-others_name').value = s.others_name;
  $('s-api_key').value = '';
  $('s-api_key-hint').textContent = s.api_key_configured ? t('web.key_set') : t('web.key_unset');
  syncEngineFields();
}

function syncEngineFields() {
  const vercel = $('s-engine').value === 'vercel';
  for (const id of ['s-vercel_model', 's-max_usd', 's-api_key']) $(id).disabled = !vercel;
  $('s-local_model').disabled = vercel;
}

async function saveSettings(e) {
  e.preventDefault();
  const changes = {};
  for (const node of $('settings-form').querySelectorAll('[name]')) {
    if (node.disabled) continue;
    const key = node.name;
    let value = node.value;
    if (key === 'api_key') { if (value) changes.api_key = value; continue; }
    if (key === 'max_usd') value = Number(value);
    if (loadedSettings[key] !== value) changes[key] = value;
  }
  $('settings-result').className = '';
  $('settings-result').textContent = t('web.saving');
  try {
    await api('POST', '/settings', changes);
    if (changes.language) await loadStrings();
    await showSettings();
    $('settings-result').textContent = t('web.saved');
    $('settings-result').className = 'ok';
  } catch (err) {
    $('settings-result').textContent = err.message;
    $('settings-result').className = 'warn';
  }
}

// --- Status -------------------------------------------------------------------------------------
async function showStatus() {
  $('doctor-table').querySelector('tbody').replaceChildren(el('tr', {}, el('td', { colspan: '4', text: t('doctor.checking') })));
  const [st, doc] = await Promise.all([api('GET', '/app/status'), api('GET', '/doctor')]);
  const svc = st.service;
  const ext = st.extension;
  const m = st.meetings;
  const service = svc.running ? t('status.service_running') : svc.installed ? t('status.service_stopped') : t('status.service_missing');
  let extension = t('status.ext_unpaired');
  if (ext.paired) {
    extension = ext.last_seen_seconds_ago !== null
      ? t('status.ext_seen', { ago: t('ago', { what: duration(Math.max(1, ext.last_seen_seconds_ago)) }) }) : t('status.ext_paired');
  }
  const facts = [
    [t('status.version'), `minuta ${st.version}`],
    [t('status.service'), service],
    [t('status.server'), t('status.server_up', { port: st.server.port })],
    [t('status.extension'), extension],
    [t('status.mode'), t(`mode.${st.recording_mode}`)],
  ];
  if (st.recording_now.length) facts.push([t('status.recording'), t('status.recording_now', { n: st.recording_now.length })]);
  facts.push([t('status.meetings'), t('status.meetings_value', { total: m.total, pending: m.without_transcript })]);
  if (m.with_failed_delivery) facts.push([t('status.failed'), t('status.failed_value', { n: m.with_failed_delivery })]);
  facts.push([t('status.data'), st.data], [t('status.config'), st.config]);
  $('status-facts').replaceChildren(...facts.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  if (m.with_failed_delivery) {
    const b = el('button', { type: 'button', text: t('menu.retry') });
    b.addEventListener('click', () => { location.hash = '#meetings'; setTimeout(retryAll, 300); });
    $('status-facts').append(el('dt', {}), el('dd', {}, b));
  }

  $('doctor-table').querySelector('tbody').replaceChildren(...doc.checks.map((c) => {
    const detail = el('td', {}, el('span', { text: c.detail }));
    if (c.hint) detail.append(el('div', { class: 'hint', text: c.hint }));
    const action = el('td', {});
    if (c.fix) {
      const b = el('button', { type: 'button', text: t(`web.fix.${c.fix}`) });
      b.addEventListener('click', () => { if (c.fix === 'pair') showPair(); else location.hash = `#${c.fix}`; });
      action.append(b);
    }
    return el('tr', {},
      el('td', { text: t(`mark.${c.status}`), class: { ok: 'ok', warn: 'warn', fail: 'fail' }[c.status] }),
      el('td', { text: c.label }), detail, action);
  }));
}

// --- Start --------------------------------------------------------------------------------------
async function pair(e) {
  e.preventDefault();
  $('pair-error').hidden = true;
  try {
    const res = await fetch(`http://127.0.0.1:${settings.port}/pair`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: $('pair-code').value.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.token) throw new Error(data.error || `HTTP ${res.status}`);
    settings.token = data.token;
    await chrome.storage.local.set({ token: data.token, port: settings.port });
    $('pair-code').value = '';
    notice(t('setup.ext_ok'));
    route();
  } catch (err) {
    $('pair-error').textContent = t('web.pair_failed', { error: err.message });
    $('pair-error').hidden = false;
  }
}

async function start() {
  const v = await chrome.storage.local.get(['port', 'token']);
  settings = { port: Number(v.port) || DEFAULT_PORT, token: v.token || null };
  try {
    await loadStrings();
  } catch (e) {
    showOffline();
    return;
  }
  if (!settings.token) { showPair(); return; }
  route();
}

$('offline-retry').addEventListener('click', start);
$('pair-form').addEventListener('submit', pair);
$('show-discarded').addEventListener('change', () => showMeetings().catch((e) => notice(e.message, 'error')));
$('retry-all').addEventListener('click', () => retryAll().catch((e) => notice(e.message, 'error')));
$('open-folder').addEventListener('click', () => openMeeting('folder').catch((e) => notice(e.message, 'error')));
$('open-transcript').addEventListener('click', () => openMeeting('transcript').catch((e) => notice(e.message, 'error')));
$('transcribe-form').addEventListener('submit', (e) => { e.preventDefault(); startTranscription(false); });
$('confirm-yes').addEventListener('click', () => startTranscription(true));
$('confirm-no').addEventListener('click', () => { $('confirm-spend').hidden = true; notice(t('transcribe.cancelled')); });
$('dest-type').addEventListener('change', () => {
  if (!$('dest-name').value || (destinationsData && destinationsData.types.some((x) => x.type === $('dest-name').value))) {
    $('dest-name').value = $('dest-type').value;
  }
  checkReplaces();
});
$('dest-name').addEventListener('input', checkReplaces);
$('dest-start').addEventListener('submit', (e) => {
  e.preventDefault();
  formState = { type: $('dest-type').value, name: $('dest-name').value.trim(), answers: {}, fields: [] };
  $('dest-result').textContent = '';
  formStep().catch((err) => {
    $('dest-result').textContent = err.message;
    $('dest-result').className = 'warn';
    resetDestinationForm();
  });
});
$('dest-form').addEventListener('submit', (e) => {
  e.preventDefault();
  for (const input of $('dest-fields').querySelectorAll('[data-key]')) {
    formState.answers[input.dataset.key] = input.dataset.kind === 'bool' ? input.checked : input.value;
  }
  formStep().catch((err) => { $('dest-result').textContent = err.message; $('dest-result').className = 'warn'; });
});
$('dest-cancel').addEventListener('click', resetDestinationForm);
$('settings-form').addEventListener('submit', saveSettings);
$('s-engine').addEventListener('change', syncEngineFields);
$('status-refresh').addEventListener('click', () => showStatus().catch((e) => notice(e.message, 'error')));
window.addEventListener('hashchange', route);
chrome.storage.onChanged.addListener((changes) => {
  if (changes.port) settings.port = Number(changes.port.newValue) || DEFAULT_PORT;
  if (changes.token) settings.token = changes.token.newValue || null;
});
start();
