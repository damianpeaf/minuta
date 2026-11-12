"""Transcription without network: helpers, the Vercel engine with a fake HTTP session, budget,
voice activity detection and the whole pipeline on synthetic audio with a fake engine."""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from minuta import audio
from minuta.meetings import MeetingStore
from minuta.transcription import common, vercel
from minuta.transcription.budget import Budget, BudgetExceeded, estimate
from minuta.transcription.export import markdown, vtt
from minuta.transcription.language import off_spanish
from minuta.transcription.pipeline import estimate_run, merge_turns, prompt_echo, transcribe

from .conftest import bursts, needs_ffmpeg, write_stereo

TCFG = {"language": "es-419", "context": "Weekly product meeting, in Spanish.", "glossary": ["Acme", "Zephyr", "OKR"]}


# --- language guard ----------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("Graças a Deus. E a Noah, ele não", "pt"),
    ("No, no, I don't understand the line.", "en"),
    ("Yeah. Yeah. Go go.", "en"),
    ("huh. Bye.", "en"),
    ("Okay. Bom dia. Obrigado, estou bem, nos vemos. Obrigado, tchau.", "pt"),
    ("Hay que hacer el deploy a staging y abrir el PR antes del commit.", None),
    ("sí, digamos que hay unos filtros ahí como para ver eso", None),
    ("Mm-hmm.", None),
])
def test_detects_phrases_out_of_spanish(text, expected):
    assert off_spanish(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("No, " * 120 + "Gracias.", "No, No, No, Gracias."),
    ("sí sí sí sí sí claro", "sí sí sí claro"),
    ("no, no, no, está bien", "no, no, no, está bien"),
    ("Ship it to staging.", "Ship it to staging."),
])
def test_collapse_loops(text, expected):
    assert common.collapse_loops(text) == expected


# --- words to phrases --------------------------------------------------------------
def w(text, start, end):
    return {"text": text, "start": start, "end": end}


def test_words_join_into_phrases_and_split_on_pauses():
    words = [w("the", 0.0, 0.2), w("issue", 0.25, 0.6), w("is", 0.65, 0.8),
             w("another", 2.0, 2.3), w("thing.", 2.35, 2.7)]
    f = common.phrases(words)
    assert [x["text"] for x in f] == ["the issue is", "another thing."]
    assert (f[0]["start"], f[0]["end"]) == (0.0, 0.8)


def test_long_phrase_splits_at_closing_mark():
    words = [w(f"p{i}", i * 0.5, i * 0.5 + 0.4) for i in range(60)]
    words[54]["text"] += "."
    f = common.phrases(words, longest=25.0)
    assert len(f) == 2 and f[0]["text"].endswith("p54.")


def test_punctuation_comes_from_full_text_when_it_matches():
    words = [w("hola", 0, 0.3), w("qué", 0.4, 0.6), w("tal", 0.7, 0.9)]
    assert [x["text"] for x in common.with_punctuation(words, "Hola, ¿qué tal?")] == ["Hola,", "¿qué", "tal?"]
    assert common.with_punctuation(words, "Hola, ¿qué tal estás?") == words


def test_turns_are_not_split_by_interjections_from_the_other_channel():
    segs = [
        {"start": 0, "end": 2, "speaker": "Ana", "text": "First part"},
        {"start": 1.5, "end": 1.8, "speaker": "Others", "text": "yes"},
        {"start": 2.5, "end": 4, "speaker": "Ana", "text": "second part."},
        {"start": 6, "end": 8, "speaker": "Others", "text": "A real answer with many words."},
    ]
    turns = merge_turns(segs)
    assert [(t["speaker"], t["text"]) for t in turns] == [
        ("Ana", "First part second part."), ("Others", "yes"), ("Others", "A real answer with many words.")]


def test_prompt_echo_is_detected():
    assert prompt_echo("Acme, Zephyr, OKR.", TCFG)
    assert prompt_echo("Weekly product meeting, in Spanish.", TCFG)
    assert not prompt_echo("We shipped Zephyr yesterday.", TCFG)


def test_prompt_combines_context_and_glossary():
    assert common.prompt(TCFG) == "Weekly product meeting, in Spanish. Acme, Zephyr, OKR."
    assert common.glossary({"glossary": "a, b ,"}) == ["a", "b"]
    assert common.prompt({}) == ""


# --- batches and compaction -----------------------------------------------------------
def test_batches_respect_the_window():
    segs = [(0, 10), (20, 30), (40, 50)]
    assert common.batches(segs, maximum=21) == [[(0, 10), (20, 30)], [(40, 50)]]
    assert common.compact_length([(0, 10), (20, 30)]) == 21


def test_compact_maps_times_back():
    x = np.ones(16000 * 30, dtype=np.float32)
    audio_c, real = common.compact(x, [(2, 4), (10, 12)])
    assert audio_c.size == 16000 * 5
    assert real(0.5) == 2.5 and real(3.5) == 10.5 and real(10) == 12


# --- Vercel engine with a fake HTTP session ---------------------------------------------
class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.lock = threading.Lock()

    def post(self, url, headers=None, json=None, timeout=None):
        with self.lock:
            self.requests.append({"url": url, "headers": headers, "json": json})
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_provider_options_per_model():
    assert vercel.provider_options("spacexai/grok-stt", TCFG)["xai"] == {
        "language": "es", "format": True, "keyterm": ["Acme", "Zephyr", "OKR"]}
    g = vercel.provider_options("google/gemini-3.5-transcribe", TCFG, word_timestamps=True)["google"]
    assert g["languageCodes"] == ["es-419"] and g["wordTimestamp"] is True and "customVocabulary" not in g
    g2 = vercel.provider_options("google/gemini-3.5-transcribe", TCFG, word_timestamps=False)["google"]
    assert g2["customVocabulary"] == ["Acme", "Zephyr", "OKR"] and "wordTimestamp" not in g2
    o = vercel.provider_options("openai/whisper-1", TCFG)["openai"]
    assert o["language"] == "es" and o["prompt"].startswith("Weekly") and o["timestampGranularities"] == ["segment"]
    assert "timestampGranularities" not in vercel.provider_options("openai/gpt-4o-transcribe", TCFG)["openai"]


def test_real_cost_and_segments_from_response():
    assert vercel.real_cost({"providerMetadata": {"gateway": {"cost": "0.0123"}}}) == 0.0123
    assert vercel.real_cost({}) is None
    words = {"text": "Hola, ¿qué tal?", "segments": [
        {"text": "hola", "startSecond": 0, "endSecond": 0.3}, {"text": "qué", "startSecond": 0.4, "endSecond": 0.6},
        {"text": "tal", "startSecond": 0.7, "endSecond": 0.9}]}
    assert vercel.segments_from(words, 5) == [{"start": 0.0, "end": 0.9, "text": "Hola, ¿qué tal?"}]
    assert vercel.segments_from({"text": "all of it"}, 7.5) == [{"start": 0.0, "end": 7.5, "text": "all of it"}]


def test_engine_builds_the_request_and_adjusts_budget():
    resp = FakeResponse(200, {"text": "Hola equipo", "segments": [
        {"text": "Hola equipo", "startSecond": 0.1, "endSecond": 1.2}],
        "providerMetadata": {"gateway": {"cost": 0.001}}})
    session = FakeSession([resp])
    eng = vercel.VercelEngine({"model": "openai/whisper-1"}, "vk_fake_key_123456", session)
    x = np.zeros(16000 * 10, dtype=np.float32)
    x[16000:16000 * 3] = 0.1
    budget = Budget(1.0)
    out = eng.transcribe_channel(x, [(1.0, 3.0)], TCFG, budget=budget, cost=lambda s: estimate(s, eng.usd_per_minute))
    req = session.requests[0]
    assert req["url"] == vercel.URL and req["headers"]["ai-model-id"] == "openai/whisper-1"
    assert req["headers"]["Authorization"] == "Bearer vk_fake_key_123456"
    assert req["json"]["mediaType"] == "audio/mpeg" and req["json"]["providerOptions"]["openai"]["language"] == "es"
    assert out == [{"start": 1.1, "end": 2.2, "text": "Hola equipo"}]
    assert budget.spent == pytest.approx(0.001)


def test_gemini_retries_without_region():
    session = FakeSession([FakeResponse(400, {"error": "bad language"}), FakeResponse(200, {"text": "ok"})])
    eng = vercel.VercelEngine({"model": "google/gemini-3.5-transcribe"}, "k" * 12, session)
    eng.request(b"mp3", TCFG)
    langs = [r["json"]["providerOptions"]["google"]["languageCodes"] for r in session.requests]
    assert langs == [["es-419"], ["es"]]


def test_with_the_default_config_each_model_keeps_its_preset():
    from minuta import config as config_mod
    from minuta.transcription import window_seconds

    cfg = config_mod.deep_merge(config_mod.DEFAULTS, {"transcription": {"vercel": {
        "model": "google/gemini-3.5-transcribe"}}})
    assert window_seconds("vercel", cfg) == 30
    session = FakeSession([FakeResponse(200, {"text": "ok"})])
    vercel.VercelEngine(cfg["transcription"]["vercel"], "k" * 12, session).request(b"mp3", TCFG)
    google = session.requests[0]["json"]["providerOptions"]["google"]
    assert "wordTimestamp" not in google and google["customVocabulary"] == ["Acme", "Zephyr", "OKR"]


def test_gateway_error_is_reported_without_the_key():
    session = FakeSession([FakeResponse(500, {"error": "down"})])
    eng = vercel.VercelEngine({"model": "openai/whisper-1"}, "vk_secret_abcdef", session)
    with pytest.raises(RuntimeError) as e:
        eng.request(b"mp3", TCFG)
    assert "500" in str(e.value) and "vk_secret_abcdef" not in str(e.value)


def test_engine_needs_a_key():
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        vercel.VercelEngine({"model": "openai/whisper-1"}, None)


def test_prices_and_presets():
    assert vercel.usd_per_minute({"model": "openai/whisper-1"}) == 0.006
    assert vercel.usd_per_minute({"model": "spacexai/grok-stt"}) == vercel.FALLBACK_USD_PER_MINUTE
    assert vercel.usd_per_minute({"model": "spacexai/grok-stt", "usd_per_minute": 0.002}) == 0.002
    assert vercel.option({"model": "google/gemini-3.5-transcribe"}, "word_timestamps", True) is False


# --- budget --------------------------------------------------------------------------
def test_budget_stops_before_the_cap():
    b = Budget(0.10)
    b.reserve(0.06)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.05)
    assert b.spent == pytest.approx(0.06)


def test_budget_is_thread_safe():
    b = Budget(1.0)
    ok = []

    def worker():
        try:
            b.reserve(0.3)
            ok.append(1)
        except BudgetExceeded:
            pass

    threads = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 3 and b.spent <= 1.0


def test_paid_engine_never_sends_past_the_cap():
    session = FakeSession([FakeResponse(200, {"text": "x"})])
    eng = vercel.VercelEngine({"model": "openai/whisper-1", "window_max_seconds": 10}, "k" * 12, session)
    x = np.full(16000 * 120, 0.1, dtype=np.float32)
    segs = [(i * 10.0, i * 10.0 + 9) for i in range(12)]
    budget = Budget(0.003)  # ~30 s at US$0.006/min
    with pytest.raises(BudgetExceeded):
        eng.transcribe_channel(x, segs, TCFG, budget=budget, cost=lambda s: estimate(s, 0.006))
    assert len(session.requests) <= 3


# --- voice activity detection ----------------------------------------------------------
def test_vad_finds_speech_and_ignores_clicks():
    sr = audio.SR
    x = np.zeros(sr * 60, dtype=np.float32)
    x[sr * 5: sr * 8] = 0.05 * np.sin(np.arange(sr * 3) / 5)  # 3 s of "speech"
    x[sr * 40: sr * 42] = 0.05 * np.sin(np.arange(sr * 2) / 5)
    x[sr * 20: sr * 20 + 1600] = 0.05  # 0.1 s click
    segs = audio.speech_segments(x)
    assert len(segs) == 2
    assert segs[0][0] == pytest.approx(4.7, abs=0.1) and segs[0][1] == pytest.approx(8.3, abs=0.1)


def test_vad_splits_long_speech_and_returns_nothing_for_silence():
    sr = audio.SR
    x = np.full(sr * 70, 0.05, dtype=np.float32)
    segs = audio.speech_segments(x)
    assert all(b - a <= 28.0 + 1e-6 for a, b in segs) and sum(b - a for a, b in segs) == pytest.approx(70, abs=0.2)
    assert audio.speech_segments(np.zeros(sr * 10, dtype=np.float32)) == []


def test_in_range_clips():
    assert audio.in_range([(0, 10), (20, 30)], (5, 25)) == [(5, 10), (20, 25)]


# --- the whole pipeline with a fake engine ------------------------------------------------
class FakeEngine:
    name = "fake"
    model = "fake-1"
    usd_per_minute = 0.0

    def __init__(self, texts):
        self.texts = texts
        self.calls = []

    def transcribe_channel(self, x, segments, tcfg, budget=None, cost=None):
        self.calls.append(list(segments))
        out = []
        for i, (a, b) in enumerate(segments):
            out.append({"start": a + 0.3, "end": b - 0.3, "text": self.texts[i % len(self.texts)]})
        return out


@pytest.fixture
def recorded(cfg, tmp_path):
    """A meeting with speech-like bursts: you at 1-3 s and 10-12 s, the others at 5-8 s."""
    store = MeetingStore(tmp_path / "meetings")
    m = store.get("2026-04-04T10-00-00-000Z_pip-elin-eok")
    m.update(status="ready", title="Pipeline", started_at="2026-04-04T10:00:00Z")
    write_stereo(m.dir / "audio.opus", bursts([(1, 3), (10, 12)], 15), bursts([(5, 8)], 15, freq=500),
                 codec="libopus")
    return m


@needs_ffmpeg
def test_pipeline_writes_json_md_vtt_and_delivers(cfg, recorded):
    from minuta.router import Router

    cfg["speakers"] = {"me": "Ana", "others": "Others"}
    me = FakeEngine(["Hello team.", "Thanks, bye."])
    doc = transcribe(recorded, cfg, "local", router=Router(cfg), engine=me)
    assert [len(c) for c in me.calls] == [2, 1]  # only the speech stretches, per channel
    assert [(t["speaker"], t["text"]) for t in doc["turns"]] == [
        ("Ana", "Hello team."), ("Others", "Hello team."), ("Ana", "Thanks, bye.")]
    files = recorded.transcript_paths
    assert set(files) == {"json", "md", "vtt"}
    assert json.loads(files["json"].read_text())["engine"] == {"name": "local", "model": "fake-1"}
    assert "] Ana:** Hello team." in files["md"].read_text()
    assert files["vtt"].read_text().startswith("WEBVTT") and "<v Others>" in files["vtt"].read_text()
    meta = recorded.read()
    assert meta["transcript_status"] == "done" and meta["transcript"]["turns"] == 3
    assert meta["destinations"]["local"]["transcript"]["status"] == "ok"


@needs_ffmpeg
def test_pipeline_drops_text_over_silence_and_prompt_echo(cfg, recorded):
    class Liar(FakeEngine):
        def transcribe_channel(self, x, segments, tcfg, budget=None, cost=None):
            return [{"start": 13.0, "end": 14.5, "text": "Made up over silence."},
                    {"start": 1.2, "end": 2.8, "text": "Acme, Zephyr, OKR."},
                    {"start": 1.2, "end": 2.8, "text": "real words"}]

    cfg["transcription"].update(glossary=["Acme", "Zephyr", "OKR"])
    doc = transcribe(recorded, cfg, "local", engine=Liar([]))
    assert {t["text"] for t in doc["turns"]} == {"real words"}


@needs_ffmpeg
def test_language_fallback_replaces_or_drops(cfg, recorded):
    first = FakeEngine(["Obrigado, tchau, estou bem.", "Yeah. Yeah. Go go."])

    class Fallback(FakeEngine):
        def __init__(self):
            super().__init__([])
            self.answers = iter(["Gracias, nos vemos.", "gracias", "Gracias, nos vemos."])
            self.usd_per_minute = 0

        def transcribe_channel(self, x, segments, tcfg, budget=None, cost=None):
            return [{"start": segments[0][0], "end": segments[0][1], "text": next(self.answers)}]

    cfg["transcription"]["language"] = "es"
    doc = transcribe(recorded, cfg, "local", engine=first, fallback=Fallback())
    texts = [t["text"] for t in doc["turns"]]
    assert "Gracias, nos vemos." in texts
    assert not any("Obrigado" in t or "Yeah" in t for t in texts)
    assert doc["language_fixes"] and doc["language_fixes"][0]["original"].startswith("Obrigado")


@needs_ffmpeg
def test_estimate_counts_only_speech(cfg, recorded):
    cfg["transcription"]["vercel"] = {"model": "openai/whisper-1"}
    est = estimate_run(recorded, cfg, "vercel")
    # ~4 s + ~3 s of speech plus margins and one 1 s gap between the two stretches of your channel.
    assert 7 < est["seconds"] < 12
    assert est["usd"] == pytest.approx(est["seconds"] / 60 * 0.006, abs=1e-4)
    assert estimate_run(recorded, cfg, "local") == {"engine": "local", "seconds": 0.0, "usd": 0.0}


@needs_ffmpeg
def test_transcribes_from_a_destination_when_local_audio_was_deleted(cfg, recorded, tmp_path):
    from minuta.router import Router

    cfg["destinations"] = {"local": {"type": "local", "copy_to": str(tmp_path / "sync")}}
    cfg["storage"]["delete_local_audio_after_upload"] = True
    r = Router(cfg)
    r.deliver_meeting(recorded)
    assert r.apply_delete_policy(recorded) and recorded.audio_path is None
    doc = transcribe(recorded, cfg, "local", router=r, engine=FakeEngine(["hi"]))
    assert doc["turns"] and recorded.audio_path is None
    assert not list(recorded.dir.glob("fetched*"))


def test_exports_format():
    doc = {"title": "T", "started_at": "2026", "duration_seconds": 3700, "url": "https://meet.google.com/x",
           "turns": [{"start": 3661.5, "end": 3663, "speaker": "A>b", "text": "hi"}]}
    assert "**[1:01:01] A>b:** hi" in markdown(doc)
    assert "01:01:01.500 --> 01:01:03.000\n<v Ab>hi" in vtt(doc)


def test_auto_device_needs_cublas_not_just_a_gpu(monkeypatch):
    from minuta.transcription import local

    monkeypatch.setattr(local, "is_apple_silicon", lambda: False)
    monkeypatch.setattr(local, "cuda_available", lambda: False)
    s = local.resolve_settings({"model": "auto", "device": "auto"})
    assert s == {"backend": "faster-whisper", "model": "small", "device": "cpu", "compute_type": "int8"}
    monkeypatch.setattr(local, "cuda_available", lambda: True)
    s = local.resolve_settings({"model": "auto", "device": "auto"})
    assert (s["model"], s["device"], s["compute_type"]) == ("large-v3-turbo", "cuda", "float16")


def test_vercel_key_is_checked_without_spending(monkeypatch):
    import http.server
    import threading

    from minuta.transcription import vercel

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            good = self.headers.get("Authorization") == "Bearer good-key-123"
            self.send_response(200 if good else 401)
            self.end_headers()
            self.wfile.write(b'{"balance": "4.50", "total_used": "0.50"}' if good else b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(vercel, "CREDITS_URL", f"http://127.0.0.1:{srv.server_port}/v1/credits")
        assert vercel.check_key("good-key-123") == ("ok", "4.50")
        assert vercel.check_key("bad-key-456") == ("invalid", None)
    finally:
        srv.shutdown()
    monkeypatch.setattr(vercel, "CREDITS_URL", "http://127.0.0.1:9/v1/credits")
    assert vercel.check_key("good-key-123")[0] == "unknown"


def test_pipeline_reports_its_steps_and_the_progress(cfg, stereo_webm, tmp_path):
    import shutil

    from minuta.meetings import MeetingStore
    from minuta.transcription.pipeline import transcribe

    class Engine:
        model = "fake"

        def transcribe_channel(self, x, segments, tcfg, budget=None, cost=None, progress=None):
            for _a, b in segments:
                progress(b)
            return [{"start": a, "end": b, "text": "hola"} for a, b in segments]

    m = MeetingStore(tmp_path / "meetings").get("2026-05-05T10-00-00-000Z_progress")
    m.update(status="ready")
    shutil.copy(stereo_webm, m.dir / "audio.webm")
    events = []
    transcribe(m, cfg, "local", engine=Engine(), on_progress=lambda step, f, info: events.append((step, f)))
    steps = [s for s, _ in events]
    assert steps[0] == "audio" and steps[-1] == "done"
    fractions = [f for s, f in events if s == "transcribe"]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0 and len(fractions) >= 3


def test_a_missing_model_is_downloaded_with_progress(monkeypatch, tmp_path):
    pytest.importorskip("huggingface_hub")
    import huggingface_hub
    import huggingface_hub.constants

    from minuta.transcription import local

    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(tmp_path))
    blobs = tmp_path / "models--org--tiny" / "blobs"

    def fake_download(repo, allow_patterns=None, local_files_only=False, **kw):
        if local_files_only:
            raise FileNotFoundError("not cached")
        blobs.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (blobs / f"part{i}").write_bytes(b"x" * 1000)
            time.sleep(0.35)
        return str(tmp_path / "snapshot")

    class Info:
        siblings = [type("F", (), {"rfilename": "model.bin", "size": 3000})()]

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_download)
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: type("A", (), {"model_info": lambda *a, **k: Info()})())
    events = []
    path = local.fetch_model("org/tiny", ["model.bin"], lambda step, f, **info: events.append((step, f, info)))
    assert path == str(tmp_path / "snapshot")
    assert all(s == "download" for s, _, _ in events) and events[-1][1] == 1.0
    assert events[-1][2]["total"] == 3000 and 0 < min(f for _, f, _ in events if f) < 1
