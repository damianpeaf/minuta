"""The extension's minuta page (app.html) in Chromium against the real server.

Pairs from the page, lists meetings, opens one, transcribes it watching the progress, adds a local
and a webhook destination from the generated forms, tests the webhook against a local receiver,
changes the recording mode and reads it back from the terminal. The fast version transcribes with a
fake engine; `-m "browser and slow"` uses the real local model `tiny` on a synthetic voice.

uv run pytest -m browser tests/test_app_page_e2e.py
"""

from __future__ import annotations

import os
import threading

import numpy as np
import pytest

from minuta import cli, pairing
from minuta import config as config_mod
from minuta.meetings import MeetingStore
from minuta.server import Server

from .conftest import ROOT, bursts, write_stereo
from .test_local_transcription_real import has_flite, speak
from .webhook_receiver import Receiver

pytestmark = pytest.mark.browser
playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
EXT = ROOT / "extension"
SHOTS = os.environ.get("MINUTA_SCREENSHOTS")
KEY = "page-signing-key-0123456789abcdef"


@pytest.fixture
def stack(cfg, tmp_path):
    srv = Server(cfg, port=0)
    srv.start_thread()
    cfg["server"]["port"] = srv.port  # the terminal talks to this server, as to the service
    config_mod.save(cfg)
    with playwright_api.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, channel="chromium",
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        ctx.route("http://127.0.0.1:47321/**", lambda r: r.abort())  # never a real minuta
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        ext_id = sw.url.split("/")[2]
        sw.evaluate(f"chrome.storage.local.set({{port: {srv.port}}})")
        yield ctx, ext_id, srv
        ctx.close()
    srv.stop()


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f"app-{name}.png"), full_page=True)


def recorded(cfg, voice: bool):
    m = MeetingStore(config_mod.meetings_dir(cfg)).get("2026-06-01T14-30-00-000Z_page-test-one")
    m.update(status="ready", title="Weekly review", started_at="2026-06-01T14:30:00Z", duration_seconds=12)
    if voice:
        sr = 48000
        me = speak("Good morning everyone. Let us review the budget.")
        others = speak("The report is ready and the numbers look good.")
        total = int(sr * 2 + len(me) + sr * 2 + len(others) + sr * 2)
        left, right = np.zeros(total, np.float32), np.zeros(total, np.float32)
        left[sr: sr + len(me)] = me
        start = sr + len(me) + 2 * sr
        right[start: start + len(others)] = others
        write_stereo(m.dir / "audio.opus", left, right)
    else:
        write_stereo(m.dir / "audio.opus", bursts([(1, 3)], 6), bursts([(4, 5)], 6, freq=500))
    old = MeetingStore(config_mod.meetings_dir(cfg)).get("2026-05-20T09-00-00-000Z_page-test-old")
    old.update(status="ready", title="Kickoff", started_at="2026-05-20T09:00:00Z", duration_seconds=1800)
    return m


class GatedEngine:
    """Holds the first channel until the test has seen the progress bar."""

    model = "fake-1"
    gate = threading.Event()

    def transcribe_channel(self, x, segments, tcfg, budget=None, cost=None, progress=None):
        assert self.gate.wait(20)
        out = []
        for a, b in segments:
            out.append({"start": a + 0.2, "end": b - 0.2, "text": "hello from the page"})
            progress(b)
        return out


def run_the_page(stack, cfg, real_model: bool):
    ctx, ext_id, srv = stack
    m = recorded(cfg, voice=real_model)
    page = ctx.new_page()
    page.set_viewport_size({"width": 1100, "height": 900})
    page.goto(f"chrome-extension://{ext_id}/app.html")

    # Pair from the page with a code from the terminal; the service worker picks the token up too.
    expect(page.locator("#pair")).to_be_visible(timeout=15000)
    page.fill("#pair-code", "000-000")
    page.click("#pair-form button")
    expect(page.locator("#pair-error")).to_be_visible()
    page.fill("#pair-code", pairing.new_code())
    page.click("#pair-form button")
    expect(page.locator("#meetings")).to_be_visible(timeout=10000)
    assert pairing.read_state()["allowed_origins"] == [f"chrome-extension://{ext_id}"]

    # Meetings, newest first, with a readable date.
    rows = page.locator("#meetings-table tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Weekly review")
    expect(rows.nth(0)).to_contain_text("2026")
    expect(rows.nth(1)).to_contain_text("30 min")
    shot(page, "1-meetings")

    rows.nth(0).locator("a").click()
    expect(page.locator("#meeting-title")).to_have_text("Weekly review")
    expect(page.locator("#transcript-empty")).to_be_visible()
    expect(page.locator("#engine option").first).to_contain_text("local")

    # Transcribe and watch the progress.
    page.click("#transcribe")
    expect(page.locator("#transcribe-progress")).to_be_visible(timeout=10000)
    if not real_model:
        expect(page.locator("#transcribe-progress .job-label")).to_contain_text("Transcribing", timeout=10000)
        expect(page.locator("#transcribe-progress progress")).to_be_visible()
        shot(page, "2-transcribing")
        GatedEngine.gate.set()
    expect(page.locator("#transcribe-progress")).to_contain_text("Audio ready", timeout=180000)
    expect(page.locator("#transcript li").first).to_be_visible(timeout=180000)
    assert m.has_transcript()
    turns = page.locator("#transcript li")
    text = " ".join(turns.all_inner_texts()).lower()
    if real_model:
        assert "budget" in text or "report" in text, text
    else:
        assert "hello from the page" in text
    expect(page.locator("#transcribe")).to_have_text("Transcribe again")
    shot(page, "3-meeting")

    # A local copy and a webhook, from the forms the server describes.
    page.click("#nav a[data-section=destinations]")
    expect(page.locator("#destinations-table tbody tr")).to_have_count(1)  # local
    page.select_option("#dest-type", "local")
    page.fill("#dest-name", "copies")
    page.click("#dest-start button[type=submit]")
    page.check("#f-_copy")
    page.click("#dest-submit")
    copies = str(config_mod.meetings_dir(cfg).parent / "copies")
    page.fill("#f-copy_to", copies)
    page.click("#dest-submit")
    expect(page.locator("#dest-result")).to_contain_text("copies: added", timeout=10000)
    assert config_mod.load()["destinations"]["copies"]["copy_to"] == copies

    with Receiver(KEY) as rx:
        page.select_option("#dest-type", "webhook")
        page.fill("#dest-name", "hook")
        page.click("#dest-start button[type=submit]")
        page.fill("#f-url", "ftp://nope")
        page.fill("#f-signing_key", KEY)
        page.click("#dest-submit")
        expect(page.locator("#f-url-error")).to_be_visible()
        page.fill("#f-url", rx.url())
        page.fill("#f-signing_key", KEY)  # a secret is typed again, never sent back to the page
        page.click("#dest-submit")
        expect(page.locator("#dest-result")).to_contain_text("hook: added", timeout=10000)
        row = page.locator("#destinations-table tbody tr", has_text="hook")
        expect(row).to_contain_text("signing_key: saved")
        shot(page, "4-destinations")
        row.get_by_role("button", name="Send a test").click()
        expect(page.locator("#dest-result")).to_contain_text("live test ok", timeout=15000)
        assert rx.valid() and rx.valid()[-1]["event"] == "ping"
    saved = config_mod.load()["destinations"]["hook"]
    assert saved["url"] == rx.url() and saved["signing_key_secret"] == "HOOK_SIGNING_KEY"
    assert KEY not in page.content()

    # The recording mode, changed here, is what the terminal and the extension see.
    page.click("#nav a[data-section=settings]")
    expect(page.locator("#s-recording_mode")).to_have_value("auto")
    page.select_option("#s-recording_mode", "manual")
    page.click("#settings-form button[type=submit]")
    expect(page.locator("#settings-result")).to_have_text("Saved.", timeout=10000)
    assert config_mod.load()["recording"]["mode"] == "manual"
    assert srv.app.status()["recording_mode"] == "manual"
    shot(page, "5-settings")

    page.click("#nav a[data-section=status]")
    expect(page.locator("#doctor-table tbody tr").first).to_be_visible(timeout=20000)
    expect(page.locator("#status-facts")).to_contain_text(f"127.0.0.1:{srv.port}")
    shot(page, "6-status")
    return page


def test_the_page_does_what_the_menu_does(stack, cfg, monkeypatch, capsys):
    GatedEngine.gate = threading.Event()
    monkeypatch.setattr("minuta.transcription.pipeline.create_engine", lambda *a, **k: GatedEngine())
    run_the_page(stack, cfg, real_model=False)
    capsys.readouterr()
    assert cli.main(["mode", "--non-interactive"]) == 0
    assert "only when you ask" in capsys.readouterr().out


@pytest.mark.slow
@pytest.mark.skipif(not has_flite(), reason="ffmpeg without flite")
def test_the_page_transcribes_with_the_real_tiny_model(stack, cfg):
    from minuta.transcription.local import is_apple_silicon

    pytest.importorskip("mlx_whisper" if is_apple_silicon() else "faster_whisper")
    cfg["transcription"].update(language="en", local={"model": "tiny", "device": "auto"})
    config_mod.save(cfg)
    stack[2].app.reload_config()
    run_the_page(stack, cfg, real_model=True)


def test_the_popup_opens_the_page(stack):
    ctx, ext_id, _ = stack
    popup = ctx.new_page()
    popup.goto(f"chrome-extension://{ext_id}/popup.html")
    with ctx.expect_page() as opened:
        popup.click("#open-app")
    page = opened.value
    page.wait_for_load_state()
    assert page.url == f"chrome-extension://{ext_id}/app.html"
    expect(page.locator("#pair")).to_be_visible(timeout=15000)
