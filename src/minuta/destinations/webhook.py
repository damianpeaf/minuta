"""The built-in `webhook` destination: POST each meeting and transcript to a URL, signed.

    [destinations.hook]
    type = "webhook"
    url = "https://example.com/minuta"
    signing_key_secret = "HOOK_SIGNING_KEY"   # set by `minuta destinations add`, stored in the keyring
    headers_secret = "HOOK_HEADERS"           # optional extra headers, also a secret
    send = ["metadata", "transcript"]         # add "audio" to attach the recording (multipart)
    max_audio_mb = 100
    timeout_seconds = 30

Every request carries `X-Minuta-Event`, `X-Minuta-Timestamp`, `X-Minuta-Signature` (HMAC-SHA256 of
`<timestamp>.<raw body>`) and an `Idempotency-Key` that is the same for every attempt of one meeting
and event. docs/destinations.md shows how to verify the signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets as pysecrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from minuta import __version__
from minuta.destinations.base import (
    MEETING_READY,
    PARTS,
    Capabilities,
    CheckResult,
    Destination,
    DestinationError,
    MeetingParts,
    PermanentError,
    Receipt,
    file_sha256,
    retry_after_seconds,
)
from minuta.redact import register_secret

log = logging.getLogger(__name__)

PING = "ping"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
RESERVED_HEADERS = {"content-type", "content-length", "host", "idempotency-key", "user-agent",
                    "transfer-encoding", "x-minuta-event", "x-minuta-timestamp", "x-minuta-signature"}
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
AUDIO_TYPES = {".opus": "audio/ogg", ".mp3": "audio/mpeg", ".webm": "audio/webm"}
MIN_KEY_LENGTH = 16
MAX_REDIRECTS = 3
CHUNK = 1 << 16


def url_problem(url: str) -> str | None:
    """Why this URL is not allowed, or None. https only; plain http only to this computer."""
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        u.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return "url is not valid"
    if not host:
        return "url needs a host"
    if u.username or u.password:
        return "url must not contain a user or password; use the headers option"
    if u.scheme == "https":
        return None
    if u.scheme == "http":
        if host in LOOPBACK_HOSTS:
            return None
        return "plain http is only allowed to 127.0.0.1 or localhost; use https"
    return "url must start with https://"


def sign(key: str | bytes, timestamp: str, body_chunks) -> str:
    """`sha256=` + hex HMAC-SHA256 of `<timestamp>.<body>`. `body_chunks` is bytes or an iterable of bytes."""
    mac = hmac.new(key.encode() if isinstance(key, str) else key, timestamp.encode() + b".", hashlib.sha256)
    for chunk in ([body_chunks] if isinstance(body_chunks, bytes) else body_chunks):
        mac.update(chunk)
    return "sha256=" + mac.hexdigest()


def parse_headers(raw: str | None) -> dict[str, str]:
    """`{"Name": "value"}` as JSON, or `Name: value` lines."""
    if not raw or not raw.strip():
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError("headers is not valid JSON") from e
        if not isinstance(data, dict):
            raise ValueError("headers must be a JSON object")
        pairs = [(str(k), str(v)) for k, v in data.items()]
    else:
        pairs = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            if ":" not in line:
                raise ValueError("each header must look like 'Name: value'")
            k, v = line.split(":", 1)
            pairs.append((k.strip(), v.strip()))
    out = {}
    for k, v in pairs:
        if not HEADER_NAME_RE.match(k):
            raise ValueError(f"invalid header name {k!r}")
        if k.lower() in RESERVED_HEADERS:
            raise ValueError(f"header {k} is set by minuta")
        if "\r" in v or "\n" in v:
            raise ValueError(f"header {k} contains a line break")
        out[k] = v
    return out


class MultipartBody:
    """multipart/form-data with a JSON `payload` part and an `audio` file part, streamed from disk.

    It has `read()` and `len()`, so requests sends it with a Content-Length and never loads the
    audio into memory. One instance per attempt: it is consumed as it is sent."""

    def __init__(self, payload: bytes, audio: Path, boundary: str | None = None):
        self.boundary = boundary or "minuta-" + pysecrets.token_hex(16)
        b = self.boundary
        ctype = AUDIO_TYPES.get(audio.suffix.lower(), "application/octet-stream")
        self.head = (f"--{b}\r\nContent-Disposition: form-data; name=\"payload\"\r\n"
                     f"Content-Type: application/json\r\n\r\n").encode() + payload + (
            f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"{audio.name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode()
        self.tail = f"\r\n--{b}--\r\n".encode()
        self.audio = audio
        self.size = len(self.head) + audio.stat().st_size + len(self.tail)
        self._gen: Iterator[bytes] | None = None
        self._buf = b""

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def chunks(self) -> Iterator[bytes]:
        yield self.head
        with open(self.audio, "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk
        yield self.tail

    def __len__(self) -> int:
        return self.size

    def __iter__(self):
        return self.chunks()

    def read(self, n: int = -1) -> bytes:
        if self._gen is None:
            self._gen = self.chunks()
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._gen)
            except StopIteration:
                break
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


class WebhookDestination(Destination):
    """POST meetings and transcripts to a URL as signed JSON (audio optional)."""

    kind = "webhook"
    capabilities = Capabilities(audio=True, transcript=True, metadata=True)
    secret_options = ("signing_key", "headers")
    default_send = ("metadata", "transcript")  # audio only when asked for

    def __init__(self, name: str, options: dict, secrets=None, clock: Callable[[], float] = time.time):
        super().__init__(name, options, secrets)
        self.clock = clock

    # --- options ---------------------------------------------------------------
    @property
    def url(self) -> str:
        return str(self.options.get("url") or "").strip()

    @property
    def timeout(self) -> float:
        return self.number("timeout_seconds", 30, 1, 600)

    @property
    def max_audio_bytes(self) -> int:
        return int(self.number("max_audio_mb", 100, 0, 2048) * 1024 * 1024)

    def _extra_headers(self) -> dict[str, str]:
        headers = parse_headers(self.secret("headers"))
        for v in headers.values():
            register_secret(v)
            register_secret(v.split(" ", 1)[-1])  # the token alone, without "Bearer "
        return headers

    @classmethod
    def configure(cls, name: str, options: dict, prompter=None, secrets=None) -> dict:
        """In a terminal, asks for the URL, the signing key, an optional header and whether to attach audio."""
        from minuta.i18n import t

        opts = dict(options)
        if prompter is None:
            return opts
        if not opts.get("url"):
            opts["url"] = prompter.text(t("setup.webhook.url"))
        if not opts.get("signing_key") and not opts.get("signing_key_secret"):
            opts["signing_key"] = prompter.password(t("setup.webhook.key"))
        if not opts.get("headers") and not opts.get("headers_secret"):
            opts["headers"] = prompter.password(t("setup.webhook.headers"))
        if "audio" not in opts.get("send", ()) and prompter.confirm(t("setup.webhook.audio"), False):
            opts["send"] = list(PARTS)
        return opts

    def check(self) -> CheckResult:
        problem = url_problem(self.url)
        if problem:
            return CheckResult(False, problem)
        try:
            _ = self.timeout, self.max_audio_bytes
            self._extra_headers()
        except ValueError as e:
            return CheckResult(False, str(e))
        key = self.secret("signing_key")
        if not key:
            return CheckResult(False, "signing_key is missing: add it with --option signing_key=<random text>")
        if len(key) < MIN_KEY_LENGTH:
            return CheckResult(False, f"signing_key must be at least {MIN_KEY_LENGTH} characters")
        u = urlsplit(self.url)
        return CheckResult(True, f"posts to {u.scheme}://{u.netloc}, signed")

    # --- payloads --------------------------------------------------------------
    def _meeting_info(self, meeting, metadata: dict | None) -> dict:
        if metadata is not None:
            return metadata
        if "metadata" in (self.options.get("send") or ["metadata"]):
            return meeting.metadata()
        return {"id": meeting.id}

    @staticmethod
    def _transcript(files: dict[str, Path] | None) -> dict:
        out: dict = {}
        if not files:
            return out
        if files.get("json"):
            out["transcript"] = json.loads(Path(files["json"]).read_text(encoding="utf-8"))
        if files.get("md"):
            out["transcript_markdown"] = Path(files["md"]).read_text(encoding="utf-8")
        return out

    @staticmethod
    def idempotency_key(meeting_id: str, event: str) -> str:
        return f"{meeting_id}.{event}"

    def _payload(self, event: str, meeting, key: str, previous: Receipt | None) -> dict:
        p = {"event": event, "delivery_id": key, "destination": self.name, "minuta_version": __version__}
        if meeting is not None:
            p["meeting_id"] = meeting.id
        if previous and previous.remote_id:
            p["previous_remote_id"] = previous.remote_id
        return p

    # --- sending ---------------------------------------------------------------
    def send_meeting(self, meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        event = parts.event
        key = self.idempotency_key(meeting.id, event)
        payload = self._payload(event, meeting, key, previous)
        payload["meeting"] = self._meeting_info(meeting, parts.metadata)
        payload.update(self._transcript(parts.transcript))
        audio = parts.audio
        if audio is not None:
            size, limit = audio.stat().st_size, self.max_audio_bytes
            if size > limit:
                log.warning("%s: audio is %d MB, over max_audio_mb for %s; sending without it",
                            meeting.id, size // (1024 * 1024), self.name)
                payload["audio"] = {"included": False, "reason": "too_large", "bytes": size, "limit_bytes": limit}
                audio = None
            else:
                payload["audio"] = {"included": True, "file": audio.name, "bytes": size, "sha256": file_sha256(audio)}
        receipt = self._post(event, key, payload, audio)
        if event == MEETING_READY:
            receipt.extra["audio_sent"] = audio is not None
        return receipt

    def ping(self) -> Receipt:
        """A signed `ping` event, for `minuta destinations test NAME --live`."""
        ok = self.check()
        if not ok.ok:
            raise PermanentError(ok.message)
        key = f"ping.{pysecrets.token_hex(8)}"
        return self._post(PING, key, self._payload(PING, None, key, None), None)

    def _post(self, event: str, key: str, payload: dict, audio: Path | None) -> Receipt:
        """One signed POST. Timeouts, connection errors, 408, 429 and 5xx are worth retrying (the
        router does it, honouring Retry-After); anything else needs the user to change something."""
        problem = url_problem(self.url)
        if problem:
            raise PermanentError(problem)
        signing_key = self.secret("signing_key")
        if not signing_key:
            raise PermanentError("signing_key is missing")
        extra = self._extra_headers()

        def build() -> tuple[dict, bytes | MultipartBody]:
            """A freshly signed request. Every request (retry or redirect) gets its own `request_id`,
            so no two carry the same signature and a receiver can treat a repeated one as a replay."""
            body_json = json.dumps({**payload, "request_id": pysecrets.token_hex(12)}, ensure_ascii=False).encode()
            ts = str(int(self.clock()))
            if audio is None:
                body: bytes | MultipartBody = body_json
                ctype, signature = "application/json", sign(signing_key, ts, body_json)
            else:
                body = MultipartBody(body_json, audio)
                ctype, signature = body.content_type, sign(signing_key, ts, body.chunks())
            headers = {**extra, "Content-Type": ctype, "User-Agent": f"minuta/{__version__}",
                       "X-Minuta-Event": event, "X-Minuta-Timestamp": ts, "X-Minuta-Signature": signature,
                       "Idempotency-Key": key}
            return headers, body

        host = urlsplit(self.url).netloc
        try:
            r, url = self._request(build)
        except requests.exceptions.SSLError as e:
            raise PermanentError(f"TLS verification failed for {host}") from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise DestinationError(f"{type(e).__name__}: could not reach {host}") from e
        code = r.status_code
        if 200 <= code < 300:
            return self._receipt(r, key)
        if code in (408, 429) or code >= 500:
            wait = retry_after_seconds(r.headers.get("Retry-After"))
            raise DestinationError(f"{code}: {_snippet(r)}", retry_after=wait)
        if 300 <= code < 400:
            raise PermanentError(f"{code}: redirect not followed ({_redirect_reason(r, url)})")
        raise PermanentError(f"{code}: {_snippet(r)}")

    def _request(self, build: Callable[[], tuple[dict, bytes | MultipartBody]]):
        """POST, following only 307/308 redirects that stay on the same scheme, host and port.
        Returns the last response and the URL that gave it."""
        url = self.url
        timeout = (min(10.0, self.timeout), self.timeout)
        for _hop in range(MAX_REDIRECTS + 1):
            headers, body = build()
            with requests.Session() as s:
                r = s.post(url, data=body, headers=headers, timeout=timeout, allow_redirects=False, verify=True)
            if r.status_code not in (307, 308) or not r.headers.get("Location"):
                return r, url
            target = urljoin(url, r.headers["Location"])
            if not _same_origin(url, target):
                return r, url  # reported as a permanent error by the caller
            url = target
        return r, url

    @staticmethod
    def _receipt(r: requests.Response, key: str) -> Receipt:
        remote_id, url = None, None
        if r.content and "json" in (r.headers.get("Content-Type") or ""):
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                remote_id = str(data["id"]) if data.get("id") is not None else None
                u = data.get("url")
                url = u if isinstance(u, str) and u.startswith(("https://", "http://")) else None
        return Receipt(remote_id=remote_id or key, url=url, audio=False, extra={"status": r.status_code})


def _same_origin(a: str, b: str) -> bool:
    ua, ub = urlsplit(a), urlsplit(b)

    def port(u):
        return u.port or (443 if u.scheme == "https" else 80)

    return ua.scheme == ub.scheme and (ua.hostname or "").lower() == (ub.hostname or "").lower() \
        and port(ua) == port(ub)


def _redirect_reason(r: requests.Response, url: str) -> str:
    loc = r.headers.get("Location")
    if not loc:
        return "no Location"
    target = urljoin(url, loc)
    if not _same_origin(url, target):
        return f"points to another host: {urlsplit(target).netloc or target[:60]}"
    if r.status_code in (307, 308):
        return "too many redirects"
    return "would turn the POST into a GET; update the url"


def _snippet(r: requests.Response) -> str:
    text = (r.text or "").strip().replace("\n", " ")
    return text[:200] or (r.reason or "")
