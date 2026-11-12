"""The webhook destination against real HTTP and HTTPS servers on 127.0.0.1."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time

import pytest

from minuta.destinations import DestinationError, MeetingParts, PermanentError
from minuta.destinations.webhook import MultipartBody, WebhookDestination, parse_headers, sign, url_problem
from minuta.meetings import MeetingStore
from minuta.router import Router

from .webhook_receiver import Receiver, doc_snippet, replay, self_signed_cert

KEY = "test-signing-key-0123456789abcdef"
TOKEN = "Bearer tok_live_abcdef0123456789"


class Secrets:
    def __init__(self, values=None):
        self.values = {"HOOK_SIGNING_KEY": KEY, **(values or {})}

    def get(self, name):
        return self.values.get(name)


def hook(url: str, secrets=None, **opts) -> WebhookDestination:
    options = {"type": "webhook", "url": url, "signing_key_secret": "HOOK_SIGNING_KEY",
               "send": ["metadata", "transcript", "audio"], **opts}
    return WebhookDestination("hook", options, secrets or Secrets())


def router_for(url: str, sleeps: list) -> Router:
    """The router retrying a webhook (adapters do not retry by themselves)."""
    cfg = {"destinations": {"hook": hook(url).options}, "delivery": {"max_attempts": 3}}
    return Router(cfg, Secrets(), sleep=sleeps.append, factory=lambda name, opts, secrets: hook(url))


def make_meeting(tmp_path, audio_bytes: int = 4096, transcript: bool = False):
    m = MeetingStore(tmp_path / "meetings").get("2026-02-02T09-00-00-000Z_xyz-abcd-efg")
    m.update(status="ready", title="Weekly sync", started_at="2026-02-02T09:00:00+00:00", duration_seconds=90,
             url="https://meet.google.com/xyz-abcd-efg")
    (m.dir / "audio.opus").write_bytes(bytes(range(256)) * (audio_bytes // 256))
    if transcript:
        write_transcript(m)
    return m


def write_transcript(m):
    doc = {"title": "Weekly sync", "turns": [{"start": 0.0, "end": 2.0, "speaker": "Me", "text": "hola"}]}
    (m.dir / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    (m.dir / "transcript.md").write_text("# Weekly sync\n\n**[00:00] Me:** hola\n", encoding="utf-8")
    return m.transcript_paths


# --- options and check -------------------------------------------------------------------
@pytest.mark.parametrize("url,ok", [
    ("https://example.com/x", True),
    ("http://127.0.0.1:5678/webhook/abc", True),
    ("http://localhost:5678/x", True),
    ("http://[::1]:5678/x", True),
    ("http://example.com/x", False),
    ("http://127.0.0.1.nip.io/x", False),
    ("http://localhost.example.com/x", False),
    ("ftp://example.com/x", False),
    ("https://user:pass@example.com/x", False),
    ("https:///nohost", False),
    ("", False),
])
def test_only_https_or_loopback_http(url, ok):
    assert (url_problem(url) is None) is ok


def test_check_needs_a_signing_key_and_valid_headers():
    assert hook("https://example.com/x").check().ok
    assert not hook("https://example.com/x", Secrets({"HOOK_SIGNING_KEY": None})).check().ok
    assert not hook("https://example.com/x", Secrets({"HOOK_SIGNING_KEY": "short"})).check().ok
    bad = Secrets({"H": "X-Minuta-Signature: forged"})
    assert "set by minuta" in hook("https://example.com/x", bad, headers_secret="H").check().message
    assert not hook("https://example.com/x", Secrets({"H": "Bad Header: x"}), headers_secret="H").check().ok
    assert not hook("https://example.com/x", max_audio_mb="lots").check().ok


def test_parse_headers_accepts_lines_and_json():
    assert parse_headers("Authorization: Bearer x\nX-Team: a:b") == {"Authorization": "Bearer x", "X-Team": "a:b"}
    assert parse_headers('{"Authorization": "Bearer x"}') == {"Authorization": "Bearer x"}
    with pytest.raises(ValueError):
        parse_headers('{"X": "a\\r\\nInjected: 1"}')


def test_plain_http_to_another_host_is_refused_without_any_request(tmp_path):
    with Receiver(KEY) as recv:
        d = hook(recv.url(host="127.0.0.1").replace("127.0.0.1", "127.0.0.1.nip.io"))
        assert not d.check().ok
        with pytest.raises(PermanentError, match="only allowed"):
            d.send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))
        assert recv.requests == []


# --- signing, payload, idempotency -------------------------------------------------------
def test_signed_json_with_transcript_and_idempotency_key(tmp_path):
    m = make_meeting(tmp_path, transcript=True)
    with Receiver(KEY) as recv:
        d = hook(recv.url(), Secrets({"HOOK_HEADERS": f"Authorization: {TOKEN}"}), headers_secret="HOOK_HEADERS",
                 send=["metadata", "transcript"])
        r1 = d.send_meeting(m, MeetingParts(metadata={"id": m.id, "title": "Weekly sync"},
                                            transcript=m.transcript_paths))
        r2 = d.send_transcript(m, m.transcript_paths, previous=r1)
        r3 = d.send_transcript(m, m.transcript_paths, previous=r2)  # a retry of the same step
    first, second, third = recv.requests
    assert all(r["valid"] and not r["replay"] for r in recv.requests)
    assert first["event"] == "meeting.ready" and first["headers"]["Authorization"] == TOKEN
    assert first["headers"]["Content-Type"] == "application/json"
    assert first["payload"]["meeting"]["title"] == "Weekly sync"
    assert first["payload"]["transcript"]["turns"][0]["text"] == "hola"
    assert first["payload"]["transcript_markdown"].startswith("# Weekly sync")
    assert first["key"] == f"{m.id}.meeting.ready" == first["payload"]["delivery_id"]
    assert second["event"] == "transcript.ready" and second["key"] == f"{m.id}.transcript.ready"
    assert second["payload"]["meeting"]["duration_seconds"] == 90  # metadata comes from meeting.json
    assert second["payload"]["previous_remote_id"] == r1.remote_id
    # Same key on the retry, so the receiver can ignore it; the signature is new each time.
    assert len({r["payload"]["request_id"] for r in recv.requests}) == 3
    assert third["key"] == second["key"] and third["duplicate"]
    assert third["headers"]["X-Minuta-Signature"] != second["headers"]["X-Minuta-Signature"]
    assert r1.remote_id == "remote-1" and r2.remote_id == r3.remote_id == "remote-2"


def test_a_captured_request_cannot_be_replayed_or_altered(tmp_path):
    m = make_meeting(tmp_path)
    with Receiver(KEY) as recv:
        hook(recv.url()).send_meeting(m, MeetingParts(metadata={"id": m.id}))
        original = recv.requests[0]
        assert replay(recv.url(), original) == 409  # same signature twice
        tampered = {**original, "body": original["body"].replace(b'"id"', b'"ID"')}
        assert replay(recv.url(), tampered) == 401
        old_ts = str(int(time.time()) - 3600)
        stale = {**original, "headers": {**original["headers"], "X-Minuta-Timestamp": old_ts,
                                         "X-Minuta-Signature": sign(KEY, old_ts, original["body"])}}
        assert replay(recv.url(), stale) == 401  # correctly signed, but an hour old


def test_wrong_key_is_rejected_by_the_receiver_and_is_permanent(tmp_path):
    with Receiver("another-key-0123456789abcdef") as recv:
        with pytest.raises(PermanentError, match="401"):
            hook(recv.url()).send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))
        assert len(recv.requests) == 1  # a 4xx is not retried


def test_audio_goes_as_multipart_and_is_signed_over_the_raw_body(tmp_path):
    m = make_meeting(tmp_path, audio_bytes=300_000)
    with Receiver(KEY) as recv:
        r = hook(recv.url(), max_audio_mb=1).send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    req = recv.requests[0]
    assert req["valid"] and req["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert req["audio"] == m.audio_path.read_bytes() and req["audio_filename"] == "audio.opus"
    assert req["payload"]["audio"] == {"included": True, "file": "audio.opus", "bytes": m.audio_path.stat().st_size,
                                       "sha256": hashlib.sha256(m.audio_path.read_bytes()).hexdigest()}
    assert r.audio is False and r.extra["audio_sent"] is True  # a webhook never allows deleting the local audio


def test_audio_over_the_limit_is_left_out(tmp_path):
    m = make_meeting(tmp_path, audio_bytes=2 * 1024 * 1024)
    with Receiver(KEY) as recv:
        r = hook(recv.url(), max_audio_mb=1).send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    req = recv.requests[0]
    assert req["valid"] and req["headers"]["Content-Type"] == "application/json" and req["audio"] is None
    assert req["payload"]["audio"]["included"] is False and req["payload"]["audio"]["reason"] == "too_large"
    assert len(req["body"]) < 10_000 and r.extra["audio_sent"] is False


def test_multipart_body_streams_the_same_bytes_it_signs(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"\x00\xff" * 100_000)
    body = MultipartBody(b'{"a": 1}', audio)
    whole = b"".join(body.chunks())
    assert len(body) == len(whole)
    read = b""
    while chunk := body.read(7777):
        read += chunk
    assert read == whole and sign(KEY, "1", whole) == sign(KEY, "1", MultipartBody(b'{"a": 1}', audio,
                                                                                    body.boundary).chunks())


# --- failures and retries ----------------------------------------------------------------
def test_429_and_5xx_are_retryable_and_carry_retry_after(tmp_path):
    with Receiver(KEY, responses=[(429, {"Retry-After": "3"}, b"slow down"), (500, {}, b"boom")]) as recv:
        for wait, text in ((3.0, "429: slow down"), (None, "500: boom")):
            with pytest.raises(DestinationError, match=text) as e:
                hook(recv.url()).send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))
            assert not isinstance(e.value, PermanentError) and e.value.retry_after == wait
    assert len(recv.requests) == 2  # one request per call: the router decides when to try again


def test_the_router_waits_for_retry_after_then_backs_off(tmp_path, monkeypatch):
    monkeypatch.setattr("minuta.router.BACKOFF", 2.0)
    sleeps = []
    m = make_meeting(tmp_path)
    with Receiver(KEY, responses=[(429, {"Retry-After": "3"}, b"slow down"), (503, {}, b"busy")]) as recv:
        result = router_for(recv.url(), sleeps).deliver_meeting(m)["hook"]
    assert result["status"] == "ok" and result["receipt"]["remote_id"] == "remote-1" and len(recv.requests) == 3
    assert sleeps == [3.0, 4.0]  # Retry-After first, then exponential backoff
    assert len({q["key"] for q in recv.requests}) == 1


def test_retry_after_longer_than_a_minute_is_left_for_minuta_retry(tmp_path):
    sleeps = []
    m = make_meeting(tmp_path)
    with Receiver(KEY, responses=[(429, {"Retry-After": "7200"}, b"")]) as recv:
        result = router_for(recv.url(), sleeps).deliver_meeting(m)["hook"]
    assert result["status"] == "failed" and not result["permanent"] and "asked to wait 7200" in result["error"]
    assert sleeps == [] and len(recv.requests) == 1


def test_timeout_is_retryable_and_bounded(tmp_path):
    with Receiver(KEY, delay=3) as recv:
        start = time.monotonic()
        with pytest.raises(DestinationError, match="ReadTimeout") as e:
            hook(recv.url(), timeout_seconds=1).send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))
        assert time.monotonic() - start < 2.5
    assert not isinstance(e.value, PermanentError)


def test_connection_refused_is_retryable(tmp_path):
    with Receiver(KEY) as recv:
        url = recv.url()
    with pytest.raises(DestinationError, match="could not reach") as e:
        hook(url).send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))
    assert not isinstance(e.value, PermanentError)


# --- redirects ---------------------------------------------------------------------------
def test_redirect_to_another_host_is_not_followed(tmp_path):
    with Receiver(KEY) as evil, Receiver(KEY) as recv:
        recv.responses = [(307, {"Location": evil.url("/steal")}, b"")]
        with pytest.raises(PermanentError, match="another host"):
            hook(recv.url()).send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))
        assert evil.requests == [] and len(recv.requests) == 1


def test_redirect_from_https_to_http_on_the_same_host_is_not_followed(tmp_path, monkeypatch):
    cert, key = self_signed_cert(tmp_path)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    with Receiver(KEY, tls=(cert, key)) as recv:
        recv.responses = [(308, {"Location": recv.url("/plain").replace("https://", "http://")}, b"")]
        with pytest.raises(PermanentError, match="another host"):
            hook(recv.url()).send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))


def test_same_host_307_is_followed_and_302_is_refused(tmp_path):
    m = make_meeting(tmp_path, audio_bytes=50_000)
    with Receiver(KEY) as recv:
        recv.responses = [(307, {"Location": "/moved"}, b"")]
        r = hook(recv.url()).send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
        assert [q["path"] for q in recv.requests] == ["/hook", "/moved"] and r.remote_id == "remote-1"
        assert recv.requests[1]["audio"] == m.audio_path.read_bytes() and recv.requests[1]["valid"]
        recv.responses = [(302, {"Location": "/moved"}, b"")]
        with pytest.raises(PermanentError, match="POST into a GET"):
            hook(recv.url()).send_meeting(m, MeetingParts(metadata={"id": m.id}))


# --- TLS ---------------------------------------------------------------------------------
def test_https_with_a_trusted_certificate(tmp_path, monkeypatch):
    cert, key = self_signed_cert(tmp_path)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))  # trusted only inside this test
    m = make_meeting(tmp_path, audio_bytes=20_000)
    with Receiver(KEY, tls=(cert, key)) as recv:
        for host in ("127.0.0.1", "localhost"):
            hook(recv.url(host=host)).send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    assert len(recv.valid()) == 2 and all(r["audio"] == m.audio_path.read_bytes() for r in recv.requests)


def test_https_with_an_untrusted_certificate_fails_for_good(tmp_path, monkeypatch):
    cert, key = self_signed_cert(tmp_path)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    with Receiver(KEY, tls=(cert, key)) as recv:
        with pytest.raises(PermanentError, match="TLS verification failed"):
            hook(recv.url()).send_meeting(make_meeting(tmp_path), MeetingParts(metadata={"id": "x"}))
        assert recv.requests == []


# --- the documented verification snippets ------------------------------------------------
def test_the_node_snippet_verifies_a_real_request(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    m = make_meeting(tmp_path, audio_bytes=10_000)
    with Receiver(KEY) as recv:
        hook(recv.url()).send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    req = recv.requests[0]
    (tmp_path / "verify.js").write_text(doc_snippet("js"), encoding="utf-8")
    (tmp_path / "body.bin").write_bytes(req["body"])
    headers = {k.lower(): v for k, v in req["headers"].items()}
    runner = tmp_path / "run.js"
    runner.write_text(
        "const fs = require('fs'); const { verify } = require('./verify.js');\n"
        "const [secret, headersFile, bodyFile] = process.argv.slice(2);\n"
        "const h = JSON.parse(fs.readFileSync(headersFile, 'utf8')); const body = fs.readFileSync(bodyFile);\n"
        "process.stdout.write(String(verify(secret, h, body)));\n", encoding="utf-8")

    def run(secret, hdrs):
        (tmp_path / "h.json").write_text(json.dumps(hdrs), encoding="utf-8")
        return subprocess.run(["node", str(runner), secret, str(tmp_path / "h.json"), str(tmp_path / "body.bin")],
                              capture_output=True, text=True, check=True).stdout

    assert run(KEY, headers) == "true"
    assert run("wrong-key-0123456789abcdef", headers) == "false"
    assert run(KEY, {**headers, "x-minuta-timestamp": str(int(headers["x-minuta-timestamp"]) - 3600)}) == "false"


def test_ping_is_signed(tmp_path):
    with Receiver(KEY) as recv:
        hook(recv.url()).ping()
    assert recv.requests[0]["event"] == "ping" and recv.requests[0]["valid"]
