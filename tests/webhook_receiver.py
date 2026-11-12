"""A real HTTP(S) webhook receiver for tests. It checks every request with the verification snippet
published in docs/destinations.md, so the documentation is tested too."""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "destinations.md"


def doc_snippet(lang: str) -> str:
    """The code block of `lang` in docs/destinations.md that defines `verify`."""
    for m in re.finditer(r"```" + lang + r"\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S):
        if "verify(" in m.group(1) and ("def verify" in m.group(1) or "function verify" in m.group(1)):
            return m.group(1)
    raise LookupError(f"no {lang} verify snippet in {DOC}")


def python_verify():
    ns: dict = {}
    exec(compile(doc_snippet("python"), str(DOC), "exec"), ns)  # noqa: S102 - our own documentation
    return ns["verify"]


def self_signed_cert(folder: Path) -> tuple[Path, Path]:
    """A throwaway certificate for 127.0.0.1 and localhost (needs the openssl command)."""
    if not shutil.which("openssl"):
        import pytest

        pytest.skip("openssl not installed")
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return cert, key


class Receiver:
    """Records what arrives. `responses` is a queue of (status, headers, body) to answer with first;
    afterwards every valid request gets 200 with {"id": "remote-<n>"}."""

    def __init__(self, secret: str, responses=None, delay: float = 0.0, tls: tuple[Path, Path] | None = None):
        self.secret = secret.encode()
        self.verify = python_verify()
        self.responses = list(responses or [])
        self.delay = delay
        self.requests: list[dict] = []
        self.seen_signatures: set[str] = set()
        self.processed: dict[str, str] = {}  # Idempotency-Key -> remote id
        recv = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                recv.handle(self)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.scheme = "http"
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*map(str, tls))
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
            self.scheme = "https"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Receiver:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def url(self, path: str = "/hook", host: str = "127.0.0.1") -> str:
        return f"{self.scheme}://{host}:{self.server.server_port}{path}"

    # --- request handling --------------------------------------------------------------
    def handle(self, h: BaseHTTPRequestHandler) -> None:
        body = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        sig = h.headers.get("X-Minuta-Signature") or ""
        req = {"path": h.path, "headers": dict(h.headers), "body": body, "event": h.headers.get("X-Minuta-Event"),
               "key": h.headers.get("Idempotency-Key"), "valid": bool(self.verify(self.secret, h.headers, body)),
               "replay": sig in self.seen_signatures}
        req.update(parse_body(h.headers.get("Content-Type") or "", body))
        self.requests.append(req)
        if self.delay:
            time.sleep(self.delay)
        if not req["valid"]:
            return self.reply(h, 401, {}, b"bad signature")
        if req["replay"]:
            return self.reply(h, 409, {}, b"replayed request")
        self.seen_signatures.add(sig)
        if self.responses:
            status, headers, out = self.responses.pop(0)
            return self.reply(h, status, headers, out)
        req["duplicate"] = req["key"] in self.processed
        rid = self.processed.setdefault(req["key"], f"remote-{len(self.processed) + 1}")
        self.reply(h, 200, {"Content-Type": "application/json"}, json.dumps({"id": rid}).encode())

    @staticmethod
    def reply(h, status: int, headers: dict, body: bytes) -> None:
        h.send_response(status)
        for k, v in headers.items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def valid(self) -> list[dict]:
        return [r for r in self.requests if r["valid"] and not r["replay"]]


def parse_body(ctype: str, body: bytes) -> dict:
    if ctype.startswith("application/json"):
        return {"payload": json.loads(body), "audio": None}
    if ctype.startswith("multipart/form-data"):
        msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
        out: dict = {"payload": None, "audio": None}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True)
            if name == "payload":
                out["payload"] = json.loads(data)
            elif name == "audio":
                out["audio"] = data
                out["audio_filename"] = part.get_filename()
        return out
    return {"payload": None, "audio": None}


def replay(url: str, req: dict, verify=True) -> int:
    """Send a recorded request again, byte for byte."""
    import requests

    headers = {k: v for k, v in req["headers"].items() if k.lower() not in ("host", "content-length")}
    return requests.post(url, data=req["body"], headers=headers, timeout=5, verify=verify).status_code
