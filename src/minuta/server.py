"""Local server: receives audio from the extension and finishes each meeting after the call.

Listens only on 127.0.0.1. See `pairing.py` for how requests are authenticated.

Endpoints (POST bodies are JSON):

    GET  /health    no token. {"ok", "app", "version", "paired"}
    POST /pair      no token. {"code"} -> {"token"}; extension origins only
    GET  /status    server state, last extension heartbeat, recordings in progress
    GET  /latest    the latest finished meeting (for the popup)
    POST /ping      the extension is alive (sent even without a Meet tab)
    POST /heartbeat state of a Meet tab every 5 s; adds to the meeting's diagnostics
    POST /start /chunk /title /mute /pause /end /discard   one recording
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import queue
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from minuta import __version__, audio, credentials, pairing, paths
from minuta import config as config_mod
from minuta.meetings import (
    DISCARDED,
    ERROR,
    PROCESSING,
    QUEUED,
    READY,
    RECORDING,
    Meeting,
    MeetingStore,
    now_iso,
    parse_time,
    valid_id,
)
from minuta.redact import redact
from minuta.router import Router

log = logging.getLogger(__name__)
MAX_BODY = 20 * 1024 * 1024
MAX_PAIR_BODY = 1024
REQUEST_TIMEOUT = 15  # seconds a client may take to send its request
SEEN_WRITE_EVERY = 5.0
CLOSE_AFTER_SILENCE = 90  # seconds without audio before a recording is closed (a tab that died)
TEXT_MAX = 200


def _mtime(path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _text(value) -> str | None:
    """A text field from the extension: a string of at most TEXT_MAX characters, or None."""
    return None if value is None else str(value)[:TEXT_MAX]


class App:
    """Everything the HTTP handler and the background threads share."""

    def __init__(self, cfg: dict, process_inline: bool = False):
        self.cfg = cfg
        self.store = MeetingStore(config_mod.meetings_dir(cfg))
        self.secrets = credentials.store(cfg)
        self.router = Router(cfg, self.secrets)
        self.queue: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.last_audio: dict[str, float] = {}
        self.heartbeat: dict | None = None
        self.last_heartbeat: str | None = None
        self.last_seen: float | None = None
        self._seen_written = 0.0
        self.process_inline = process_inline
        self.stopping = threading.Event()
        self._cfg_mtime = _mtime(paths.config_file())

    def reload_config(self) -> None:
        """Pick up changes to config.toml (`minuta destinations add`, an edit by hand) without a restart.
        The port and the data folder still need one."""
        mtime = _mtime(paths.config_file())
        if mtime is None or mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            cfg = config_mod.load()
        except Exception as e:  # noqa: BLE001 - a half-edited file keeps the previous config
            log.warning("config.toml not reloaded: %s", e)
            return
        self.cfg, self.router = cfg, Router(cfg, self.secrets)
        log.info("config.toml reloaded")

    # --- extension liveness ------------------------------------------------------
    def seen(self, heartbeat: dict | None = None) -> None:
        self.last_seen = time.time()
        if heartbeat is not None:
            self.heartbeat = heartbeat
            self.last_heartbeat = now_iso()
        if self.last_seen - self._seen_written >= SEEN_WRITE_EVERY:
            self._seen_written = self.last_seen
            pairing.update_state(last_seen=self.last_seen)

    # --- meetings ----------------------------------------------------------------
    def close(self, meeting_id: str, reason: str) -> None:
        with self.lock:
            if meeting_id not in self.last_audio:
                return
            self.last_audio.pop(meeting_id, None)
        self.store.get(meeting_id).update(status=QUEUED, ended_at=now_iso(), end_reason=reason)
        log.info("meeting %s closed (%s)", meeting_id, reason)
        if self.process_inline:
            self.process(meeting_id)
        else:
            self.queue.put(meeting_id)

    def process(self, meeting_id: str) -> None:
        self.reload_config()
        m = self.store.get(meeting_id)
        if m.status == DISCARDED:
            return
        try:
            m.update(status=PROCESSING)
            fmt = self.cfg["general"].get("audio_format", "opus")
            if m.audio_path is None:
                path = audio.build_audio(m.dir, fmt)
            else:
                path = m.audio_path
            data = m.read()
            check = audio.check_recording(path, data.get("diagnostics") or {})
            m.update(audio={"file": path.name, "format": path.suffix.lstrip("."), "bytes": path.stat().st_size,
                            "deleted": False},
                     duration_seconds=audio.duration_seconds(path), audio_check=check, status=READY, error=None)
            if check.get("warning"):
                log.warning("meeting %s: %s", meeting_id, check["warning"])
            self.router.deliver_meeting(m)
            if self.cfg["transcription"].get("mode") == "auto":
                self.auto_transcribe(m)
            self.router.apply_delete_policy(m)
        except Exception as e:  # noqa: BLE001 - one bad meeting must not stop the worker
            log.exception("could not process %s", meeting_id)
            m.update(status=ERROR, error=redact(str(e))[:500])

    def auto_transcribe(self, m: Meeting) -> None:
        from minuta.transcription.budget import Budget
        from minuta.transcription.pipeline import transcribe

        try:
            budget = Budget(float(self.cfg["transcription"].get("max_usd") or 0))
            transcribe(m, self.cfg, router=self.router, secrets=self.secrets, budget=budget)
        except Exception as e:  # noqa: BLE001
            log.warning("automatic transcription of %s failed: %s", m.id, redact(str(e)))

    def worker(self) -> None:
        while not self.stopping.is_set():
            try:
                meeting_id = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.process(meeting_id)

    def watchdog(self) -> None:
        while not self.stopping.wait(5):
            with self.lock:
                stale = [s for s, t in self.last_audio.items() if time.time() - t > CLOSE_AFTER_SILENCE]
            for s in stale:
                self.close(s, "no audio")

    def recover(self) -> None:
        """At startup: recordings in progress are watched again, queued ones go back to the queue."""
        for m in self.store.all():
            st = m.status
            if st == RECORDING:
                with self.lock:
                    self.last_audio[m.id] = time.time()
            elif st in (QUEUED, PROCESSING):
                m.update(status=QUEUED)
                self.queue.put(m.id)

    def latest(self) -> dict | None:
        for m in reversed(self.store.all()):
            d = m.read()
            if d.get("status") in (None, DISCARDED, RECORDING):
                continue
            return {"id": m.id, "title": m.title, "status": d.get("status"), "started_at": d.get("started_at"),
                    "duration_seconds": d.get("duration_seconds"),
                    "warning": (d.get("audio_check") or {}).get("warning"),
                    "transcribed": m.has_transcript()}
        return None

    def status(self) -> dict:
        with self.lock:
            recording = list(self.last_audio)
        return {"ok": True, "version": __version__, "recording": recording, "queued": self.queue.qsize(),
                "last_heartbeat": self.last_heartbeat, "last_seen": self.last_seen, "heartbeat": self.heartbeat,
                "paired_origins": pairing.read_state().get("allowed_origins", []), "latest": self.latest()}

    # --- messages from the extension ----------------------------------------------
    def handle(self, kind: str, msg: dict) -> tuple[int, dict]:
        if kind == "ping":
            self.seen()
            return 200, {"ok": True}
        if kind == "heartbeat":
            hb = {k: v for k, v in msg.items() if k not in ("src", "type")}
            self.seen(hb)
            s = msg.get("session")
            if valid_id(s) and msg.get("recording") and self.store.get(s).status not in (DISCARDED, None):
                self._diagnose(self.store.get(s), msg)
            return 200, {"ok": True}

        meeting_id = msg.get("session")
        if not valid_id(meeting_id):
            return 400, {"error": "invalid session"}
        m = self.store.get(meeting_id)
        self.seen()

        if kind == "start":
            with self.lock:
                self.last_audio[meeting_id] = time.time()
            started = msg.get("startedAt")
            m.update(status=RECORDING, code=_text(msg.get("code")), title=_text(msg.get("title")),
                     url=_text(msg.get("url")), started_at=started if parse_time(started) else now_iso())
            m.mkdir()
            m.chunks_dir.mkdir(exist_ok=True)
            log.info("recording %s", meeting_id)
        elif kind == "chunk":
            if m.status == DISCARDED:
                return 200, {"ok": True, "ignored": True}
            try:
                seq = int(msg.get("seq", 0))
                data = base64.b64decode(msg.get("data") or "", validate=True)
            except (TypeError, ValueError, binascii.Error):
                return 400, {"error": "invalid chunk"}
            if seq < 0 or seq > 10**6:
                return 400, {"error": "invalid chunk"}
            m.mkdir()
            m.chunks_dir.mkdir(exist_ok=True)
            (m.chunks_dir / f"{seq:06d}.webm").write_bytes(data)
            with self.lock:
                if meeting_id not in self.last_audio and m.status in (None, RECORDING):
                    # The server restarted mid-call: pick the recording up again.
                    m.update(status=RECORDING, started_at=m.read().get("started_at") or now_iso())
                if m.status == RECORDING:
                    self.last_audio[meeting_id] = time.time()
        elif kind == "title":
            if msg.get("title") and m.exists():
                m.update(title=_text(msg["title"]))
        elif kind in ("mute", "pause"):
            if m.exists():
                ev = {"type": kind, "t": msg.get("t")}
                ev["muted" if kind == "mute" else "paused"] = msg.get("muted" if kind == "mute" else "paused")
                m.append_event(ev)
        elif kind == "discard":
            with self.lock:
                self.last_audio.pop(meeting_id, None)
            m.update(status=DISCARDED, ended_at=now_iso(), end_reason="discarded by the user")
            shutil.rmtree(m.chunks_dir, ignore_errors=True)
            for name in ("recording.webm", "audio.opus", "audio.mp3", "audio.webm"):
                (m.dir / name).unlink(missing_ok=True)
            log.info("meeting %s discarded", meeting_id)
        elif kind == "end":
            self.close(meeting_id, "call ended")
        else:
            return 404, {"error": "unknown message"}
        return 200, {"ok": True}

    @staticmethod
    def _diagnose(m: Meeting, msg: dict) -> None:
        d = m.read().get("diagnostics") or {}

        def bump(key: str) -> None:
            d[key] = d.get(key, 0) + 1

        d["max_remote_tracks"] = max(d.get("max_remote_tracks", 0), msg.get("remoteTracks") or 0)
        d["max_remote_outputs"] = max(d.get("max_remote_outputs", 0), msg.get("remoteSources") or 0)
        d["max_participants"] = max(d.get("max_participants", 0), msg.get("participantCount") or 0)
        d["remote_signal_seen"] = d.get("remote_signal_seen", False) or bool(msg.get("remoteSignal"))
        d["remote_peak"] = max(d.get("remote_peak", 0), msg.get("remoteLevel") or 0)
        if msg.get("inboundKbps") is not None:
            d["max_inbound_kbps"] = max(d.get("max_inbound_kbps", 0), msg["inboundKbps"])
        if msg.get("remoteBroken"):
            bump("heartbeats_capture_broken")
        if msg.get("paused"):
            bump("heartbeats_paused")
        if msg.get("sinkless") is not None:
            d["recording_context_silent"] = bool(msg["sinkless"])
        if msg.get("remoteUnverified"):
            bump("heartbeats_without_remote_signal")
        if msg.get("captureError"):
            d["capture_error"] = str(msg["captureError"])[:300]
        if not msg.get("localTrack"):
            bump("heartbeats_without_microphone")
        if msg.get("muted") is None:
            bump("heartbeats_without_mute_button")
        if msg.get("pip"):
            bump("heartbeats_in_picture_in_picture")
        if msg.get("inCall") is False:
            bump("heartbeats_call_not_visible")
        if msg.get("ctxState") not in (None, "running"):
            bump("heartbeats_audio_suspended")
        m.update(diagnostics=d)


def make_handler(app: App, port_ref: list[int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "minuta"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), redact(fmt % args))

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _gate(self, needs_token: bool) -> bool:
            """Host, origin and token checks. Sends the error itself and returns False when refused."""
            host = (self.headers.get("Host") or "").lower()
            allowed_hosts = {f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}"}
            if host not in allowed_hosts:  # DNS rebinding
                self._json(403, {"error": "forbidden host"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "null":
                if not pairing.is_extension_origin(origin):
                    self._json(403, {"error": "forbidden origin"})
                    return False
                if needs_token and origin not in pairing.read_state().get("allowed_origins", []):
                    self._json(403, {"error": "extension not paired"})
                    return False
            if needs_token and not pairing.check_token(self.headers.get(pairing.TOKEN_HEADER)):
                self._json(401, {"error": "missing or wrong token"})
                return False
            return True

        def do_OPTIONS(self):  # noqa: N802 - no CORS for anyone: extensions do not need it
            self._json(403, {"error": "forbidden"})

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                if not self._gate(needs_token=False):
                    return
                return self._json(200, {"ok": True, "app": "minuta", "version": __version__,
                                        "paired": bool(pairing.read_state().get("allowed_origins"))})
            if path not in ("/status", "/latest"):
                return self._json(404, {"error": "not found"})
            if not self._gate(needs_token=True):
                return
            if path == "/status":
                return self._json(200, app.status())
            return self._json(200, {"ok": True, "meeting": app.latest()})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0].strip("/")
            raw = self.headers.get("Content-Length")
            if raw is None:
                if self.headers.get("Transfer-Encoding"):  # a body without a length: not read
                    return self._json(411, {"error": "length required"})
                raw = "0"
            try:
                length = int(raw)
            except ValueError:
                return self._json(400, {"error": "bad length"})
            if length < 0:
                return self._json(400, {"error": "bad length"})
            if length > (MAX_PAIR_BODY if path == "pair" else MAX_BODY):
                return self._json(413, {"error": "too large"})
            if path == "pair":
                if not self._gate(needs_token=False):
                    return
                origin = self.headers.get("Origin") or ""
                if not pairing.is_extension_origin(origin):
                    return self._json(403, {"error": "pairing is only for the browser extension"})
                msg = self._read(length)
                if msg is None:
                    return
                if not pairing.redeem(str(msg.get("code") or "")):
                    return self._json(401, {"error": "wrong or expired code"})
                pairing.allow_origin(origin)
                app.seen()
                log.info("extension paired: %s", origin)
                return self._json(200, {"ok": True, "token": pairing.get_token(create=True)})
            if not self._gate(needs_token=True):
                return
            msg = self._read(length)
            if msg is None:
                return
            code, body = app.handle(path, msg)
            self._json(code, body)

        def _read(self, length: int) -> dict | None:
            try:
                msg = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"error": "invalid json"})
                return None
            if not isinstance(msg, dict):
                self._json(400, {"error": "invalid json"})
                return None
            return msg

    return Handler


class Server:
    def __init__(self, cfg: dict, port: int | None = None, process_inline: bool = False):
        self.app = App(cfg, process_inline=process_inline)
        self.port_ref = [port if port is not None else int(cfg["server"]["port"])]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port_ref[0]), make_handler(self.app, self.port_ref))
        self.httpd.daemon_threads = True
        self.port_ref[0] = self.httpd.server_address[1]
        self.threads: list[threading.Thread] = []

    @property
    def port(self) -> int:
        return self.port_ref[0]

    def start_background(self) -> None:
        pairing.get_token(create=True)
        config_mod.make_data_dirs(self.app.cfg)
        self.app.recover()
        for target in (self.app.worker, self.app.watchdog):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def serve_forever(self) -> None:
        self.start_background()
        log.info("minuta %s listening on 127.0.0.1:%s", __version__, self.port)
        try:
            self.httpd.serve_forever()
        finally:
            self.app.stopping.set()

    def start_thread(self) -> threading.Thread:
        self.start_background()
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self.app.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
