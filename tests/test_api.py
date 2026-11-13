"""The endpoints of the extension's page (api.py) on a real server, and the jobs behind them."""

from __future__ import annotations

import json
import threading
import time

import pytest

from minuta import cli, pairing, paths
from minuta import config as config_mod
from minuta.jobs import Jobs
from minuta.meetings import MeetingStore
from minuta.server import Server

from .conftest import bursts, needs_ffmpeg, write_stereo
from .test_server import EXT, call
from .webhook_receiver import Receiver

KEY = "signing-key-0123456789abcdef"
HEADER = "Authorization: Bearer header-secret-987654"


@pytest.fixture
def server(cfg):
    srv = Server(cfg, port=0, process_inline=True)
    srv.start_thread()
    cfg["server"]["port"] = srv.port  # the CLI finds this server, as it finds the service
    config_mod.save(cfg)
    yield srv
    srv.stop()


def api(srv, method, path, body=None, origin=None):
    return call(srv, method, path, body if body is not None or method == "GET" else {}, token=pairing.get_token(),
                origin=origin)


def meeting(cfg, mid="2026-05-05T10-00-00-000Z_api-test-one", title="API meeting", audio=True):
    m = MeetingStore(config_mod.meetings_dir(cfg)).get(mid)
    m.update(status="ready", title=title, started_at="2026-05-05T10:00:00Z", duration_seconds=6)
    if audio:
        write_stereo(m.dir / "audio.opus", bursts([(1, 3)], 6), bursts([(4, 5)], 6, freq=500))
    return m


def wait_job(srv, job_id, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        job = api(srv, "GET", f"/jobs/{job_id}")[1]["job"]
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


class Engine:
    """Says "hello" for every stretch of speech and reports how far it got. `gate`: waits for it first."""

    model = "fake-1"
    usd_per_minute = 0.0
    gate: threading.Event | None = None
    started: list = []

    def transcribe_channel(self, x, segments, tcfg, budget=None, cost=None, progress=None):
        Engine.started.append(time.time())
        if Engine.gate is not None:
            assert Engine.gate.wait(10)
        out = []
        for a, b in segments:
            out.append({"start": a + 0.2, "end": b - 0.2, "text": "hello"})
            if progress:
                progress(b)
        return out


@pytest.fixture
def engine(monkeypatch):
    Engine.gate, Engine.started = None, []
    monkeypatch.setattr("minuta.transcription.pipeline.create_engine", lambda *a, **k: Engine())
    return Engine


# --- protection ------------------------------------------------------------------------------------
NEW_ENDPOINTS = [("GET", "/app/status"), ("GET", "/doctor"), ("GET", "/meetings/x"), ("GET", "/jobs"),
                 ("GET", "/destinations"), ("GET", "/settings"), ("POST", "/settings"), ("POST", "/retry"),
                 ("POST", "/destinations"), ("POST", "/destinations/form"), ("POST", "/meetings/x/transcribe"),
                 ("POST", "/meetings/x/open"), ("POST", "/destinations/local/remove"), ("POST", "/jobs/x/cancel")]


@pytest.mark.parametrize("method, path", NEW_ENDPOINTS)
def test_every_new_endpoint_needs_the_token_and_the_paired_extension(server, method, path):
    body = {} if method == "POST" else None
    assert call(server, method, path, body)[0] == 401
    assert call(server, method, path, body, token="wrong")[0] == 401
    token = pairing.get_token()
    assert call(server, method, path, body, token=token, origin="https://evil.example")[0] == 403
    assert call(server, method, path, body, token=token, origin=EXT)[0] == 403  # not paired
    assert call(server, method, path, body, token=token, host="evil.example:80")[0] == 403


def test_the_paired_extension_page_is_answered(server):
    pairing.allow_origin(EXT)
    code, body = call(server, "GET", "/settings", token=pairing.get_token(), origin=EXT)
    assert code == 200 and body["recording_mode"] == "auto"


def test_texts_are_open_and_in_the_configured_language(server, cfg):
    code, body = call(server, "GET", "/i18n")
    assert code == 200 and body["language"] == "en" and body["strings"]["menu.status"] == "Status"
    api(server, "POST", "/settings", {"language": "es"})
    assert call(server, "GET", "/i18n")[1]["strings"]["menu.status"] == "Estado"
    assert call(server, "GET", "/i18n", origin="https://evil.example")[0] == 403


def test_bodies_of_the_page_are_small(server):
    code, _ = api(server, "POST", "/settings", {"name": "x" * (70 * 1024)})
    assert code == 413


# --- meetings ----------------------------------------------------------------------------------------
@needs_ffmpeg
def test_meeting_detail_engines_and_open(server, cfg, monkeypatch):
    m = meeting(cfg)
    code, d = api(server, "GET", f"/meetings/{m.id}")
    assert code == 200 and d["title"] == "API meeting" and d["transcribable"] and d["transcript_text"] is None
    assert d["folder"] == str(m.dir) and d["deliveries"] == {"meeting": [], "transcript": []}
    engines = api(server, "GET", f"/meetings/{m.id}/engines")[1]["engines"]
    assert engines[0]["engine"] == "local" and engines[0]["paid"] is False
    assert api(server, "GET", "/meetings/../etc")[0] == 404
    assert api(server, "GET", "/meetings/nope-123")[0] == 404
    opened = []
    monkeypatch.setattr("minuta.ui.open_with_system", lambda p: opened.append(p) or True)
    code, r = api(server, "POST", f"/meetings/{m.id}/open", {"what": "folder"})
    assert code == 200 and r["opened"] and opened == [m.dir]
    assert api(server, "POST", f"/meetings/{m.id}/open", {"what": "/etc/passwd"})[0] == 400
    assert api(server, "POST", f"/meetings/{m.id}/open", {"what": "transcript"})[0] == 400  # none yet
    listed = api(server, "GET", "/meetings")[1]["meetings"]
    assert [x["id"] for x in listed] == [m.id]


@needs_ffmpeg
def test_transcribe_as_a_job_with_progress(server, cfg, engine):
    m = meeting(cfg)
    code, out = api(server, "POST", f"/meetings/{m.id}/transcribe", {"engine": "local"})
    assert code == 202 and out["plan"]["paid"] is False
    job = wait_job(server, out["job"]["id"])
    assert job["state"] == "done", job
    assert job["steps"][:2] == ["audio", "transcribe"] and job["steps"][-1] == "done"
    assert job["result"]["turns"] >= 1 and m.has_transcript()
    assert job["done"][0] == "Audio ready" and any(x.startswith("Transcribed") for x in job["done"])
    d = api(server, "GET", f"/meetings/{m.id}")[1]
    assert d["transcript_text"]["turns"][0]["text"] == "hello"
    assert {x["destination"] for x in d["deliveries"]["transcript"]} == {"local"}


@needs_ffmpeg
def test_transcriptions_wait_in_line_and_a_queued_one_can_be_cancelled(server, cfg, engine):
    a, b, c = (meeting(cfg, f"2026-05-05T1{i}-00-00-000Z_api-q-{i}", f"M{i}") for i in range(3))
    engine.gate = threading.Event()
    ja = api(server, "POST", f"/meetings/{a.id}/transcribe", {})[1]["job"]
    jb = api(server, "POST", f"/meetings/{b.id}/transcribe", {})[1]["job"]
    jc = api(server, "POST", f"/meetings/{c.id}/transcribe", {})[1]["job"]
    assert jb["position"] >= 1 and jc["position"] >= 2
    # Asking again for the same meeting returns the same job.
    assert api(server, "POST", f"/meetings/{b.id}/transcribe", {})[1]["job"]["id"] == jb["id"]
    assert api(server, "POST", f"/jobs/{jc['id']}/cancel")[1]["job"]["state"] == "cancelled"
    time.sleep(0.3)
    assert api(server, "GET", f"/jobs/{jb['id']}")[1]["job"]["state"] == "queued"  # a is still running
    engine.gate.set()
    assert wait_job(server, ja["id"])["state"] == "done"
    assert wait_job(server, jb["id"])["state"] == "done"
    assert not c.has_transcript()
    assert len(engine.started) == 4  # two channels each for a and b, one meeting after the other


@needs_ffmpeg
def test_a_running_transcription_can_be_cancelled(server, cfg, engine):
    m = meeting(cfg)
    engine.gate = threading.Event()
    job = api(server, "POST", f"/meetings/{m.id}/transcribe", {})[1]["job"]
    while not engine.started:
        time.sleep(0.02)
    api(server, "POST", f"/jobs/{job['id']}/cancel")
    engine.gate.set()
    assert wait_job(server, job["id"])["state"] == "cancelled"
    assert not m.has_transcript() and m.read().get("transcript_status") is None


@needs_ffmpeg
def test_a_failing_transcription_reports_its_error(server, cfg, monkeypatch):
    class Broken:
        model = "x"

        def transcribe_channel(self, *a, **k):
            raise RuntimeError("the model exploded")

    monkeypatch.setattr("minuta.transcription.pipeline.create_engine", lambda *a, **k: Broken())
    m = meeting(cfg)
    job = wait_job(server, api(server, "POST", f"/meetings/{m.id}/transcribe", {})[1]["job"]["id"])
    assert job["state"] == "error" and "exploded" in job["error"]


@needs_ffmpeg
def test_a_paid_engine_asks_to_confirm_and_respects_the_cap(server, cfg, monkeypatch):
    m = meeting(cfg)
    runs = []
    monkeypatch.setattr("minuta.actions.transcribe", lambda *a, **k: runs.append(k) or {"id": m.id})
    code, out = api(server, "POST", f"/meetings/{m.id}/transcribe", {"engine": "vercel"})
    assert code == 409 and out["needs_confirmation"] and out["plan"]["estimate"]["usd"] > 0 and not runs
    code, out = api(server, "POST", f"/meetings/{m.id}/transcribe", {"engine": "vercel", "max_usd": 0.0000001,
                                                                     "confirm": True})
    assert code == 402 and "cap" in out["error"] and not runs
    code, out = api(server, "POST", f"/meetings/{m.id}/transcribe", {"engine": "vercel", "confirm": True})
    assert code == 202 and wait_job(server, out["job"]["id"])["state"] == "done"
    assert runs[0]["budget"].max == 1.0


def test_recordings_cannot_be_transcribed(server, cfg):
    m = meeting(cfg, audio=False)
    m.update(status="recording")
    assert api(server, "POST", f"/meetings/{m.id}/transcribe", {})[0] == 400


# --- retry --------------------------------------------------------------------------------------------
@needs_ffmpeg
def test_retry_sends_what_failed_as_a_job(server, cfg, tmp_path):
    with Receiver(KEY, responses=[(500, {}, b"down")] * 3) as rx:
        out = api(server, "POST", "/destinations", {"type": "webhook", "name": "hook", "answers": {
            "url": rx.url(), "signing_key": KEY, "headers": "", "_audio": False}})[1]
        assert out["ok"], out
        m = meeting(cfg)
        server.app.reload_config()
        server.app.router.deliver_meeting(m)
        assert m.read()["destinations"]["hook"]["meeting"]["status"] == "failed"
        code, out = api(server, "POST", "/retry", {})
        assert code == 202 and out["meetings"] == 1
        job = wait_job(server, out["job"]["id"])
    assert job["state"] == "done"
    rows = [(r["destination"], r["status"]) for r in job["result"]["rows"]]
    assert ("hook", "ok") in rows and all(status in ("ok", "skipped") for _, status in rows)
    assert api(server, "POST", "/retry", {"meetings": ["nothing-like-this"]})[0] == 404


# --- destinations --------------------------------------------------------------------------------------
def test_destinations_add_test_toggle_remove_and_never_return_secrets(server, cfg):
    with Receiver(KEY) as rx:
        answers = {"url": rx.url(), "signing_key": KEY, "headers": HEADER, "_audio": False}
        step = api(server, "POST", "/destinations/form", {"type": "webhook", "name": "hook", "answers": {}})[1]
        assert [f["key"] for f in step["fields"]] == ["url", "signing_key", "headers", "_audio"]
        step = api(server, "POST", "/destinations/form", {"type": "webhook", "name": "hook", "answers": answers})[1]
        assert step["complete"]
        code, out = api(server, "POST", "/destinations", {"type": "webhook", "name": "hook", "answers": answers})
        assert code == 200 and out["ok"]
        code, listed = api(server, "GET", "/destinations")
        hook = next(d for d in listed["configured"] if d["name"] == "hook")
        assert hook["secrets"] == {"signing_key": True, "headers": True}
        code, tested = api(server, "POST", "/destinations/hook/test", {"live": True})
        assert code == 200 and tested["ok"] and tested["live"]["ok"]
        assert rx.valid() and rx.requests[-1]["valid"]
    every_answer = json.dumps([listed, tested, out, step])
    assert KEY not in every_answer and "header-secret" not in every_answer
    assert KEY not in paths.config_file().read_text()
    assert api(server, "POST", "/destinations/hook/disable")[1]["enabled"] is False
    assert config_mod.load()["destinations"]["hook"]["enabled"] is False
    assert api(server, "POST", "/destinations/hook/enable")[1]["enabled"] is True
    assert api(server, "POST", "/destinations/hook/remove")[0] == 200
    assert "hook" not in config_mod.load()["destinations"]
    assert api(server, "POST", "/destinations/hook/remove")[0] == 404
    types = {x["type"]: x["label"] for x in api(server, "GET", "/destinations")[1]["types"]}
    assert types["webhook"].startswith("POST to a URL")


def test_a_destination_that_fails_its_check_is_not_added_unless_forced(server):
    answers = {"command": "no-such-program-xyz"}
    code, out = api(server, "POST", "/destinations", {"type": "command", "name": "run", "answers": answers})
    assert code == 400 and "not found" in out["message"]
    assert "run" not in config_mod.load()["destinations"]
    code, out = api(server, "POST", "/destinations", {"type": "command", "name": "run", "answers": answers,
                                                      "force": True})
    assert code == 200 and "run" in config_mod.load()["destinations"]
    code, out = api(server, "POST", "/destinations", {"type": "webhook", "name": "w", "answers": {"url": "x"}})
    assert code == 400 and out["errors"]["url"] and out["fields"]


# --- settings ------------------------------------------------------------------------------------------
def test_settings_change_what_the_terminal_sees(server, cfg, capsys, monkeypatch):
    code, s = api(server, "GET", "/settings")
    assert code == 200 and s["api_key_configured"] is False and "api_key" not in s
    code, out = api(server, "POST", "/settings", {"recording_mode": "manual", "name": "Ana", "engine": "vercel",
                                                  "vercel_model": "openai/gpt-4o-transcribe", "max_usd": 2.5})
    assert code == 200 and out["settings"]["recording_mode"] == "manual"
    saved = config_mod.load()
    assert saved["recording"]["mode"] == "manual" and saved["speakers"]["me"] == "Ana"
    assert saved["transcription"]["vercel"]["model"] == "openai/gpt-4o-transcribe"
    assert saved["transcription"]["max_usd"] == 2.5
    assert server.app.status()["recording_mode"] == "manual"  # what the extension asks for
    assert cli.main(["mode", "--non-interactive"]) == 0
    assert "only when you ask" in capsys.readouterr().out
    assert api(server, "POST", "/settings", {"recording_mode": "sometimes"})[0] == 400
    assert api(server, "POST", "/settings", {"port": 1})[0] == 400
    monkeypatch.setattr("minuta.transcription.vercel.check_key", lambda key: ("ok", "5.00"))
    secret = "vck_" + "z" * 40
    code, out = api(server, "POST", "/settings", {"api_key": secret})
    assert code == 200 and out["settings"]["api_key_configured"] is True and secret not in json.dumps(out)
    monkeypatch.setattr("minuta.transcription.vercel.check_key", lambda key: ("invalid", None))
    assert api(server, "POST", "/settings", {"api_key": "bad"})[0] == 400


def test_status_and_doctor(server, cfg):
    code, st = api(server, "GET", "/app/status")
    assert code == 200 and st["server"]["answering"] and st["meetings"]["total"] == 0
    checks = {c["check"]: c for c in api(server, "GET", "/doctor")[1]["checks"]}
    assert checks["server"]["status"] == "ok" and checks["config"]["label"] == "configuration"
    assert checks["extension"]["fix"] == "pair"


# --- the terminal uses the same jobs -------------------------------------------------------------------
@needs_ffmpeg
def test_the_terminal_transcribes_through_the_service_when_it_runs(server, cfg, engine, capsys):
    m = meeting(cfg)
    assert cli.main(["transcribe", m.id, "--engine", "local"]) == 0
    out = capsys.readouterr().out
    jobs = server.app.jobs.all()
    assert [j.kind for j in jobs] == ["transcribe"] and jobs[0].state == "done" and jobs[0].meeting == m.id
    assert "Audio ready" in out and "Transcript ready" in out


@needs_ffmpeg
def test_without_the_service_the_terminal_transcribes_by_itself(cfg, engine, capsys):
    cfg["server"]["port"] = 1  # nothing answers there
    config_mod.save(cfg)
    m = meeting(cfg)
    assert cli.main(["transcribe", m.id, "--engine", "local"]) == 0
    assert m.has_transcript() and "Audio ready" in capsys.readouterr().out


# --- jobs on their own ------------------------------------------------------------------------------------
def test_jobs_report_progress_and_keep_only_the_last_ones():
    jobs = Jobs()
    seen = threading.Event()

    def run(progress):
        progress("transcribe", 0.5, {"seconds": 3, "skip": object()})
        seen.set()
        return {"ok": True}

    j = jobs.submit("transcribe", run, meeting="m", serial=True)
    end = time.time() + 5
    while j.state != "done" and time.time() < end:
        time.sleep(0.01)
    d = j.to_dict()
    assert d["state"] == "done" and d["fraction"] == 0.5 and d["info"] == {"seconds": 3} and d["result"] == {"ok": True}
    json.dumps(d)
    for _ in range(60):
        jobs.submit("retry", lambda p: None)
    time.sleep(0.3)
    jobs.submit("retry", lambda p: None)
    assert len(jobs.all()) <= 52


def test_a_job_error_is_redacted():
    from minuta.redact import register_secret

    register_secret("super-secret-value-123")
    jobs = Jobs()

    def run(progress):
        raise RuntimeError("failed with super-secret-value-123")

    j = jobs.submit("retry", run)
    end = time.time() + 5
    while j.state != "error" and time.time() < end:
        time.sleep(0.01)
    assert j.state == "error" and "super-secret-value-123" not in j.error
