// Runs in the Meet page's own world, before Meet's scripts.
// Records one stereo file: left channel = you, right channel = everyone else.
// Your channel goes through a gain that is 0 unless Meet confirms you are NOT muted.
(() => {
  if (window.__minuta) return;
  window.__minuta = true;

  const CHUNK_MS = 5000;
  const START_POLLS = 2; // seconds in a call before recording starts
  const END_POLLS = 6; // seconds out of a call before recording stops
  const UNMUTE_HOLD_MS = 300; // Meet's button flickers "unmuted" for ~100 ms while switching
  const SIGNAL = 0.0001; // minimum peak to say a channel has sound
  const REMOTE_SILENCE_MS = 60000; // nothing from the others for this long: soft warning
  const STATS_MS = 5000;
  // Meet sends almost nothing while nobody talks. Above this, speech is arriving over the network.
  const SPEECH_KBPS = 12;
  const BROKEN_SAMPLES = 4; // samples in a row (20 s) with speech on the network and a silent channel
  const LIVE_MS = 250;

  const localTracks = new Set(); // microphones Meet asked for; the last live one is the real one
  const remoteTracks = new Set();
  const peers = new Set();
  // Meet processes voices in an AudioWorklet and leaves the WebRTC tracks silent: what you hear is
  // what it connects to the speakers. Here we only note which nodes go to the speakers; the copy is
  // made while recording.
  const speakers = new Map(); // AudioNode -> Set<output>
  const watchedContexts = new WeakSet();
  const nativeConnect = AudioNode.prototype.connect;
  const nativeDisconnect = AudioNode.prototype.disconnect;

  let rec = null;
  let captureError = null;
  let skipCall = null; // call the user asked not to record (or finished early)
  let approvedCall = null; // call the user asked to record (ask and manual modes)
  // auto | ask | manual, from the minuta app through the extension. Unknown: nothing starts by itself.
  let mode = null;
  let liveUntil = 0;

  const post = (type, payload = {}) => window.postMessage({ src: 'minuta', type, ...payload }, '*');
  const now = () => performance.now();

  // --- Hooks ----------------------------------------------------------------
  function watchContext(ctx) {
    if (watchedContexts.has(ctx)) return;
    watchedContexts.add(ctx);
    ctx.addEventListener('statechange', () => {
      if (ctx.state !== 'closed') return;
      for (const node of [...speakers.keys()]) if (node.context === ctx) speakers.delete(node);
      syncCapture();
    });
  }

  AudioNode.prototype.connect = function(destination, output) {
    const result = nativeConnect.apply(this, arguments);
    try {
      if (destination instanceof AudioDestinationNode && this.context instanceof AudioContext &&
          (!rec || this.context !== rec.ctx)) {
        let outs = speakers.get(this);
        if (!outs) { outs = new Set(); speakers.set(this, outs); }
        outs.add(typeof output === 'number' ? output : 0);
        watchContext(this.context);
        syncCapture();
      }
    } catch (e) {
      captureError = `Could not follow the audio output: ${e.name}`;
    }
    return result;
  };

  AudioNode.prototype.disconnect = function(destination, output) {
    const result = nativeDisconnect.apply(this, arguments);
    const outs = speakers.get(this);
    if (outs) {
      try {
        if (arguments.length === 0) outs.clear();
        else if (typeof destination === 'number') outs.delete(destination);
        else if (destination === this.context.destination) {
          if (typeof output === 'number') outs.delete(output); else outs.clear();
        }
        if (!outs.size) speakers.delete(this);
        syncCapture();
      } catch (_) {}
    }
    return result;
  };

  const md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    const origGUM = md.getUserMedia.bind(md);
    md.getUserMedia = async (constraints) => {
      const stream = await origGUM(constraints);
      stream.getAudioTracks().forEach((t) => localTracks.add(t));
      return stream;
    };
  }

  const OrigPC = window.RTCPeerConnection;
  if (OrigPC) {
    class PatchedPC extends OrigPC {
      constructor(...args) {
        super(...args);
        peers.add(this);
        this.addEventListener('connectionstatechange', () => {
          if (this.connectionState === 'closed') peers.delete(this);
        });
        this.addEventListener('track', (e) => {
          if (e.track && e.track.kind === 'audio') {
            remoteTracks.add(e.track);
            e.track.addEventListener('ended', () => { remoteTracks.delete(e.track); syncCapture(); });
            syncCapture();
          }
        });
      }
    }
    window.RTCPeerConnection = PatchedPC;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = PatchedPC;
  }

  // --- Meet state -----------------------------------------------------------
  // When you switch tabs Meet opens a floating window (Document Picture-in-Picture) and MOVES the
  // videos and controls there: they are no longer in `document`. Look in both.
  function pipDoc() {
    try {
      const w = window.documentPictureInPicture && window.documentPictureInPicture.window;
      return w ? w.document : null;
    } catch (_) {
      return null;
    }
  }
  const docs = () => [document, pipDoc()].filter(Boolean);
  const inCall = () => docs().some((d) => !!d.querySelector('[data-participant-id]'));
  // A live WebRTC connection: Chrome closes it when you leave the call.
  const peerAlive = () => [...peers].some((pc) => pc.connectionState === 'connected');
  const participantCount = () => new Set(docs().flatMap((d) =>
    [...d.querySelectorAll('[data-participant-id]')].map((el) => el.getAttribute('data-participant-id')))).size;

  // null = unknown (treated as muted)
  function isMuted() {
    for (const d of docs()) {
      for (const b of d.querySelectorAll('[data-is-muted]')) {
        const lower = `${b.textContent || ''} ${b.getAttribute('aria-label') || ''}`.toLowerCase();
        if (lower.includes('videocam') || lower.includes('cámara') || lower.includes('camera')) continue;
        if (lower.includes('mic') || lower.includes('micrófono') || lower.includes('microphone')) {
          return b.getAttribute('data-is-muted') === 'true';
        }
      }
    }
    return null;
  }

  // meet.google.com/abc-defg-hij, and also /meet/abc-defg-hij or /_meet/abc-defg-hij (links from
  // Calendar, Gmail and the Meet app).
  function meetingCode() {
    const m = location.pathname.match(/(?:^|\/)([a-z]{3}-[a-z]{4}-[a-z]{3})(?:\/|$)/);
    return m ? m[1] : null;
  }
  // The link to the call without query or extra path: the same for everyone invited.
  const meetingUrl = () => (meetingCode() ? `https://meet.google.com/${meetingCode()}` : location.origin + location.pathname);
  const callKey = () => meetingCode() || location.pathname;

  function meetingTitle() {
    const el = document.querySelector('[data-meeting-title]');
    const t = el && el.getAttribute('data-meeting-title');
    if (t) return t;
    const title = document.title.replace(/\s*-\s*Google Meet\s*$/i, '').replace(/^Meet\s*-\s*/i, '').trim();
    // Without a calendar event the tab only shows the code: that is not a title.
    return title && title !== meetingCode() && title !== 'Google Meet' && title !== 'Meet' ? title : null;
  }

  // --- Capture --------------------------------------------------------------
  function addTap(node, output) {
    try {
      const dest = node.context.createMediaStreamDestination();
      nativeConnect.call(node, dest, output, 0);
      const src = rec.ctx.createMediaStreamSource(dest.stream);
      nativeConnect.call(src, rec.gainRemote);
      let outs = rec.taps.get(node);
      if (!outs) { outs = new Map(); rec.taps.set(node, outs); }
      outs.set(output, { dest, src });
    } catch (e) {
      captureError = `Could not copy the audio output: ${e.name}`;
    }
  }

  function dropTap(node, output) {
    const outs = rec.taps.get(node);
    const tap = outs && outs.get(output);
    if (!tap) return;
    try { nativeDisconnect.call(node, tap.dest, output, 0); } catch (_) {}
    try { nativeDisconnect.call(tap.src); } catch (_) {}
    tap.dest.stream.getTracks().forEach((t) => t.stop());
    outs.delete(output);
    if (!outs.size) rec.taps.delete(node);
  }

  const tapCount = () => [...rec.taps.values()].reduce((n, outs) => n + outs.size, 0);

  // Fallback: the WebRTC tracks, in case Meet plays them the usual way again.
  function addFallback(track) {
    const stream = new MediaStream([track]);
    // Chrome does not deliver WebRTC audio to WebAudio unless the track is also in an audio element.
    const sink = new Audio();
    sink.muted = true;
    sink.srcObject = stream;
    sink.play().catch(() => {});
    const src = rec.ctx.createMediaStreamSource(stream);
    nativeConnect.call(src, rec.gainRemote);
    rec.fallback.set(track, { src, sink });
  }

  function dropFallback(track) {
    const f = rec.fallback.get(track);
    if (!f) return;
    try { nativeDisconnect.call(f.src); } catch (_) {}
    f.sink.srcObject = null;
    rec.fallback.delete(track);
  }

  // A single path for the others: adding playback and tracks together doubles the voices.
  function syncCapture() {
    if (!rec) return;
    for (const [node, outs] of [...rec.taps]) {
      for (const output of [...outs.keys()]) {
        const wanted = speakers.get(node);
        if (!wanted || !wanted.has(output) || node.context.state === 'closed') dropTap(node, output);
      }
    }
    for (const [node, outs] of speakers) {
      if (node.context.state === 'closed') continue;
      for (const output of outs) if (!(rec.taps.get(node) || new Map()).has(output)) addTap(node, output);
    }
    const playback = tapCount() > 0;
    for (const track of [...rec.fallback.keys()]) {
      if (playback || track.readyState !== 'live') dropFallback(track);
    }
    if (!playback) {
      for (const track of remoteTracks) {
        if (track.readyState === 'live' && !rec.fallback.has(track)) addFallback(track);
      }
    }
  }

  function currentLocalTrack() {
    let last = null;
    for (const t of [...localTracks]) {
      if (t.readyState === 'live') last = t;
      else localTracks.delete(t);
    }
    return last;
  }

  function syncLocal() {
    const t = currentLocalTrack();
    if (t === rec.localTrack) return;
    if (rec.localSrc) nativeDisconnect.call(rec.localSrc);
    rec.localSrc = null;
    rec.localTrack = t;
    if (t) {
      rec.localSrc = rec.ctx.createMediaStreamSource(new MediaStream([t]));
      nativeConnect.call(rec.localSrc, rec.gainLocal);
    }
  }

  function level(analyser, samples) {
    analyser.getFloatTimeDomainData(samples);
    let peak = 0;
    for (let i = 0; i < samples.length; i++) {
      const v = samples[i] < 0 ? -samples[i] : samples[i];
      if (v > peak) peak = v;
    }
    return peak;
  }

  // The recording is silent: a context without an output does not open another stream to the headphones.
  function newContext() {
    const opts = { sampleRate: 48000, latencyHint: 'playback' };
    try {
      return new AudioContext({ ...opts, sinkId: { type: 'none' } });
    } catch (_) {
      return new AudioContext(opts);
    }
  }

  // --- Recording ------------------------------------------------------------
  function start() {
    const code = meetingCode();
    const session = `${new Date().toISOString().replace(/[:.]/g, '-')}_${code || 'meet'}`;
    const ctx = newContext();
    ctx.resume().catch(() => {});
    const merger = ctx.createChannelMerger(2);
    const gainLocal = ctx.createGain();
    const gainRemote = ctx.createGain();
    const analyserLocal = ctx.createAnalyser();
    const analyserRemote = ctx.createAnalyser();
    analyserLocal.fftSize = analyserRemote.fftSize = 2048;
    gainLocal.gain.value = 0;
    nativeConnect.call(gainLocal, analyserLocal);
    nativeConnect.call(analyserLocal, merger, 0, 0);
    nativeConnect.call(gainRemote, analyserRemote);
    nativeConnect.call(analyserRemote, merger, 0, 1);
    const dest = ctx.createMediaStreamDestination();
    dest.channelCount = 2;
    nativeConnect.call(merger, dest);

    const recorder = new MediaRecorder(dest.stream, { mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 64000 });
    rec = {
      session, code, ctx, gainLocal, gainRemote, analyserLocal, analyserRemote, recorder,
      samples: new Float32Array(2048), started: now(), startedAt: new Date().toISOString(),
      taps: new Map(), fallback: new Map(), localSrc: null, localTrack: null,
      muted: null, unmutedSince: null, paused: false, discard: false,
      localLevel: 0, remoteLevel: 0, peakLocal: 0, peakRemote: 0,
      remoteSignal: false, lastRemoteSignal: null,
      stats: { bytes: null, at: null, kbps: null, speechSamples: 0 }, remoteBroken: false,
    };
    syncCapture();
    syncLocal();

    let seq = 0;
    let chain = Promise.resolve();
    recorder.ondataavailable = (e) => {
      if (!e.data || !e.data.size) return;
      const n = seq++;
      const blob = e.data;
      chain = chain.then(async () => {
        const buf = new Uint8Array(await blob.arrayBuffer());
        let bin = '';
        for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        post('chunk', { session, seq: n, data: btoa(bin) });
      }).catch(() => {});
    };
    const r = rec;
    recorder.onstop = () => { chain = chain.then(() => post(r.discard ? 'discard' : 'end', { session })); };
    recorder.start(CHUNK_MS);
    post('start', { session, code, title: meetingTitle(), url: meetingUrl(), startedAt: rec.startedAt });
  }

  function stop({ discard = false } = {}) {
    if (!rec) return;
    const r = rec;
    rec = null;
    r.discard = discard;
    try {
      r.recorder.stop();
    } catch (_) {
      post(discard ? 'discard' : 'end', { session: r.session });
    }
    // Torn down after the recorder hands over the last chunk.
    setTimeout(() => {
      for (const [node, outs] of [...r.taps]) {
        for (const [output, tap] of outs) {
          try { nativeDisconnect.call(node, tap.dest, output, 0); } catch (_) {}
          tap.dest.stream.getTracks().forEach((t) => t.stop());
        }
      }
      r.taps.clear();
      r.fallback.forEach(({ sink }) => { sink.srcObject = null; });
      r.fallback.clear();
      r.ctx.close().catch(() => {});
    }, 1000);
  }

  function setPaused(paused) {
    if (!rec || rec.paused === paused) return;
    rec.paused = paused;
    rec.gainRemote.gain.setTargetAtTime(paused ? 0 : 1, rec.ctx.currentTime, 0.01);
    post('pause', { session: rec.session, paused, t: new Date().toISOString() });
  }

  // Every 5 s: is speech from the others arriving over the network while their channel is silent?
  async function checkStats() {
    if (!rec) return;
    const r = rec;
    let bytes = 0;
    for (const pc of [...peers]) {
      if (pc.connectionState !== 'connected') continue;
      try {
        (await pc.getStats()).forEach((s) => {
          if (s.type === 'inbound-rtp' && s.kind === 'audio') bytes += s.bytesReceived || 0;
        });
      } catch (_) {}
    }
    if (rec !== r) return;
    const t = now();
    const st = r.stats;
    if (st.bytes != null && bytes >= st.bytes && t > st.at) st.kbps = ((bytes - st.bytes) * 8) / (t - st.at);
    st.bytes = bytes;
    st.at = t;
    // The others' channel can only be quiet if nobody talks or if pause silences it.
    const silent = !r.lastRemoteSignal || t - r.lastRemoteSignal > STATS_MS;
    if (st.kbps != null && st.kbps > SPEECH_KBPS && silent && !r.paused) st.speechSamples++;
    else st.speechSamples = 0;
    r.remoteBroken = st.speechSamples >= BROKEN_SAMPLES;
  }

  function snapshot() {
    const people = participantCount();
    const remoteSources = rec ? tapCount() : 0;
    return {
      session: rec ? rec.session : null,
      code: meetingCode(),
      title: rec ? rec.title || meetingTitle() : meetingTitle(),
      inCall: inCall(),
      pip: !!pipDoc(),
      peerAlive: peerAlive(),
      recording: !!rec,
      skipped: !rec && skipCall === callKey() && inCall(),
      mode,
      // In a call, not recording, and waiting for the user: ask mode (or mode not known yet).
      awaiting: !rec && inCall() && skipCall !== callKey() && approvedCall !== callKey() && mode !== 'auto' &&
        mode !== 'manual',
      manualIdle: !rec && inCall() && skipCall !== callKey() && approvedCall !== callKey() && mode === 'manual',
      paused: rec ? rec.paused : false,
      elapsedMs: rec ? now() - rec.started : 0,
      muted: isMuted(), // what Meet shows now; rec.muted starts as null until the first cycle
      localTrack: !!currentLocalTrack(),
      remoteTracks: [...remoteTracks].filter((t) => t.readyState === 'live').length,
      participantCount: people,
      remoteSources,
      remoteCapture: rec ? (remoteSources ? 'playback' : 'webrtc') : null,
      remoteLevel: rec ? rec.peakRemote : null,
      localLevel: rec ? rec.peakLocal : null,
      remoteSignal: rec ? rec.remoteSignal : false,
      remoteUnverified: !!rec && !rec.paused && now() - (rec.lastRemoteSignal ?? rec.started) > REMOTE_SILENCE_MS &&
        (remoteSources > 0 || remoteTracks.size > 0 || people > 1),
      remoteBroken: rec ? rec.remoteBroken : false,
      inboundKbps: rec && rec.stats.kbps != null ? Math.round(rec.stats.kbps * 10) / 10 : null,
      captureError: rec ? captureError : null,
      ctxState: rec ? rec.ctx.state : null,
      sinkless: rec ? !!(rec.ctx.sinkId && rec.ctx.sinkId.type === 'none') : null,
    };
  }

  // --- Controls (from the extension popup) -----------------------------------
  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.src !== 'minuta-ctl') return;
    switch (e.data.action) {
      case 'live': liveUntil = now() + 3000; break;
      case 'pause': setPaused(true); break;
      case 'resume': setPaused(false); break;
      case 'finish': if (rec) { skipCall = callKey(); stop(); } break;
      case 'discard': if (rec) { skipCall = callKey(); stop({ discard: true }); } break;
      case 'record': skipCall = null; approvedCall = callKey(); break;
      case 'skip': skipCall = callKey(); approvedCall = null; break;
      case 'mode': mode = ['auto', 'ask', 'manual'].includes(e.data.mode) ? e.data.mode : null; break;
      default: return;
    }
    post('state', snapshot());
  });

  // --- Loop -----------------------------------------------------------------
  let inCallPolls = 0;
  let outPolls = 0;
  let tick = 0;
  let lastLive = 0;

  // Mute is checked often: it decides whether your voice is written.
  setInterval(() => {
    if (!rec) return;
    syncLocal();
    rec.localLevel = level(rec.analyserLocal, rec.samples);
    rec.remoteLevel = level(rec.analyserRemote, rec.samples);
    rec.peakLocal = Math.max(rec.peakLocal, rec.localLevel);
    rec.peakRemote = Math.max(rec.peakRemote, rec.remoteLevel);
    if (rec.remoteLevel > SIGNAL) {
      rec.remoteSignal = true;
      rec.lastRemoteSignal = now();
      rec.remoteBroken = false;
      rec.stats.speechSamples = 0;
    }
    const muted = isMuted();
    if (muted !== false) rec.unmutedSince = null;
    else if (rec.unmutedSince == null) rec.unmutedSince = now();
    const open = !rec.paused && muted === false && now() - rec.unmutedSince >= UNMUTE_HOLD_MS && !!rec.localTrack;
    rec.gainLocal.gain.setTargetAtTime(open ? 1 : 0, rec.ctx.currentTime, 0.01);
    if (muted !== rec.muted) {
      rec.muted = muted;
      post('mute', { session: rec.session, muted, t: new Date().toISOString() });
    }
    if (now() < liveUntil && now() - lastLive >= LIVE_MS) {
      lastLive = now();
      post('level', { session: rec.session, local: rec.localLevel, remote: rec.remoteLevel, muted: rec.muted,
        paused: rec.paused, elapsedMs: now() - rec.started });
    }
  }, 100);

  setInterval(() => {
    const here = inCall();
    // Starting needs the call on screen; stopping also needs no live WebRTC connection, so a Meet UI
    // change (floating window, new layout) does not split the recording.
    const alive = here || (!!rec && peerAlive());
    if (here) inCallPolls++; else inCallPolls = 0;
    if (alive) outPolls = 0; else outPolls++;
    if (!here && outPolls >= END_POLLS) { skipCall = null; approvedCall = null; } // the next call starts fresh
    const allowed = mode === 'auto' || approvedCall === callKey();
    if (!rec && inCallPolls >= START_POLLS && skipCall !== callKey() && allowed) start();
    if (inCallPolls === 1) post('state', snapshot()); // the extension answers with the recording mode
    if (rec && outPolls >= END_POLLS) stop();

    if (rec && rec.title !== meetingTitle()) {
      rec.title = meetingTitle();
      post('title', { session: rec.session, title: rec.title });
    }

    if (rec && (tick % (STATS_MS / 1000)) === 0) checkStats();
    if (rec) syncCapture(); // in case Meet closed a context without telling

    if (++tick % 5 === 0) {
      post('heartbeat', snapshot());
      if (rec) { rec.peakLocal = 0; rec.peakRemote = 0; } // heartbeats carry the peak of their 5 s
    } else if (now() < liveUntil) {
      post('state', snapshot());
    }
  }, 1000);

  window.addEventListener('pagehide', () => stop());
})();
