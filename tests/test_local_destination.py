"""The built-in local destination and the registry."""

from __future__ import annotations

import json

from minuta import destinations
from minuta.destinations import MeetingParts
from minuta.destinations.local import LocalDestination
from minuta.meetings import MeetingStore
from minuta.router import Router


def make_meeting(tmp_path):
    m = MeetingStore(tmp_path / "meetings").get("2026-02-02T09-00-00-000Z_xyz-abcd-efg")
    m.update(status="ready", title="Plan: Q3 / review")
    (m.dir / "audio.opus").write_bytes(b"opus-bytes")
    return m


def test_registry_has_the_builtins_and_creates_by_type():
    assert destinations.registry()["local"] is LocalDestination
    assert {"local", "webhook", "command", "obsidian"} <= set(destinations.registry())
    d = destinations.create("mine", {"type": "local"})
    assert isinstance(d, LocalDestination) and d.name == "mine"


def test_without_copy_to_the_meeting_stays_in_place(tmp_path):
    m = make_meeting(tmp_path)
    d = LocalDestination("local", {"type": "local"})
    assert d.check().ok
    r = d.send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    assert r.remote_id == str(m.dir) and r.audio is False and r.extra["in_place"]


def test_copy_to_copies_audio_metadata_and_transcript_idempotently(tmp_path):
    m = make_meeting(tmp_path)
    target = tmp_path / "Drive" / "Meetings"
    d = LocalDestination("local", {"type": "local", "copy_to": str(target)})
    assert d.check().ok
    r = d.send_meeting(m, MeetingParts(metadata={"id": m.id, "title": "x"}, audio=m.audio_path))
    folder = target / f"{m.id} Plan Q3 review"
    assert r.remote_id == str(folder) and r.audio is True
    assert (folder / "audio.opus").read_bytes() == b"opus-bytes"
    assert json.loads((folder / "meeting.json").read_text())["title"] == "x"
    # Again with the previous receipt: same folder, no duplicates, no partial files.
    r2 = d.send_meeting(m, MeetingParts(audio=m.audio_path), previous=r)
    assert r2.remote_id == r.remote_id
    assert sorted(p.name for p in target.iterdir()) == [folder.name]
    (m.dir / "transcript.md").write_text("# t")
    r3 = d.send_transcript(m, {"md": m.dir / "transcript.md"}, previous=r2)
    assert (folder / "transcript.md").read_text() == "# t" and r3.audio is True
    assert not list(folder.glob(".*partial"))


def test_fetch_audio_back(tmp_path):
    m = make_meeting(tmp_path)
    d = LocalDestination("local", {"type": "local", "copy_to": str(tmp_path / "copy")})
    r = d.send_meeting(m, MeetingParts(audio=m.audio_path))
    got = d.fetch_audio(m, r, m.dir / "fetched")
    assert got.name == "fetched.opus" and got.read_bytes() == b"opus-bytes"


def test_check_rejects_a_file_as_folder(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    assert not LocalDestination("l", {"type": "local", "copy_to": str(f)}).check().ok


def test_router_with_copy_to_allows_deleting_local_audio(cfg, tmp_path):
    m = make_meeting(tmp_path)
    c = {**cfg, "destinations": {"local": {"type": "local", "copy_to": str(tmp_path / "sync")}},
         "storage": {"delete_local_audio_after_upload": True}}
    r = Router(c)
    r.deliver_meeting(m)
    assert r.apply_delete_policy(m) is True and m.audio_path is None
    fetched = r.fetch_audio(m, m.dir / "fetched")
    assert fetched.read_bytes() == b"opus-bytes"
