"""The real extension in Chromium, a fake Meet page, and the real minuta server.

Pairs through the popup with a one-time code, records a call with synthetic voices (Chromium's fake
microphone for you, an oscillator played to the speakers for the others), uses the popup controls,
and checks the meeting that lands in the meetings folder. Nothing leaves the machine: Playwright
answers for meet.google.com and the default port is blocked so a running minuta is never touched.

uv run pytest -m browser tests/test_extension_e2e.py
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from minuta import audio, pairing
from minuta import config as config_mod
from minuta.meetings import MeetingStore
from minuta.server import Server

from .conftest import ROOT

pytestmark = pytest.mark.browser
playwright_api = pytest.importorskip("playwright.sync_api")
EXT = ROOT / "extension"
SHOTS = os.environ.get("MINUTA_SCREENSHOTS")

MEET = """<!doctype html><title>Team sync - Google Meet</title>
<button data-is-muted="false" aria-label="Turn off microphone">mic</button>
<script>
  window.join = async () => {
    const ctx = new AudioContext();
    await ctx.resume();
    await navigator.mediaDevices.getUserMedia({audio: true}); // Chromium's fake microphone
    const voice = ctx.createOscillator(), gain = ctx.createGain();
    voice.frequency.value = 880; gain.gain.value = 0.1;
    voice.connect(gain); gain.connect(ctx.destination); voice.start(); // "the others" on the speakers
    document.body.insertAdjacentHTML('beforeend',
      '<div data-participant-id="p1"></div><div data-participant-id="p2"></div>');
  };
</script>"""


@pytest.fixture
def stack(cfg, tmp_path):
    os.environ["PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS"] = "1"
    srv = Server(cfg, port=0)
    srv.start_thread()
    with playwright_api.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, channel="chromium",
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                  "--autoplay-policy=no-user-gesture-required"])
        # Never talk to a real minuta on the default port.
        ctx.route("http://127.0.0.1:47321/**", lambda r: r.abort())
        ctx.route("https://meet.google.com/**", lambda r: r.fulfill(body=MEET, content_type="text/html"))
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        ext_id = sw.url.split("/")[2]
        yield ctx, ext_id, srv, MeetingStore(config_mod.meetings_dir(cfg))
        ctx.close()
    srv.stop()


def wait(page, cond, seconds=20):
    # page.wait_for_timeout, not time.sleep: Playwright must keep pumping events meanwhile.
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return
        page.wait_for_timeout(200)
    raise AssertionError("condition not met in time")


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


def amplitude(x, freq, sr=16000):
    phase = np.arange(len(x)) * 2 * np.pi * freq / sr
    return 2 * abs(np.sum(x * np.exp(-1j * phase))) / len(x)


def test_pair_record_control_and_save(stack):
    expect = playwright_api.expect
    ctx, ext_id, srv, store = stack
    popup = ctx.new_page()
    popup.set_viewport_size({"width": 348, "height": 520})
    popup.goto(f"chrome-extension://{ext_id}/popup.html")

    # Not paired yet: the popup asks for a code. Point it at our server's port first.
    expect(popup.locator("#pairing")).to_be_visible(timeout=15000)
    popup.click("#settings-toggle")
    popup.fill("#port", str(srv.port))
    popup.click("#save-port")
    expect(popup.locator("#server")).to_contain_text(f"port {srv.port}", timeout=10000)
    popup.fill("#code", "000-000")
    popup.click("#pair")
    expect(popup.locator("#pair-error")).to_be_visible(timeout=10000)
    shot(popup, "1-not-paired")
    popup.fill("#code", pairing.new_code())
    popup.click("#pair")
    expect(popup.locator("#pairing")).to_be_hidden(timeout=10000)
    assert pairing.read_state()["allowed_origins"] == [f"chrome-extension://{ext_id}"]
    expect(popup.locator("#status")).to_have_text("Waiting for a Meet call")

    meet = ctx.new_page()
    meet.goto("https://meet.google.com/abc-defg-hij")
    meet.evaluate("join()")
    wait(meet, lambda: srv.app.last_audio)
    session = next(iter(srv.app.last_audio))
    m = store.get(session)
    assert m.read()["title"] == "Team sync" and m.read()["code"] == "abc-defg-hij"
    wait(meet, lambda: (m.read().get("diagnostics") or {}).get("max_participants") == 2)
    hb = srv.app.heartbeat
    assert hb["remoteCapture"] == "playback" and hb["sinkless"] is True and hb["muted"] is False

    popup.bring_to_front()
    expect(popup.locator("#status")).to_have_text("Recording", timeout=10000)
    expect(popup.locator("#meeting")).to_have_text("Team sync")
    bar = lambda which: popup.evaluate(f"parseFloat(document.getElementById('{which}-bar').style.width) || 0")  # noqa: E731
    wait(popup, lambda: bar("remote") > 30)
    wait(popup, lambda: bar("local") > 10)
    shot(popup, "2-recording")

    popup.click("#pause")
    expect(popup.locator("#status")).to_have_text("Paused", timeout=10000)
    wait(popup, lambda: '"paused": true' in (m.dir / "events.jsonl").read_text().replace("'", '"')
         if (m.dir / "events.jsonl").exists() else False)
    popup.click("#pause")
    expect(popup.locator("#status")).to_have_text("Recording", timeout=10000)
    popup.wait_for_timeout(6000)  # at least one more 5 s chunk

    popup.click("#finish")
    expect(popup.locator("#status")).to_have_text("This call is not being recorded", timeout=10000)
    wait(popup, lambda: m.status == "ready", seconds=30)
    d = m.read()
    assert d["audio"]["file"] == "audio.opus" and d["duration_seconds"] > 5
    assert d["audio_check"]["ok"] is True and d["destinations"]["local"]["meeting"]["status"] == "ok"
    left, right = audio.channel(m.audio_path, 0), audio.channel(m.audio_path, 1)
    tail = slice(-16000 * 2, None)
    assert amplitude(right[tail], 880) > 0.02  # the others
    assert np.abs(left[tail]).max() > 0.01  # you (Chromium's fake microphone beeps)
    assert amplitude(left[tail], 880) < 0.005  # the others never leak into your channel
    expect(popup.locator("#last")).to_contain_text("Team sync", timeout=40000)
    shot(popup, "3-finished")

    # Record again and discard: deleted, nothing kept.
    popup.click("#record")
    wait(popup, lambda: len([x for x in store.all() if x.id != session]) == 1)
    second = next(x for x in store.all() if x.id != session)
    expect(popup.locator("#status")).to_have_text("Recording", timeout=10000)
    popup.click("#discard")
    expect(popup.locator("#confirm")).to_be_visible()
    popup.click("#discard-yes")
    wait(popup, lambda: second.status == "discarded")
    assert not second.chunks_dir.exists() and second.audio_path is None
