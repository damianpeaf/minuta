"""The notion destination against the real Notion API.

Skipped unless both variables are set; never commit their values:

    MINUTA_TEST_NOTION_TOKEN   an internal integration secret
    MINUTA_TEST_NOTION_PARENT  a page or database the integration can write to

Everything happens inside ONE container page created under PARENT, titled
"minuta adapter test (temporary, delete)", with a database created inside it by the adapter itself.
The container is moved to the trash at the end, also when a test fails.

    uv run pytest -m notion_live -s tests/test_notion_live.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil

import pytest

from minuta import audio as audio_mod
from minuta.destinations import DestinationError, MeetingParts
from minuta.destinations import notion as notion_mod
from minuta.destinations.notion import NotionAPI, NotionDestination, parse_id
from minuta.meetings import MeetingStore

TOKEN = os.environ.get("MINUTA_TEST_NOTION_TOKEN", "")
PARENT = os.environ.get("MINUTA_TEST_NOTION_PARENT", "")
CONTAINER_TITLE = "minuta adapter test (temporary, delete)"

pytestmark = [
    pytest.mark.notion_live,
    pytest.mark.skipif(not (TOKEN and PARENT), reason="MINUTA_TEST_NOTION_TOKEN / MINUTA_TEST_NOTION_PARENT not set"),
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed"),
]


class Secrets:
    def get(self, name):
        return TOKEN if name == "LIVE_TOKEN" else None


def _title(text):
    return {"title": [{"type": "text", "text": {"content": text}}]}


@pytest.fixture(scope="module")
def container():
    api = NotionAPI(TOKEN)
    parent = parse_id(PARENT)
    assert parent, "MINUTA_TEST_NOTION_PARENT is not a Notion id or link"
    try:
        db = api.request("GET", f"/databases/{parent}")
        title = next(n for n, p in db["properties"].items() if p["type"] == "title")
        body = {"parent": {"database_id": parent}, "properties": {title: _title(CONTAINER_TITLE)}}
    except DestinationError:
        body = {"parent": {"page_id": parent}, "properties": {"title": _title(CONTAINER_TITLE)}}
    page = api.request("POST", "/pages", body)
    print(f"\ncontainer page: {page['id']}")
    try:
        yield page["id"]
    finally:
        api.request("PATCH", f"/pages/{page['id']}", {"archived": True})
        again = api.request("GET", f"/pages/{page['id']}")
        print(f"container page {page['id']}: archived={again.get('archived')} in_trash={again.get('in_trash')}")
        assert again.get("archived") or again.get("in_trash")


@pytest.fixture(scope="module")
def database(container):
    opts = NotionDestination.configure("live", {"type": "notion", "token": TOKEN, "parent_page": container})
    return opts["database"]


def dest(database):
    return NotionDestination("live", {"type": "notion", "token_secret": "LIVE_TOKEN", "database": database}, Secrets())


def synthetic_ogg(path, seconds, bitrate="64k"):
    """Real Ogg/Opus: a tone on the left, another on the right."""
    audio_mod.ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                     "-f", "lavfi", "-i", f"sine=frequency=660:duration={seconds}",
                     "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]", "-map", "[a]",
                     "-c:a", "libopus", "-b:a", bitrate, "-vbr", "off", "-f", "ogg", str(path))
    return path


def make_meeting(tmp_path, mid, title, seconds=5, bitrate="64k"):
    m = MeetingStore(tmp_path / "meetings").get(mid)
    m.update(status="ready", title=title, started_at="2026-01-02T03:04:05Z", duration_seconds=float(seconds),
             url="https://meet.google.com/abc-defg-hij")
    synthetic_ogg(m.dir / "audio.opus", seconds, bitrate)
    return m


def metadata(m):
    d = m.read()
    return {k: d[k] for k in ("id", "title", "started_at", "duration_seconds", "url") if k in d}


def children(api, block_id):
    return api.children(block_id)


def test_check_on_the_created_database(database):
    r = dest(database).check()
    print(r.message)
    assert r.ok and "title=Title, date=Date, duration=Duration (min), url=Meet, status=Transcribed" in r.message


def test_meeting_audio_transcript_update_and_fetch(database, tmp_path):
    d = dest(database)
    api = d.api
    m = make_meeting(tmp_path, "2026-01-02T03-04-05-000Z_abc-defg-hij", "Live test meeting")
    original = m.audio_path.read_bytes()

    r1 = d.send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    assert r1.audio and r1.remote_id and r1.url
    page = api.request("GET", f"/pages/{r1.remote_id}")
    props = page["properties"]
    assert props["Title"]["title"][0]["plain_text"] == "Live test meeting"
    assert props["Date"]["date"]["start"].startswith("2026-01-02T03:04")  # Notion keeps minutes only
    assert props["Duration (min)"]["number"] == 0.1
    assert props["Meet"]["url"] == "https://meet.google.com/abc-defg-hij"
    assert props["Transcribed"]["checkbox"] is False

    # Same meeting again with the receipt: updated in place, no second audio.
    m.update(title="Live test meeting (renamed)")
    r2 = dest(database).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path), previous=r1)
    assert r2.remote_id == r1.remote_id and r2.audio
    assert [b["type"] for b in children(api, r1.remote_id)].count("audio") == 1
    # And without the receipt (an answer that was lost): found, not duplicated.
    r3 = dest(database).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    assert r3.remote_id == r1.remote_id
    rows = api.request("POST", f"/databases/{database}/query", {"page_size": 100})["results"]
    assert [p["id"] for p in rows] == [r1.remote_id]

    # A transcript over the limits: > 100 blocks and a turn over 2000 characters.
    long_text = " ".join(f"word{i}" for i in range(900)) + " ✅🙂" * 200
    turns = [{"start": i * 3.0, "end": i * 3.0 + 2, "speaker": "Me" if i % 2 else "Others", "text": f"line {i}"}
             for i in range(130)]
    turns.insert(5, {"start": 15.5, "end": 40.0, "speaker": "Me", "text": long_text})
    (m.dir / "transcript.json").write_text(json.dumps({"turns": turns}, ensure_ascii=False), encoding="utf-8")
    for _ in range(2):  # the second time replaces the first
        rt = d.send_transcript(m, m.transcript_paths, previous=r2)
        assert rt.remote_id == r1.remote_id and rt.audio
    heads = [b for b in children(api, r1.remote_id) if b["type"] == "heading_2"]
    assert len(heads) == 1
    paras = children(api, heads[0]["id"])
    texts = ["".join(t["plain_text"] for t in p["paragraph"]["rich_text"]) for p in paras]
    assert texts[0] == "[00:00] Others: line 0" and texts[-1] == "[06:27] Me: line 129"
    end = next(k for k in range(6, len(texts)) if texts[k].startswith("["))
    assert end - 5 >= 4 and " ".join(texts[5:end]).split() == f"[00:15] Me: {long_text}".split()
    assert len(paras) == 130 + (end - 5)
    page = api.request("GET", f"/pages/{r1.remote_id}")
    assert page["properties"]["Transcribed"]["checkbox"] is True

    # The audio comes back byte for byte, from a freshly signed link.
    got = dest(database).fetch_audio(m, rt, tmp_path / "fetched")
    assert got.suffix == ".opus" and got.read_bytes() == original
    print(f"page {r1.remote_id}: {len(paras)} transcript blocks, audio {len(original)} bytes back intact")


def test_multi_part_upload(database, tmp_path, monkeypatch):
    # Smaller thresholds so an 11 MB file goes in parts (Notion's minimum part is 5 MB).
    monkeypatch.setattr(notion_mod, "SINGLE_PART_MAX", 5 * 1024 * 1024)
    monkeypatch.setattr(notion_mod, "PART_SIZE", 5 * 1024 * 1024)
    m = make_meeting(tmp_path, "2026-01-02T05-00-00-000Z_mul-tipa-rtt", "Live multi-part", seconds=180,
                     bitrate="500k")
    data = m.audio_path.read_bytes()
    assert len(data) > 10 * 1024 * 1024
    d = dest(database)
    r = d.send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    assert r.audio and r.extra["audio_sha256"] == hashlib.sha256(data).hexdigest()
    got = dest(database).fetch_audio(m, r, tmp_path / "fetched")
    assert got.read_bytes() == data
    print(f"multi-part: {len(data)} bytes in {-(-len(data) // (5 * 1024 * 1024))} parts, back intact")
