"""The obsidian destination: one note per meeting, user edits kept across deliveries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minuta.destinations import MeetingParts, PermanentError
from minuta.destinations.obsidian import END, PENDING, START, ObsidianDestination
from minuta.meetings import MeetingStore


def make_meeting(tmp_path, mid="2026-02-02T09-00-00-000Z_xyz-abcd-efg", title='Plan: Q3 / "review"'):
    m = MeetingStore(tmp_path / "meetings").get(mid)
    m.update(status="ready", title=title, started_at="2026-02-02T09:00:00+00:00", duration_seconds=1830,
             url="https://meet.google.com/xyz-abcd-efg")
    return m


def transcript(m, text="hola"):
    doc = {"title": "t", "turns": [{"start": 65.0, "end": 70.0, "speaker": "Me", "text": text}]}
    (m.dir / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    return {"json": m.dir / "transcript.json"}


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "Vault"
    (v / ".obsidian").mkdir(parents=True)
    return v


def dest(vault, **opts):
    return ObsidianDestination("notes", {"type": "obsidian", "vault": str(vault), **opts})


def test_note_with_frontmatter_then_transcript_keeps_user_edits(tmp_path, vault):
    m = make_meeting(tmp_path)
    d = dest(vault, tags="meeting, work")
    assert d.check().ok
    r1 = d.send_meeting(m, MeetingParts(metadata={"id": m.id}))
    note = vault / "Meetings" / "2026-02-02 Plan Q3 review.md"
    assert r1.remote_id == str(note)
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---\ntitle: \"Plan: Q3 / \\\"review\\\"\"\n")
    assert 'minuta_id: "2026-02-02T09-00-00-000Z_xyz-abcd-efg"' in text and "duration_minutes: 30" in text
    assert 'tags: ["meeting", "work"]' in text and PENDING in text
    # The user writes notes and a property; the transcript arrives later.
    note.write_text(text.replace("---\n\n", "---\n\nMy notes before.\n\n", 1).replace(
        "tags:", "status: done\ntags:") + "\nMy notes after.\n", encoding="utf-8")
    r2 = d.send_transcript(m, transcript(m), previous=r1)
    text = note.read_text(encoding="utf-8")
    assert r2.remote_id == r1.remote_id and "**[01:05] Me:** hola" in text and PENDING not in text
    assert "My notes before." in text and "My notes after." in text and "status: done" in text
    assert text.count(START) == 1 and text.count(END) == 1
    # A late meeting delivery never replaces a transcript with "pending"; nothing is duplicated.
    d.send_meeting(m, MeetingParts(metadata={"id": m.id}), previous=r2)
    assert "hola" in note.read_text(encoding="utf-8")
    assert [p.name for p in (vault / "Meetings").iterdir()] == [note.name]


def test_same_title_on_the_same_day_gets_its_own_note(tmp_path, vault):
    a = make_meeting(tmp_path, title="Daily")
    b = make_meeting(tmp_path, mid="2026-02-02T15-00-00-000Z_aaa-bbbb-ccc", title="Daily")
    b.update(started_at="2026-02-02T15:00:00+00:00")
    d = dest(vault)
    ra = d.send_meeting(a, MeetingParts(metadata={}))
    rb = d.send_meeting(b, MeetingParts(metadata={}))
    assert ra.remote_id != rb.remote_id and rb.remote_id.endswith("2026-02-02 Daily (2).md")
    # Without a receipt (state lost), each meeting still finds its own note instead of a third one.
    assert d.send_transcript(b, transcript(b, "b")).remote_id == rb.remote_id
    assert len(list((vault / "Meetings").iterdir())) == 2


def test_folder_cannot_escape_the_vault_and_vault_must_exist(tmp_path, vault):
    assert not dest(vault, folder="../outside").check().ok
    assert not dest(tmp_path / "missing").check().ok
    assert not ObsidianDestination("n", {"type": "obsidian"}).check().ok
    with pytest.raises(PermanentError):
        dest(vault, folder="../../etc").send_meeting(make_meeting(tmp_path), MeetingParts(metadata={}))
    assert dest(vault, folder="").check().ok


def test_a_markerless_note_gets_the_transcript_appended(tmp_path, vault):
    m = make_meeting(tmp_path)
    d = dest(vault)
    r = d.send_meeting(m, MeetingParts(metadata={}))
    note = vault / "Meetings" / "2026-02-02 Plan Q3 review.md"
    note.write_text("I rewrote everything.\n", encoding="utf-8")
    d.send_transcript(m, transcript(m), previous=r)
    text = note.read_text(encoding="utf-8")
    assert text.startswith("I rewrote everything.") and "hola" in text and not list(vault.rglob(".*partial"))


@pytest.mark.parametrize("started_at", ["/tmp/x", "../../../outside", "2026-02-02/../../x", "..\\..\\x"])
def test_a_crafted_start_time_cannot_move_the_note_out_of_the_folder(tmp_path, vault, started_at):
    m = make_meeting(tmp_path)
    m.update(started_at=started_at)
    r = dest(vault).send_meeting(m, MeetingParts(metadata={}))
    note = vault / "Meetings" / f"{m.id[:10]} Plan Q3 review.md"
    assert r.remote_id == str(note) and note.is_file()
    assert [p for p in tmp_path.rglob("*.md") if vault not in p.parents] == []


def test_the_title_is_one_escaped_line_in_the_heading_and_the_frontmatter(tmp_path, vault):
    m = make_meeting(tmp_path, title="# Plan\n---\ninjected: true\n[click](https://evil.example) ![x](y) <b>")
    note = Path(dest(vault).send_meeting(m, MeetingParts(metadata={})).remote_id)
    text = note.read_text(encoding="utf-8")
    front, body = text.split("\n---\n", 1)
    assert "injected: true" not in front.splitlines() and front.count("\n") == 6
    assert 'title: "# Plan --- injected: true [click](https://evil.example) ![x](y) <b>"' in front
    heading = next(line for line in body.splitlines() if line.startswith("# "))
    assert heading == r"# \# Plan --- injected: true \[click\]\(https://evil.example\) \!\[x\]\(y\) \<b\>"
