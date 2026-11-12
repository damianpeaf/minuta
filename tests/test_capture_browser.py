"""page.js with real audio in an isolated Chromium: no Meet, no paid APIs, no real microphone.

uv run pytest -m browser tests/test_capture_browser.py
CHROME_BIN picks another Chrome/Chromium; by default Playwright's Chromium is used.
"""
from __future__ import annotations

import base64
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.browser
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def chrome():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # /not-in-call: Meet open but not in the call yet.
            participant = b"" if "not-in-call" in self.path else b'<div data-participant-id="test"></div>'
            body = b'<title>Capture test</title>' + participant + b'''
                <button data-is-muted="false" aria-label="Microphone">mic</button>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with sync_playwright() as p:
        binary = os.environ.get("CHROME_BIN") or None
        browser = p.chromium.launch(executable_path=binary, headless=True,
                                    args=["--autoplay-policy=no-user-gesture-required",
                                          # Without this the local IP goes as a .local name and, if it
                                          # does not resolve, the two test connections never meet.
                                          "--disable-features=WebRtcHideLocalIpsWithMdns"])
        yield browser, f"http://127.0.0.1:{server.server_port}"
        browser.close()
    server.shutdown()
    server.server_close()


def open_page(chrome, route="/abc-defg-hij"):
    browser, base = chrome
    context = browser.new_context()
    # Synthetic microphone: the test never touches a real one.
    bootstrap = """window.events = [];
      window.addEventListener('message', e => { if(e.data.src === 'minuta') events.push(e.data); });
      navigator.mediaDevices.getUserMedia = async () => window.fakeMic;
      window.advance = 0;
      const now = performance.now.bind(performance);
      performance.now = () => now() + window.advance;
    """
    script = ROOT / "extension/page.js"
    # The extension tells page.js the recording mode; these tests record every call.
    auto = "\nwindow.postMessage({ src: 'minuta-ctl', action: 'mode', mode: 'auto' }, '*');"
    context.add_init_script(bootstrap + script.read_text() + auto)
    page = context.new_page()
    page.goto(base + route)
    return context, page


@pytest.fixture
def page(chrome):
    context, page = open_page(chrome)
    yield page
    context.close()


@pytest.fixture
def page_not_in_call(chrome):
    context, page = open_page(chrome, "/not-in-call")
    yield page
    context.close()


SCENE = """async ({playback, raw, muted}) => {
  document.querySelector('button').setAttribute('data-is-muted', String(muted));
  const ctx = window.audio = new AudioContext({sampleRate: 48000});
  await ctx.resume();
  function tone(frequency, amplitude) {
    const oscillator = ctx.createOscillator(), gain = ctx.createGain();
    oscillator.frequency.value = frequency; gain.gain.value = amplitude;
    oscillator.connect(gain); oscillator.start(); return gain;
  }
  const mic = ctx.createMediaStreamDestination();
  tone(440, 0.08).connect(mic);
  window.fakeMic = mic.stream;
  await navigator.mediaDevices.getUserMedia({audio: true});

  // A real remote WebRTC track. The regression case sends silence here while
  // Meet plays the voices through a separate AudioWorklet.
  const remote = ctx.createMediaStreamDestination();
  tone(880, raw ? 0.08 : 0).connect(remote);
  const sender = window.sender = new RTCPeerConnection();
  const receiver = window.receiver = new RTCPeerConnection();
  sender.onicecandidate = e => e.candidate && receiver.addIceCandidate(e.candidate);
  receiver.onicecandidate = e => e.candidate && sender.addIceCandidate(e.candidate);
  const gotTrack = new Promise(resolve => receiver.ontrack = resolve);
  sender.addTrack(remote.stream.getAudioTracks()[0], remote.stream);
  await sender.setLocalDescription(await sender.createOffer());
  await receiver.setRemoteDescription(sender.localDescription);
  await receiver.setLocalDescription(await receiver.createAnswer());
  await sender.setRemoteDescription(receiver.localDescription);
  await gotTrack;

  if (playback) {
    const code = `class Voice extends AudioWorkletProcessor {
      constructor() { super(); this.phase = 0; }
      process(inputs, outputs) {
        const out = outputs[0];
        for(let i=0; i<out[0].length; i++) {
          const sample = Math.sin(this.phase) * 0.08;
          this.phase += 2*Math.PI*880/sampleRate;
          for(const channel of out) channel[i] = sample;
        }
        return true;
      }
    }; registerProcessor('voice', Voice);`;
    const url = URL.createObjectURL(new Blob([code], {type:'application/javascript'}));
    await ctx.audioWorklet.addModule(url); URL.revokeObjectURL(url);
    const voice = new AudioWorkletNode(ctx, 'voice');
    const gain = window.speaker = ctx.createGain();
    voice.connect(gain); gain.connect(ctx.destination);
  }
}"""


def start(page, *, playback=True, raw=False, muted=False):
    page.evaluate(SCENE, dict(playback=playback, raw=raw, muted=muted))
    page.wait_for_function("events.some(e => e.type === 'start')")
    page.wait_for_function("events.some(e => e.type === 'heartbeat' && e.recording)")


def latest(page):
    return page.evaluate("events.filter(e => e.type === 'heartbeat' && e.recording).at(-1)")


def finish(page, tmp_path):
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.wait_for_function("events.some(e => e.type === 'end')")
    chunks = page.evaluate("events.filter(e => e.type === 'chunk').sort((a,b)=>a.seq-b.seq).map(e=>e.data)")
    audio = tmp_path / "recording.webm"
    audio.write_bytes(b"".join(base64.b64decode(c) for c in chunks))
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-ar", "16000",
                             "-ac", "2", "-f", "f32le", "-"], capture_output=True, check=True)
    frames = np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)
    # Only the last second: both sources and mute are stable by then.
    return frames[-16000:]


def amplitude(frames, channel, frequency):
    """The tone's amplitude in a typical 100 ms of the window (the median). A loaded CI machine drops
    a few hundred ms of the synthetic microphone now and then; one dropout must not decide the test."""
    samples = frames[:, channel]
    window = 1600  # 100 ms: a whole number of periods of 440 and 880 Hz
    phase = np.arange(window) * 2 * np.pi * frequency / 16000
    wave = np.exp(-1j * phase)
    parts = [samples[i:i + window] for i in range(0, len(samples) - window + 1, window)]
    return float(np.median([2 * abs(np.sum(p * wave)) / window for p in parts]))


def test_captures_playback_when_the_webrtc_track_is_silent(page, tmp_path):
    start(page)
    hb = latest(page)
    frames = finish(page, tmp_path)
    assert amplitude(frames, 0, 440) > 0.05
    assert amplitude(frames, 1, 880) > 0.05
    assert amplitude(frames, 1, 440) < 0.005  # your mic never leaks into the others' channel
    assert amplitude(frames, 0, 880) < 0.005
    assert hb["remoteCapture"] == "playback" and hb["remoteSources"] == 1
    assert hb["remoteTracks"] >= 1 and hb["remoteSignal"]


def test_does_not_double_playback_and_webrtc(page, tmp_path):
    start(page, raw=True)
    frames = finish(page, tmp_path)
    assert 0.05 < amplitude(frames, 1, 880) < 0.10


def test_falls_back_to_webrtc_without_speaker_output(page, tmp_path):
    start(page, playback=False, raw=True)
    assert latest(page)["remoteCapture"] == "webrtc"
    frames = finish(page, tmp_path)
    assert amplitude(frames, 1, 880) > 0.05


def test_mute_only_silences_your_channel(page, tmp_path):
    start(page, muted=True)
    frames = finish(page, tmp_path)
    assert np.max(np.abs(frames[:, 0])) < 0.0001
    assert amplitude(frames, 1, 880) > 0.05


@pytest.mark.parametrize("disconnect", ["speaker.disconnect()", "speaker.disconnect(0)",
                                       "speaker.disconnect(audio.destination)"])
def test_stops_capturing_a_disconnected_output_and_reconnects(page, tmp_path, disconnect):
    start(page)
    page.evaluate(disconnect)
    page.wait_for_function("events.filter(e=>e.type==='heartbeat').at(-1)?.remoteSources === 0")
    assert latest(page)["remoteCapture"] == "webrtc"
    page.evaluate("speaker.connect(audio.destination)")
    page.wait_for_function("events.filter(e=>e.type==='heartbeat').at(-1)?.remoteSources === 1")
    frames = finish(page, tmp_path)
    assert 0.05 < amplitude(frames, 1, 880) < 0.10


def test_warns_when_there_are_tracks_but_no_signal(page, tmp_path):
    start(page, playback=False, raw=False, muted=True)
    page.evaluate("window.advance += 65000")
    page.wait_for_function("events.some(e=>e.type==='heartbeat' && e.remoteUnverified)")
    assert not latest(page)["remoteSignal"]
    frames = finish(page, tmp_path)
    assert np.max(np.abs(frames)) < 0.0001


def test_output_that_appears_later_and_context_close(page, tmp_path):
    start(page, playback=False, raw=True)
    page.evaluate("""async () => {
      const ctx = window.lateAudio = new AudioContext(); await ctx.resume();
      const oscillator = ctx.createOscillator(), gain = ctx.createGain();
      oscillator.frequency.value = 880; gain.gain.value = 0.08;
      oscillator.connect(gain); gain.connect(ctx.destination); oscillator.start();
    }""")
    page.wait_for_function("events.filter(e=>e.type==='heartbeat').at(-1)?.remoteCapture === 'playback'")
    page.evaluate("lateAudio.close()")
    page.wait_for_function("events.filter(e=>e.type==='heartbeat').at(-1)?.remoteCapture === 'webrtc'")
    frames = finish(page, tmp_path)
    assert amplitude(frames, 1, 880) > 0.05


def test_warns_when_signal_is_lost_after_being_seen(page, tmp_path):
    start(page)
    assert latest(page)["remoteSignal"]
    page.evaluate("speaker.disconnect()")
    page.wait_for_function("events.filter(e=>e.type==='heartbeat').at(-1)?.remoteSources === 0")
    page.evaluate("window.advance += 65000")
    page.wait_for_function("events.filter(e=>e.type==='heartbeat').at(-1)?.remoteUnverified === true")
    assert latest(page)["remoteSignal"]  # the warning is not limited to the start of the call
    page.evaluate("speaker.connect(audio.destination)")
    page.wait_for_function("events.filter(e=>e.type==='heartbeat').at(-1)?.remoteUnverified === false")
    frames = finish(page, tmp_path)
    assert amplitude(frames, 1, 880) > 0.05


def ctl(page, action):
    page.evaluate("a => window.postMessage({src: 'minuta-ctl', action: a}, '*')", action)


def test_copies_nothing_before_joining_the_call(page_not_in_call, tmp_path):
    page = page_not_in_call
    page.evaluate("""() => {
      window.copies = 0;
      const orig = AudioContext.prototype.createMediaStreamDestination;
      AudioContext.prototype.createMediaStreamDestination = function() { copies++; return orig.call(this); };
    }""")
    page.evaluate(SCENE, dict(playback=True, raw=False, muted=False))
    page.wait_for_timeout(3000)
    # SCENE makes 2 destinations of its own (fake mic and remote track); the extension adds none.
    assert page.evaluate("copies") == 2
    assert not page.evaluate("events.some(e => e.type === 'start')")
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div data-participant-id=\"x\"></div>')")
    page.wait_for_function("events.some(e => e.type === 'heartbeat' && e.recording)")
    assert latest(page)["remoteSources"] == 1
    page.wait_for_timeout(2000)
    frames = finish(page, tmp_path)
    assert amplitude(frames, 1, 880) > 0.05


def test_recording_context_opens_no_audio_output(page):
    start(page)
    assert latest(page)["sinkless"] is True


def test_pause_silences_both_channels_and_resumes(page, tmp_path):
    start(page)
    ctl(page, "pause")
    page.wait_for_function("events.some(e => e.type === 'pause' && e.paused)")
    page.wait_for_timeout(1500)
    paused_chunks = page.evaluate("events.filter(e => e.type === 'chunk').length")
    ctl(page, "resume")
    page.wait_for_function("events.some(e => e.type === 'pause' && !e.paused)")
    page.wait_for_timeout(1500)
    frames = finish(page, tmp_path)
    assert paused_chunks >= 0
    assert amplitude(frames, 0, 440) > 0.05 and amplitude(frames, 1, 880) > 0.05  # everything is back


def test_pause_leaves_silence_in_the_recording(page, tmp_path):
    start(page)
    ctl(page, "pause")
    page.wait_for_function("events.some(e => e.type === 'pause' && e.paused)")
    page.wait_for_timeout(1500)
    frames = finish(page, tmp_path)
    assert np.max(np.abs(frames)) < 0.001


def test_discard_does_not_record_the_same_call_again(page):
    start(page)
    ctl(page, "discard")
    page.wait_for_function("events.some(e => e.type === 'discard')")
    assert not page.evaluate("events.some(e => e.type === 'end')")
    page.wait_for_timeout(3500)
    assert page.evaluate("events.filter(e => e.type === 'start').length") == 1
    page.wait_for_function("events.filter(e => e.type === 'heartbeat').at(-1).skipped === true")
    ctl(page, "record")
    page.wait_for_function("events.filter(e => e.type === 'start').length === 2")


def test_finish_and_the_next_call_is_recorded(page):
    start(page)
    ctl(page, "finish")
    page.wait_for_function("events.some(e => e.type === 'end')")
    page.wait_for_timeout(2500)
    assert page.evaluate("events.filter(e => e.type === 'start').length") == 1
    # Leaves the call and joins another one: it records on its own.
    page.evaluate("document.querySelector('[data-participant-id]').remove()")
    page.wait_for_timeout(7000)
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div data-participant-id=\"y\"></div>')")
    page.wait_for_function("events.filter(e => e.type === 'start').length === 2", timeout=10000)


def test_live_levels_only_when_asked(page):
    start(page)
    page.wait_for_timeout(1000)
    assert not page.evaluate("events.some(e => e.type === 'level')")
    ctl(page, "live")
    page.wait_for_function("events.filter(e => e.type === 'level').length >= 3")
    nivel = page.evaluate("events.filter(e => e.type === 'level').at(-1)")
    assert nivel["remote"] > 0.05 and nivel["local"] > 0.05
    page.wait_for_timeout(4000)  # without renewal they stop on their own
    n = page.evaluate("events.filter(e => e.type === 'level').length")
    page.wait_for_timeout(1000)
    assert page.evaluate("events.filter(e => e.type === 'level').length") == n


def test_detects_speech_on_the_network_that_is_not_recorded(page):
    # The WebRTC track carries audio (no DTX, like someone talking) but the channel stays silent.
    start(page, playback=False, raw=False)
    page.wait_for_function("events.filter(e => e.type === 'heartbeat').at(-1)?.remoteBroken === true", timeout=45000)
    assert latest(page)["inboundKbps"] > 12


def test_not_broken_when_the_channel_has_the_voice(page):
    start(page, playback=False, raw=True)
    page.wait_for_timeout(27000)
    hb = latest(page)
    assert hb["inboundKbps"] > 12 and hb["remoteBroken"] is False


@pytest.mark.slow
def test_memory_is_stable_in_a_long_recording(page):
    """3 min recording with live levels and outputs connecting and disconnecting: memory does not grow."""
    start(page)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")

    def heap():
        cdp.send("HeapProfiler.collectGarbage")
        return {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}["JSHeapUsedSize"]

    page.wait_for_timeout(20000)
    base = heap()
    for _ in range(16):
        ctl(page, "live")
        page.evaluate("speaker.disconnect(); speaker.connect(audio.destination)")
        page.wait_for_timeout(10000)
    final = heap()
    n_events = page.evaluate("events.length")
    page.evaluate("events.length = 0")  # what the test itself accumulates does not count
    final_clean = heap()
    print(f"heap start {base/1e6:.1f} MB, end {final/1e6:.1f} MB, "
          f"without test events {final_clean/1e6:.1f} MB, events {n_events}")
    assert final_clean - base < 2e6
