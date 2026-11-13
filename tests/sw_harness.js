// Runs the real extension/background.js with a fake Chrome API. Each scenario is a function.
// node tests/sw_harness.js <path to background.js> <scenario>
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const [, , file, scenario] = process.argv;
const TOKEN = 'tok-123456789';

function load({ serverUp = true, stored = { port: 47321, token: TOKEN }, validToken = TOKEN, pairCode = '111-222',
  mode = 'auto' } = {}) {
  const h = {
    icon: null, badge: null, title: null, notifications: [], sent: [], ctl: [], ports: [], fetches: [],
    serverUp, validToken, mode, storage: { ...stored }, listeners: {}, cleared: [],
  };
  const event = (name) => ({ addListener: (f) => { h.listeners[name] = f; } });
  const chrome = {
    action: {
      setIcon: (o) => { h.icon = o.path[16]; return Promise.resolve(); },
      setBadgeText: (o) => { h.badge = o.text; },
      setBadgeBackgroundColor: () => {},
      setTitle: (o) => { h.title = o.title; },
    },
    notifications: {
      create: (id, o, cb) => { h.notifications.push({ id, ...o }); cb && cb(); },
      clear: (id, cb) => { h.cleared.push(id); cb && cb(); },
      onButtonClicked: event('button'),
    },
    runtime: { onMessage: event('message'), onConnect: event('connect'), lastError: null },
    tabs: {
      onRemoved: event('removed'),
      sendMessage: (tabId, msg) => { h.ctl.push({ tabId, ...msg }); return Promise.resolve(); },
    },
    alarms: { create: () => {}, onAlarm: event('alarm') },
    storage: {
      local: {
        get: async (keys) => Object.fromEntries(keys.filter((k) => k in h.storage).map((k) => [k, h.storage[k]])),
        set: async (o) => { Object.assign(h.storage, o); },
      },
      onChanged: event('storage'),
    },
  };
  const reply = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const fetch = async (url, opts = {}) => {
    h.fetches.push({ url, opts });
    if (!h.serverUp) throw new Error('down');
    const path = new URL(url).pathname;
    if (path === '/health') return reply(200, { ok: true, app: 'minuta', paired: true });
    if (path === '/pair') {
      const body = JSON.parse(opts.body);
      return body.code === pairCode ? reply(200, { ok: true, token: h.validToken }) : reply(401, { error: 'wrong or expired code' });
    }
    const token = (opts.headers || {})['X-Minuta-Token'];
    if (token !== h.validToken) return reply(401, { error: 'missing or wrong token' });
    if (path === '/status') {
      return reply(200, { ok: true, recording_mode: h.mode,
        latest: { id: 'm0', title: 'Previous', status: 'ready', transcribed: true } });
    }
    if (path === '/mode') { h.mode = JSON.parse(opts.body).mode; return reply(200, { ok: true, recording_mode: h.mode }); }
    h.sent.push({ path, port: new URL(url).port, ...JSON.parse(opts.body) });
    return reply(200, { ok: true });
  };
  const timers = [];
  let offset = 0; // clock that can jump: page problems wait a few seconds before warning
  class FakeDate extends Date {}
  FakeDate.now = () => Date.now() + offset;
  h.advance = (ms) => { offset += ms; };
  const ctx = {
    chrome, fetch, console, Promise, JSON, Date: FakeDate, Math, Set, Map, Error, Number, String, URL, Object,
    setInterval: (f) => { timers.push(f); return timers.length; },
    setTimeout: () => 0,
    clearInterval: () => {},
  };
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), ctx);
  h.msg = (msg, tabId = 7) => h.listeners.message({ src: 'minuta', ...msg }, { tab: { id: tabId } });
  h.connect = () => {
    const port = { name: 'popup', got: [], onMsg: null, onDisc: null,
      postMessage: (m) => port.got.push(m),
      onMessage: { addListener: (f) => { port.onMsg = f; } },
      onDisconnect: { addListener: (f) => { port.onDisc = f; } } };
    h.listeners.connect(port);
    h.ports.push(port);
    return port;
  };
  h.flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 5)); };
  h.tick = () => timers.forEach((f) => f());
  h.lastView = (port) => port.got.filter((m) => m.type === 'view').at(-1).view;
  return h;
}

const hb = (extra = {}) => ({ type: 'heartbeat', session: 's1', recording: true, inCall: true, ctxState: 'running',
  muted: false, localTrack: true, remoteSignal: true, paused: false, elapsedMs: 1000, mode: 'auto', ...extra });
const waiting = (extra = {}) => ({ type: 'state', session: null, recording: false, inCall: true, title: 'Plan',
  ...extra });

const scenarios = {
  async recording_paints_rec_and_sends_token() {
    const h = load();
    await h.flush();
    assert.equal(h.icon, 'icons/idle-16.png');
    h.msg(hb());
    await h.flush();
    assert.equal(h.icon, 'icons/rec-16.png');
    assert.equal(h.badge, 'REC');
    assert.equal(h.notifications.length, 0);
    const beat = h.sent.filter((m) => m.path === '/heartbeat').at(-1);
    assert.ok(beat && beat.port === '47321');
  },

  async paused_paints_paused() {
    const h = load();
    h.msg(hb({ paused: true }));
    await h.flush();
    assert.equal(h.icon, 'icons/paused-16.png');
    assert.equal(h.badge, 'II');
  },

  async broken_capture_warns_once_and_rearms() {
    const h = load();
    h.msg(hb({ remoteBroken: true }));
    await h.flush();
    assert.equal(h.icon, 'icons/rec-16.png', 'one odd moment is not enough to warn');
    h.advance(5000);
    h.msg(hb({ remoteBroken: true }));
    h.advance(5000);
    h.msg(hb({ remoteBroken: true }));
    await h.flush();
    assert.equal(h.icon, 'icons/warn-16.png');
    assert.equal(h.badge, '!');
    assert.match(h.title, /others' voices/);
    assert.equal(h.notifications.filter((n) => n.id === 's1:remote').length, 1);
    h.msg(hb({ remoteBroken: false }));
    await h.flush();
    assert.equal(h.icon, 'icons/rec-16.png');
    h.msg(hb({ remoteBroken: true }));
    h.advance(5000);
    h.msg(hb({ remoteBroken: true }));
    await h.flush();
    assert.equal(h.notifications.filter((n) => n.id === 's1:remote').length, 2);
  },

  async silence_from_others_does_not_alarm() {
    const h = load();
    h.msg(hb({ remoteUnverified: true }));
    await h.flush();
    assert.equal(h.icon, 'icons/rec-16.png');
    assert.equal(h.notifications.length, 0);
    const port = h.connect();
    assert.ok(h.lastView(port).problems.some((p) => p.key === 'silence' && !p.serious));
  },

  async missing_mute_button_warns() {
    const h = load();
    h.msg(hb({ muted: null }));
    await h.flush();
    assert.equal(h.notifications.length, 0, 'at start the button may not be there yet');
    h.advance(5000);
    h.msg(hb({ muted: null }));
    await h.flush();
    assert.equal(h.icon, 'icons/warn-16.png');
    assert.ok(h.notifications.some((n) => /microphone button/.test(n.message)));
  },

  async server_down_keeps_and_resends_in_order() {
    const h = load();
    h.msg({ type: 'start', session: 's1', title: 'X' });
    await h.flush();
    h.serverUp = false;
    for (let i = 0; i < 5; i++) h.msg({ type: 'chunk', session: 's1', seq: i, data: 'AA' });
    h.msg(hb());
    await h.flush();
    assert.equal(h.icon, 'icons/warn-16.png');
    assert.ok(h.notifications.some((n) => /not answering/.test(n.message)));
    const before = h.sent.length;
    h.serverUp = true;
    h.listeners.alarm();
    await h.flush();
    const chunks = h.sent.slice(before).filter((m) => m.type === 'chunk').map((m) => m.seq);
    assert.deepEqual(chunks, [0, 1, 2, 3, 4]);
    assert.equal(h.icon, 'icons/rec-16.png');
  },

  async pending_has_a_cap() {
    const h = load({ serverUp: false });
    for (let i = 0; i < 2300; i++) h.msg({ type: 'chunk', session: 's1', seq: i, data: 'A' });
    await h.flush();
    const port = h.connect();
    await h.flush();
    const v = h.lastView(port);
    assert.equal(v.pending, 2200);
    assert.ok(v.problems.some((p) => p.key === 'lost'));
  },

  async unpaired_keeps_audio_and_asks_to_pair() {
    const h = load({ stored: {} });
    h.msg({ type: 'start', session: 's1', title: 'X' });
    h.msg({ type: 'chunk', session: 's1', seq: 0, data: 'AA' });
    await h.flush();
    assert.equal(h.sent.length, 0, 'nothing leaves without a token');
    const port = h.connect();
    await h.flush();
    const v = h.lastView(port);
    assert.equal(v.paired, false);
    assert.equal(v.pending, 2);
    assert.ok(v.problems.some((p) => p.key === 'pairing' && p.serious));
    assert.equal(h.icon, 'icons/warn-16.png');
    // Wrong code, then the right one: the token is stored and what was pending is delivered.
    port.onMsg({ action: 'pair', code: '000-000', port: '47321' });
    await h.flush();
    assert.equal(port.got.filter((m) => m.type === 'pair').at(-1).result.ok, false);
    port.onMsg({ action: 'pair', code: '111-222', port: '47321' });
    await h.flush();
    assert.equal(port.got.filter((m) => m.type === 'pair').at(-1).result.ok, true);
    assert.equal(h.storage.token, TOKEN);
    assert.deepEqual(h.sent.filter((m) => m.type !== 'ping').map((m) => m.type), ['start', 'chunk']);
    assert.equal(h.lastView(port).paired, true);
    assert.equal(h.lastView(port).latest.title, 'Previous');
  },

  async revoked_token_shows_pairing_problem() {
    const h = load({ validToken: 'another-token' });
    h.msg(hb());
    await h.flush();
    const port = h.connect();
    await h.flush();
    assert.equal(h.lastView(port).paired, false);
    assert.equal(h.icon, 'icons/warn-16.png');
  },

  async port_is_configurable() {
    const h = load({ stored: { port: 50000, token: TOKEN } });
    h.msg(hb());
    await h.flush();
    assert.equal(h.sent.at(-1).port, '50000');
    const port = h.connect();
    port.onMsg({ action: 'port', port: '1' });
    await h.flush();
    assert.equal(port.got.filter((m) => m.type === 'port').at(-1).result.ok, false);
    port.onMsg({ action: 'port', port: '47400' });
    await h.flush();
    assert.equal(h.storage.port, 47400);
    h.msg(hb());
    await h.flush();
    assert.equal(h.sent.at(-1).port, '47400');
  },

  async popup_gets_state_levels_and_sends_controls() {
    const h = load();
    h.msg(hb(), 9);
    h.msg({ type: 'heartbeat', recording: false, inCall: false, mode: 'auto' }, 3); // another Meet tab, not in a call
    await h.flush();
    const port = h.connect();
    assert.equal(h.lastView(port).tabId, 9);
    assert.ok(h.ctl.some((c) => c.tabId === 9 && c.action === 'live'));
    h.msg({ type: 'level', session: 's1', local: 0.5, remote: 0.2 }, 9);
    assert.equal(port.got.at(-1).type, 'level');
    assert.ok(!h.sent.some((m) => m.type === 'level'), 'levels never go to the server');
    port.onMsg({ action: 'pause' });
    await h.flush();
    assert.deepEqual(h.ctl.at(-1), { tabId: 9, src: 'minuta-ctl', action: 'pause' });
    h.tick();
    assert.ok(h.ctl.filter((c) => c.action === 'live').length >= 2);
    port.onDisc();
  },

  async discard_goes_back_to_waiting() {
    const h = load();
    h.msg(hb());
    h.msg({ type: 'discard', session: 's1' });
    await h.flush();
    assert.equal(h.icon, 'icons/idle-16.png');
    assert.equal(h.sent.filter((m) => m.type !== 'ping').at(-1).type, 'discard');
  },

  async every_tab_is_told_the_mode_of_the_app() {
    const h = load({ mode: 'manual' });
    await h.flush();
    h.msg(waiting({ mode: null }), 4);
    await h.flush();
    assert.ok(h.ctl.some((c) => c.tabId === 4 && c.action === 'mode' && c.mode === 'manual'));
    // From the popup: the app stores it, and the tabs are told.
    const port = h.connect();
    await h.flush();
    assert.equal(h.lastView(port).mode, 'manual');
    port.onMsg({ action: 'mode', mode: 'ask' });
    await h.flush();
    assert.equal(h.mode, 'ask');
    assert.ok(h.ctl.some((c) => c.tabId === 4 && c.action === 'mode' && c.mode === 'ask'));
  },

  async without_the_app_tabs_are_told_to_ask() {
    const h = load({ serverUp: false });
    await h.flush();
    h.msg(waiting({ mode: null }), 4);
    await h.flush();
    assert.ok(h.ctl.some((c) => c.tabId === 4 && c.action === 'mode' && c.mode === 'ask'));
  },

  async ask_mode_notifies_once_and_the_buttons_control_the_tab() {
    const h = load({ mode: 'ask' });
    await h.flush();
    h.msg(waiting({ mode: 'ask', awaiting: true }), 4);
    h.msg(waiting({ mode: 'ask', awaiting: true }), 4);
    await h.flush();
    const asks = h.notifications.filter((n) => n.id.startsWith('ask:'));
    assert.equal(asks.length, 1);
    assert.deepEqual(asks[0].buttons.map((b) => b.title), ['Record', "Don't record"]);
    assert.equal(h.badge, '?');
    h.listeners.button(asks[0].id, 1);
    assert.deepEqual(h.ctl.at(-1), { tabId: 4, src: 'minuta-ctl', action: 'skip' });
    assert.ok(h.cleared.includes(asks[0].id));
    // Recording from the popup instead closes the question.
    h.msg(waiting({ mode: 'ask', awaiting: true }), 5);
    await h.flush();
    const second = h.notifications.filter((n) => n.id.startsWith('ask:')).at(-1);
    h.msg(hb({ mode: 'ask' }), 5);
    await h.flush();
    assert.ok(h.cleared.includes(second.id));
  },

  async a_token_from_the_minuta_page_is_picked_up() {
    // The page (app.html) pairs again and stores the token; the service worker uses it at once.
    const h = load({ stored: { port: 47321, token: 'old-token' } });
    h.msg(hb());
    await h.flush();
    const port = h.connect();
    await h.flush();
    assert.equal(h.lastView(port).paired, false);
    h.storage.token = TOKEN;
    h.listeners.storage({ token: { newValue: TOKEN } }, 'local');
    await h.flush();
    h.msg(hb());
    await h.flush();
    assert.equal(h.lastView(port).paired, true);
    assert.equal(h.sent.at(-1).type, 'heartbeat');
  },

  async closed_tab_is_forgotten() {
    const h = load();
    h.msg(hb());
    await h.flush();
    h.listeners.removed(7);
    await h.flush();
    assert.equal(h.icon, 'icons/idle-16.png');
  },
};

scenarios[scenario]().then(
  () => process.exit(0),
  (e) => { console.error(e); process.exit(1); },
);
