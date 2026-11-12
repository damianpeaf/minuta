"""The built-in local destination and the registry."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from minuta import destinations
from minuta.destinations import MeetingParts
from minuta.destinations.local import LocalDestination
from minuta.meetings import MeetingStore
from minuta.router import Router


def make_meeting(tmp_path):
    m = MeetingStore(tmp_path / "meetings").get("2026-02-02T09-00-00-000Z_xyz-abcd-efg")
    m.update(status="ready", title="Plan: Q3 / review", started_at="2026-02-02T09:00:00Z")
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
    stamp = datetime(2026, 2, 2, 9, 0, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H-%M")
    folder = target / f"{stamp} Plan Q3 review"  # local time and the title; the id stays in meeting.json
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


def test_a_second_meeting_with_the_same_name_gets_its_own_folder(tmp_path):
    target = tmp_path / "Drive"
    d = LocalDestination("local", {"type": "local", "copy_to": str(target)})
    store = MeetingStore(tmp_path / "meetings")
    folders = []
    for suffix in ("aaa-bbbb-ccc", "ddd-eeee-fff"):
        m = store.get(f"2026-02-02T09-00-00-000Z_{suffix}")
        m.update(status="ready", title="Daily", started_at="2026-02-02T09:00:00Z")
        r = d.send_meeting(m, MeetingParts(metadata=m.metadata()))
        folders.append(Path(r.remote_id).name)
    assert folders[1] == f"{folders[0]} (2)"


def test_a_meeting_without_title_uses_the_meet_code(tmp_path):
    m = MeetingStore(tmp_path / "meetings").get("2026-02-02T09-00-00-000Z_meet")
    m.update(status="ready", code="xyz-abcd-efg", started_at="2026-02-02T09:00:00Z")
    r = LocalDestination("local", {"type": "local", "copy_to": str(tmp_path / "D")}).send_meeting(
        m, MeetingParts(metadata=m.metadata()))
    assert Path(r.remote_id).name.endswith(" xyz-abcd-efg")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize("private, dir_mode, file_mode", [(None, 0o700, 0o600), (False, None, None)])
def test_copies_are_private_unless_configured_otherwise(tmp_path, private, dir_mode, file_mode):
    m = make_meeting(tmp_path)
    target = tmp_path / "Documents"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)  # noqa: S103 - a typical shared folder
    opts = {"type": "local", "copy_to": str(target)}
    if private is not None:
        opts["private"] = private
    r = LocalDestination("local", opts).send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    folder = Path(r.remote_id)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755  # the user's folder is not touched
    if dir_mode is None:
        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(folder.stat().st_mode) == 0o777 & ~umask
    else:
        assert stat.S_IMODE(folder.stat().st_mode) == dir_mode
        for f in folder.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == file_mode, f
