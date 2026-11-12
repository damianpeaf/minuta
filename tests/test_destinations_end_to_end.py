"""The router with the real built-in destinations at once: local, webhook, command, obsidian.
One failing destination must not stop the others, and no secret may end up in meeting.json."""

from __future__ import annotations

import json
import logging
import sys

from minuta import credentials
from minuta.meetings import MeetingStore
from minuta.router import Router

from .webhook_receiver import Receiver

KEY = "e2e-signing-key-0123456789abcdef"
HEADER_TOKEN = "e2e_header_token_0123456789"

RECORDER = r'''
import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "event": os.environ["MINUTA_EVENT"],
                        "has_transcript": os.path.exists(os.path.join(os.environ["MINUTA_MEETING_DIR"],
                                                                      "transcript.json"))}) + "\n")
'''


def test_all_destinations_together_with_one_failing(cfg, tmp_path, caplog):
    store = credentials.store(cfg)
    store.set("HOOK_SIGNING_KEY", KEY)
    store.set("HOOK_HEADERS", f"Authorization: Bearer {HEADER_TOKEN}")
    store.set("DOWN_SIGNING_KEY", KEY)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "rec.py").write_text(RECORDER, encoding="utf-8")
    vault = tmp_path / "Vault"
    vault.mkdir()

    m = MeetingStore(tmp_path / "meetings").get("2026-02-02T09-00-00-000Z_xyz-abcd-efg")
    m.update(status="ready", title="Weekly", started_at="2026-02-02T09:00:00+00:00", duration_seconds=60)
    (m.dir / "audio.opus").write_bytes(b"opus" * 1000)

    with Receiver(KEY) as recv, Receiver(KEY, responses=[(500, {}, b"down")] * 50) as down:
        dests = {
            "local": {"type": "local", "copy_to": str(tmp_path / "copy")},
            "hook": {"type": "webhook", "url": recv.url(), "signing_key_secret": "HOOK_SIGNING_KEY",
                     "headers_secret": "HOOK_HEADERS", "send": ["audio", "metadata", "transcript"]},
            "down": {"type": "webhook", "url": down.url(), "signing_key_secret": "DOWN_SIGNING_KEY"},
            "broken": {"type": "command", "command": [sys.executable, "-c", "import sys; sys.exit(9)"]},
            "script": {"type": "command", "command": [sys.executable, str(bindir / "rec.py")]},
            "notes": {"type": "obsidian", "vault": str(vault)},
        }
        router = Router({**cfg, "destinations": dests}, store)
        with caplog.at_level(logging.INFO):
            res = router.deliver_meeting(m)
            assert {k: v["status"] for k, v in res.items()} == {
                "local": "ok", "hook": "ok", "down": "failed", "broken": "failed", "script": "ok", "notes": "ok"}
            assert res["down"]["permanent"] is False and "500" in res["down"]["error"]
            assert "exited with 9" in res["broken"]["error"]

            # The transcript arrives later.
            doc = {"title": "Weekly", "turns": [{"start": 0, "end": 1, "speaker": "Me", "text": "listo"}]}
            (m.dir / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
            (m.dir / "transcript.md").write_text("# Weekly\n", encoding="utf-8")
            tres = router.deliver_transcript(m)
            assert {k: v["status"] for k, v in tres.items()} == {
                "local": "ok", "hook": "ok", "down": "failed", "broken": "failed", "script": "ok", "notes": "ok"}

            # A retry sends only what failed; what was delivered is not sent again.
            before = len(recv.requests)
            router.retry(m)
            assert len(recv.requests) == before

    events = [(r["event"], r["key"]) for r in recv.valid()]
    assert events == [("meeting.ready", f"{m.id}.meeting.ready"), ("transcript.ready", f"{m.id}.transcript.ready")]
    assert recv.requests[0]["audio"] == b"opus" * 1000
    assert recv.requests[1]["payload"]["transcript"]["turns"][0]["text"] == "listo"
    script_calls = [json.loads(x) for x in (bindir / "calls.jsonl").read_text().splitlines()]
    assert [(c["event"], c["has_transcript"]) for c in script_calls] == [("meeting.ready", False),
                                                                         ("transcript.ready", True)]
    assert "listo" in next((vault / "Meetings").iterdir()).read_text(encoding="utf-8")
    assert any((tmp_path / "copy").iterdir())

    state = m.meta_path.read_text(encoding="utf-8")
    for secret in (KEY, HEADER_TOKEN):
        assert secret not in state and secret not in caplog.text
    assert json.loads(state)["destinations"]["hook"]["meeting"]["receipt"]["remote_id"] == "remote-1"
