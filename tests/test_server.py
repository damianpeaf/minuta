"""Local server: host/origin/token checks, pairing, and a whole meeting from chunks to the meetings folder."""

from __future__ import annotations

import base64
import json
import os
import socket
import stat
import time
import urllib.error
import urllib.request

import pytest

from minuta import config as config_mod
from minuta import pairing, paths
from minuta import server as server_mod
from minuta.meetings import MeetingStore
from minuta.server import Server

from .conftest import needs_ffmpeg

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


@pytest.fixture
def server(cfg):
    srv = Server(cfg, port=0, process_inline=True)
    srv.start_thread()
    yield srv
    srv.stop()


def call(srv, method, path, body=None, token=None, origin=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header(pairing.TOKEN_HEADER, token)
    if origin:
        req.add_header("Origin", origin)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_health_is_open_but_says_little(server):
    code, body = call(server, "GET", "/health")
    assert code == 200 and body["app"] == "minuta" and body["paired"] is False
    assert set(body) == {"ok", "app", "version", "paired"}


def test_requests_without_or_with_wrong_token_get_401(server):
    assert call(server, "GET", "/status")[0] == 401
    assert call(server, "GET", "/status", token="nope")[0] == 401
    assert call(server, "POST", "/chunk", {"session": "s1", "seq": 0, "data": ""})[0] == 401
    assert call(server, "GET", "/status", token=pairing.get_token())[0] == 200


def test_web_pages_and_rebinding_hosts_are_refused(server):
    token = pairing.get_token()
    assert call(server, "GET", "/health", origin="https://evil.example")[0] == 403
    assert call(server, "GET", "/status", token=token, origin="https://meet.google.com")[0] == 403
    assert call(server, "GET", "/health", host="evil.example:80")[0] == 403
    assert call(server, "OPTIONS", "/chunk")[0] == 403


def test_pairing_with_a_one_time_code(server):
    code = pairing.new_code()
    # Only an extension may pair, even with the right code.
    assert call(server, "POST", "/pair", {"code": code})[0] == 403
    assert call(server, "POST", "/pair", {"code": code}, origin="https://evil.example")[0] == 403
    assert call(server, "POST", "/pair", {"code": "000-000"}, origin=EXT)[0] == 401
    status, body = call(server, "POST", "/pair", {"code": code}, origin=EXT)
    assert status == 200 and body["token"] == pairing.get_token()
    # The code works once.
    assert call(server, "POST", "/pair", {"code": code}, origin=EXT)[0] == 401
    # The paired extension is accepted; another extension with the same token is not.
    assert call(server, "GET", "/status", token=body["token"], origin=EXT)[0] == 200
    other = "chrome-extension://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    assert call(server, "GET", "/status", token=body["token"], origin=other)[0] == 403
    assert call(server, "GET", "/health")[1]["paired"] is True


def test_code_dies_after_too_many_wrong_guesses(server):
    code = pairing.new_code()
    for _ in range(pairing.MAX_ATTEMPTS):
        assert call(server, "POST", "/pair", {"code": "999-999"}, origin=EXT)[0] == 401
    assert call(server, "POST", "/pair", {"code": code}, origin=EXT)[0] == 401


def test_expired_code_is_refused(server):
    code = pairing.new_code(ttl=-1)
    assert call(server, "POST", "/pair", {"code": code}, origin=EXT)[0] == 401


def test_rotating_the_token_unpairs(server):
    code = pairing.new_code()
    old = call(server, "POST", "/pair", {"code": code}, origin=EXT)[1]["token"]
    new = pairing.rotate_token()
    assert new != old
    assert call(server, "GET", "/status", token=old)[0] == 401
    assert call(server, "GET", "/status", token=new, origin=EXT)[0] == 403  # must pair again


def test_bad_input_is_rejected(server):
    token = pairing.get_token()
    assert call(server, "POST", "/start", {"session": "../etc"}, token=token)[0] == 400
    assert call(server, "POST", "/chunk", {"session": "ok-id", "seq": 0, "data": "@@notb64"}, token=token)[0] == 400
    assert call(server, "POST", "/nope", {"session": "ok-id"}, token=token)[0] == 404


def test_ping_and_heartbeat_mark_the_extension_alive(server):
    token = pairing.get_token()
    before = time.time()
    assert call(server, "POST", "/ping", {}, token=token)[0] == 200
    st = call(server, "GET", "/status", token=token)[1]
    assert st["last_seen"] >= before and st["last_heartbeat"] is None
    call(server, "POST", "/heartbeat", {"recording": False, "inCall": False}, token=token)
    assert call(server, "GET", "/status", token=token)[1]["last_heartbeat"]


@needs_ffmpeg
def test_a_whole_meeting_ends_up_in_the_meetings_folder(server, cfg, stereo_webm):
    token = pairing.get_token()
    session = "2026-03-03T10-00-00-000Z_abc-defg-hij"
    data = stereo_webm.read_bytes()
    half = len(data) // 2
    assert call(server, "POST", "/start", {"session": session, "code": "abc-defg-hij", "title": "Standup",
                                           "url": "https://meet.google.com/abc-defg-hij",
                                           "startedAt": "2026-03-03T10:00:00Z"}, token=token)[0] == 200
    for seq, part in enumerate((data[:half], data[half:])):
        assert call(server, "POST", "/chunk", {"session": session, "seq": seq,
                                               "data": base64.b64encode(part).decode()}, token=token)[0] == 200
    call(server, "POST", "/heartbeat", {"session": session, "recording": True, "remoteTracks": 1,
                                        "participantCount": 2, "remoteSignal": True, "localTrack": True,
                                        "muted": False, "inCall": True, "ctxState": "running"}, token=token)
    call(server, "POST", "/mute", {"session": session, "muted": True, "t": "2026-03-03T10:00:01Z"}, token=token)
    assert call(server, "GET", "/status", token=token)[1]["recording"] == [session]
    assert call(server, "POST", "/end", {"session": session}, token=token)[0] == 200

    m = MeetingStore(config_mod.meetings_dir(cfg)).get(session)
    d = m.read()
    assert d["status"] == "ready", d.get("error")
    assert d["title"] == "Standup" and d["audio"]["file"] == "audio.opus"
    assert 2.5 < d["duration_seconds"] < 3.5
    assert d["audio_check"]["ok"] is True and d["diagnostics"]["max_participants"] == 2
    assert d["destinations"]["local"]["meeting"]["status"] == "ok"
    assert (m.dir / "audio.opus").exists() and not (m.dir / "chunks").exists()
    assert "mute" in (m.dir / "events.jsonl").read_text()
    latest = call(server, "GET", "/latest", token=token)[1]["meeting"]
    assert latest["id"] == session and latest["status"] == "ready"


@needs_ffmpeg
def test_a_destination_added_while_the_server_runs_gets_the_next_meeting(server, cfg, stereo_webm, tmp_path):
    from minuta import cli

    assert cli.main(["destinations", "add", "later", "--type", "local", "--non-interactive",
                     "--option", f"copy_to={tmp_path / 'later'}"]) == 0
    token = pairing.get_token()
    s = "2026-03-03T16-00-00-000Z_reload"
    call(server, "POST", "/start", {"session": s}, token=token)
    data = base64.b64encode(stereo_webm.read_bytes()).decode()
    call(server, "POST", "/chunk", {"session": s, "seq": 0, "data": data}, token=token)
    call(server, "POST", "/end", {"session": s}, token=token)
    m = MeetingStore(config_mod.meetings_dir(cfg)).get(s)
    assert m.read()["destinations"]["later"]["meeting"]["status"] == "ok"
    assert any((tmp_path / "later").iterdir())


@needs_ffmpeg
def test_mp3_format_keeps_both_channels(cfg, stereo_webm):
    from minuta import audio

    cfg["general"]["audio_format"] = "mp3"
    srv = Server(cfg, port=0, process_inline=True)
    srv.start_thread()
    try:
        token = pairing.get_token()
        s = "2026-03-03T11-00-00-000Z_mp3"
        call(srv, "POST", "/start", {"session": s}, token=token)
        data = base64.b64encode(stereo_webm.read_bytes()).decode()
        call(srv, "POST", "/chunk", {"session": s, "seq": 0, "data": data}, token=token)
        call(srv, "POST", "/end", {"session": s}, token=token)
        m = MeetingStore(config_mod.meetings_dir(cfg)).get(s)
        assert m.audio_path.name == "audio.mp3"
        left, right = audio.channel(m.audio_path, 0), audio.channel(m.audio_path, 1)
        assert left.std() > 0.03 and right.std() > 0.03
    finally:
        srv.stop()


def test_discard_deletes_audio_and_ignores_late_chunks(server, cfg):
    token = pairing.get_token()
    s = "2026-03-03T12-00-00-000Z_disc"
    call(server, "POST", "/start", {"session": s}, token=token)
    call(server, "POST", "/chunk", {"session": s, "seq": 0, "data": base64.b64encode(b"x").decode()}, token=token)
    call(server, "POST", "/discard", {"session": s}, token=token)
    m = MeetingStore(config_mod.meetings_dir(cfg)).get(s)
    assert m.status == "discarded" and not m.chunks_dir.exists()
    assert call(server, "POST", "/chunk", {"session": s, "seq": 1, "data": base64.b64encode(b"y").decode()},
                token=token)[1].get("ignored") is True
    assert not m.chunks_dir.exists()


def test_a_meeting_without_audio_is_marked_error(server, cfg):
    token = pairing.get_token()
    s = "2026-03-03T13-00-00-000Z_empty"
    call(server, "POST", "/start", {"session": s}, token=token)
    call(server, "POST", "/end", {"session": s}, token=token)
    m = MeetingStore(config_mod.meetings_dir(cfg)).get(s)
    assert m.status == "error" and "no audio" in m.read()["error"]


def test_restart_recovers_recordings_and_queue(cfg):
    store = MeetingStore(config_mod.meetings_dir(cfg))
    store.get("a-recording").update(status="recording")
    store.get("b-queued").update(status="queued")
    srv = Server(cfg, port=0)
    srv.app.recover()
    assert "a-recording" in srv.app.last_audio
    assert srv.app.queue.get_nowait() == "b-queued"
    srv.httpd.server_close()


def raw(srv, head: str, body: bytes = b"") -> int:
    """Send a hand-written request; returns the status code."""
    with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
        s.sendall(head.replace("\n", "\r\n").encode() + b"\r\n" + body)
        return int(s.recv(4096).split(b" ", 2)[1])


def test_lengths_that_are_negative_missing_with_a_body_or_too_big_are_refused(server):
    token = pairing.get_token()
    host = f"Host: 127.0.0.1:{server.port}\n{pairing.TOKEN_HEADER}: {token}\n"
    assert raw(server, f"POST /ping HTTP/1.1\n{host}Content-Length: -1\n", b"{}") == 400
    assert raw(server, f"POST /ping HTTP/1.1\n{host}Transfer-Encoding: chunked\n", b"2\r\n{}\r\n0\r\n\r\n") == 411
    assert raw(server, f"POST /ping HTTP/1.1\n{host}Content-Length: 2\n", b"{}") == 200
    big = json.dumps({"code": "1" * 2000}).encode()
    pair = f"POST /pair HTTP/1.1\nHost: 127.0.0.1:{server.port}\nOrigin: {EXT}\nContent-Length: {len(big)}\n"
    assert raw(server, pair, big) == 413


def test_a_client_that_never_sends_its_request_is_dropped(cfg, monkeypatch):
    monkeypatch.setattr(server_mod, "REQUEST_TIMEOUT", 1)
    srv = Server(cfg, port=0)
    srv.start_thread()
    try:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
            s.sendall(b"POST /ping HTTP/1.1\r\n")  # and nothing else
            started = time.monotonic()
            assert s.recv(4096) == b"" and time.monotonic() - started < 4
    finally:
        srv.stop()


def test_start_keeps_only_a_real_start_time_and_short_text_fields(server, cfg):
    token = pairing.get_token()
    s = "2026-03-03T14-00-00-000Z_fields"
    call(server, "POST", "/start", {"session": s, "startedAt": "../../tmp/x", "title": ["x"] * 500,
                                    "code": 12345, "url": "u" * 1000}, token=token)
    d = MeetingStore(config_mod.meetings_dir(cfg)).get(s).read()
    assert d["started_at"].startswith("20") and "/" not in d["started_at"]
    assert isinstance(d["title"], str) and len(d["title"]) == 200
    assert d["code"] == "12345" and len(d["url"]) == 200


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_config_data_and_meeting_folders_are_private(server, cfg):
    token = pairing.get_token()
    s = "2026-03-03T15-00-00-000Z_private"
    call(server, "POST", "/start", {"session": s}, token=token)
    meetings = config_mod.meetings_dir(cfg)

    def mode(p):
        return stat.S_IMODE(p.stat().st_mode)

    for folder in (paths.config_dir(), config_mod.data_dir(cfg), meetings, meetings / s):
        assert mode(folder) == 0o700, folder
    assert mode(paths.config_file()) == 0o600


def test_recording_mode_is_read_and_set_through_the_server(server):
    token = pairing.get_token()
    assert call(server, "GET", "/status", token=token)[1]["recording_mode"] == "auto"
    assert call(server, "POST", "/mode", {"mode": "ask"})[0] == 401  # needs the token
    code, body = call(server, "POST", "/mode", {"mode": "ask"}, token=token)
    assert code == 200 and body["recording_mode"] == "ask"
    assert config_mod.load()["recording"]["mode"] == "ask"
    assert call(server, "POST", "/mode", {"mode": "always"}, token=token)[0] == 400
    # A change made with the CLI (config.toml) is what the extension reads next.
    cfg = config_mod.load()
    cfg["recording"]["mode"] = "manual"
    config_mod.save(cfg)
    assert call(server, "GET", "/status", token=token)[1]["recording_mode"] == "manual"


def test_meetings_are_listed_for_the_extension(server):
    token = pairing.get_token()
    m = MeetingStore(config_mod.meetings_dir(server.app.cfg)).get("2026-05-05T10-00-00-000Z_abc-defg-hij")
    m.update(status="ready", title="Plan", code="abc-defg-hij", started_at="2026-05-05T10:00:00Z")
    assert call(server, "GET", "/meetings")[0] == 401
    code, body = call(server, "GET", "/meetings", token=token)
    assert code == 200 and [x["title"] for x in body["meetings"]] == ["Plan"]
    assert body["meetings"][0]["transcript"] is False
