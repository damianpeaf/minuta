"""End to end through the router: a recorded meeting goes to `local` and to Notion (in memory),
the local audio is deleted only when Notion confirmed it holds it, and transcribing afterwards
gets the audio back from Notion."""

from __future__ import annotations

import pytest

from minuta import audio, credentials
from minuta.meetings import MeetingStore
from minuta.router import Router
from minuta.transcription.pipeline import transcribe

from .conftest import bursts, needs_ffmpeg, write_stereo
from .notion_fake import TOKEN
from .test_notion_destination import PROPS, fake  # noqa: F401 - the fixture
from .test_transcription import FakeEngine


def setup_cfg(cfg, db, mode="on-demand"):
    c = {**cfg, "storage": {"delete_local_audio_after_upload": True},
         "transcription": {**cfg["transcription"], "mode": mode},
         "destinations": {
             # The local copy stays in place, so it does not hold the audio once it is deleted.
             "local": {"type": "local", "send": ["metadata", "transcript"]},
             "notion": {"type": "notion", "token_secret": "NOTION_TOKEN", "database": db},
         }}
    credentials.store(c).set("NOTION_TOKEN", TOKEN)
    return c


def recorded(tmp_path, ogg: bool):
    m = MeetingStore(tmp_path / "meetings").get("2026-05-05T09-00-00-000Z_end-toen-ddd")
    m.update(status="ready", title="End to end", started_at="2026-05-05T09:00:00Z", duration_seconds=15.0,
             url="https://meet.google.com/end-toen-ddd")
    if ogg:
        webm = write_stereo(tmp_path / "rec.webm", bursts([(1, 3), (10, 12)], 15), bursts([(5, 8)], 15, freq=500))
        audio.ffmpeg("-i", str(webm), "-c:a", "copy", "-f", "ogg", str(m.dir / "audio.opus"))
    else:
        (m.dir / "audio.opus").write_bytes(b"OggS" + bytes(1000))
    return m


@needs_ffmpeg
def test_deliver_delete_and_transcribe_from_notion(fake, cfg, tmp_path):  # noqa: F811
    db = fake.add_database(PROPS)
    c = setup_cfg(cfg, db)
    m = recorded(tmp_path, ogg=True)
    original = m.audio_path.read_bytes()

    # Notion rejects the token: local is delivered, Notion is not, the audio stays.
    fake.token = "ntn_rotated_token_000000"
    r = Router(c, credentials.store(c), sleep=lambda s: None)
    res = r.deliver_meeting(m)
    assert res["local"]["status"] == "ok" and res["notion"]["status"] == "failed" and res["notion"]["permanent"]
    assert r.apply_delete_policy(m) is False and m.audio_path is not None

    # The token works again: `minuta retry` delivers it and only then the local audio goes.
    fake.token = TOKEN
    r.retry(m)
    st = m.read()["destinations"]["notion"]["meeting"]
    assert st["status"] == "ok" and st["receipt"]["audio"] is True
    assert m.audio_path is None and m.read()["audio"]["deleted"] is True
    assert fake.upload_bytes(next(iter(fake.uploads))) == original

    # Transcribing now gets the audio from Notion and sends the transcript to both destinations.
    doc = transcribe(m, c, "local", router=r, engine=FakeEngine(["Hello.", "Bye."]))
    assert [t["speaker"] for t in doc["turns"]] == ["Me", "Others", "Me"]
    assert not list(m.dir.glob("fetched*")) and not list(m.dir.glob(".*partial"))
    states = m.read()["destinations"]
    assert states["notion"]["transcript"]["status"] == "ok" and states["local"]["transcript"]["status"] == "ok"
    page_id = st["receipt"]["remote_id"]
    assert len(fake.db_pages(db)) == 1
    heads = [b for b in fake.page_children(page_id) if b["type"] == "heading_2"]
    assert len(heads) == 1 and len(fake.page_children(heads[0]["id"])) == 3
    assert fake.pages[page_id]["properties"]["Done"]["checkbox"] is True
    assert all("Authorization" not in h for h in fake.downloads)


def test_auto_mode_keeps_the_audio_until_it_is_transcribed(fake, cfg, tmp_path):  # noqa: F811
    db = fake.add_database(PROPS)
    c = setup_cfg(cfg, db, mode="auto")
    m = recorded(tmp_path, ogg=False)
    r = Router(c, credentials.store(c), sleep=lambda s: None)
    r.deliver_meeting(m)
    assert m.read()["destinations"]["notion"]["meeting"]["receipt"]["audio"] is True
    assert r.apply_delete_policy(m) is False and m.audio_path is not None


def test_audio_too_big_for_the_workspace_keeps_the_local_copy(fake, cfg, tmp_path):  # noqa: F811
    db = fake.add_database(PROPS)
    fake.upload_limit = 500
    c = setup_cfg(cfg, db)
    m = recorded(tmp_path, ogg=False)
    r = Router(c, credentials.store(c), sleep=lambda s: None)
    res = r.deliver_meeting(m)
    assert res["notion"]["permanent"] and "takes files up to" in res["notion"]["error"]
    assert r.apply_delete_policy(m) is False and m.audio_path is not None
    assert len(fake.calls("POST", "/pages")) == 0


@pytest.mark.parametrize("send", [["metadata", "transcript"], ["audio", "metadata", "transcript"]])
def test_notion_without_audio_never_allows_deleting(fake, cfg, tmp_path, send):  # noqa: F811
    db = fake.add_database(PROPS)
    c = setup_cfg(cfg, db)
    c["destinations"]["notion"]["send"] = ["metadata", "transcript"]
    c["destinations"]["local"]["send"] = send
    m = recorded(tmp_path, ogg=False)
    r = Router(c, credentials.store(c), sleep=lambda s: None)
    r.deliver_meeting(m)
    assert fake.uploads == {} and r.apply_delete_policy(m) is False and m.audio_path is not None
