"""The notion destination against an in-memory Notion (tests/notion_fake.py): no network."""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
import requests

from minuta import cli, credentials, destinations, paths
from minuta import config as config_mod
from minuta.destinations import DestinationError, MeetingParts, PermanentError, Receipt, notion
from minuta.destinations.notion import NotionDestination, parse_id, split_text
from minuta.meetings import MeetingStore

from .conftest import needs_ffmpeg, tone, write_stereo
from .notion_fake import BOT_ID, MB, TOKEN, FakeNotion

PROPS = {"Name": {"type": "title"}, "When": {"type": "date"}, "Minutes": {"type": "number"},
         "Link": {"type": "url"}, "Done": {"type": "checkbox"}, "Notes": {"type": "rich_text"}}


class Secrets:
    def __init__(self, **values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


@pytest.fixture
def fake(monkeypatch):
    f = FakeNotion()

    def session():
        s = requests.Session()
        s.mount("https://", f)
        return s

    monkeypatch.setattr(notion, "new_session", session)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # an environment variable would win over the store
    return f


@pytest.fixture
def db(fake):
    return fake.add_database(PROPS)


def dest(db, token=TOKEN, **opts):
    return NotionDestination("notion", {"type": "notion", "token_secret": "NOTION_TOKEN", "database": db, **opts},
                             Secrets(NOTION_TOKEN=token))


def make_meeting(tmp_path, audio: bytes | None = b"OggS" + bytes(range(256)) * 40, title="Weekly sync",
                 mid="2026-03-03T15-00-00-000Z_abc-defg-hij"):
    m = MeetingStore(tmp_path / "meetings").get(mid)
    m.update(status="ready", title=title, started_at="2026-03-03T15:00:42Z", duration_seconds=1830.0,
             url="https://meet.google.com/abc-defg-hij")
    if audio is not None:
        (m.dir / "audio.opus").write_bytes(audio)
    return m


def metadata(m):
    return m.metadata()


def write_transcript(m, turns):
    doc = {"id": m.id, "title": m.title, "turns": turns}
    (m.dir / "transcript.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (m.dir / "transcript.md").write_text("# t", encoding="utf-8")
    return m.transcript_paths


def heading_and_paragraphs(fake, page_id):
    heads = [b for b in fake.page_children(page_id) if b["type"] == "heading_2"]
    assert len(heads) == 1, heads
    return heads[0], fake.page_children(heads[0]["id"])


def plain(block):
    return "".join(t["text"]["content"] for t in block[block["type"]]["rich_text"])


# --- ids and text ------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "0123456789abcdef0123456789abcdef",
    "01234567-89ab-cdef-0123-456789abcdef",
    "https://www.notion.so/team/Meetings-0123456789abcdef0123456789abcdef?v=fedcba0000000000fedcba0000000000",
    "www.notion.so/0123456789ABCDEF0123456789ABCDEF#section",
])
def test_parse_id_takes_the_id_from_the_path_not_the_view(text):
    assert parse_id(text) == "01234567-89ab-cdef-0123-456789abcdef"


def test_parse_id_rejects_garbage():
    assert parse_id("meetings") is None and parse_id("") is None and parse_id(None) is None


def test_split_text_respects_utf16_units_and_keeps_every_word():
    text = ("word " * 900) + "🙂" * 1500 + " end"
    pieces = split_text(text)
    assert all(len(p.encode("utf-16-le")) // 2 <= 2000 for p in pieces)
    assert "".join(pieces).replace(" ", "") == text.replace(" ", "")  # only cut spaces are gone
    assert pieces[0].endswith("word") and pieces[-1].endswith(" end")
    assert split_text("short") == ["short"] and split_text("") == [""]


# --- check --------------------------------------------------------------------------------
def test_check_reports_properties_by_type_and_writes_nothing(fake, db):
    r = dest(db).check()
    assert r.ok, r.message
    assert "title=Name" in r.message and "date=When" in r.message and "duration=Minutes" in r.message
    assert "url=Link" in r.message and "status=Done" in r.message and "5120.0 MB" in r.message
    assert {m for m, _ in fake.log} == {"GET"}


def test_check_without_token_or_database(fake, db):
    assert not NotionDestination("n", {"type": "notion", "database": db}, Secrets()).check().ok
    assert "database" in NotionDestination("n", {"type": "notion"}, Secrets(NOTION_TOKEN=TOKEN)).check().message


def test_check_with_a_rejected_token_says_what_to_do(fake, db):
    r = dest(db, token="ntn_wrong_token_0000000000").check()
    assert not r.ok and "rejected the token" in r.message and "ntn_wrong_token_0000000000" not in r.message


def test_check_with_a_database_not_shared_says_to_add_the_connection(fake):
    r = dest("0123456789abcdef0123456789abcdef").check()
    assert not r.ok and "Connections" in r.message


def test_check_rejects_a_database_without_title(fake):
    db = fake.add_database({"When": {"type": "date"}})
    r = dest(db).check()
    assert not r.ok and "title" in r.message


# --- detection by type ---------------------------------------------------------------------
def test_any_property_names_work(fake, tmp_path):
    db = fake.add_database({"Nombre": {"type": "title"}, "Fecha": {"type": "date"},
                            "Duración (min)": {"type": "number"}, "Meet": {"type": "url"}})
    m = make_meeting(tmp_path)
    r = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m)))
    props = fake.pages[r.remote_id]["properties"]
    assert props["Nombre"]["title"][0]["plain_text"] == "Weekly sync"
    assert props["Fecha"]["date"]["start"] == "2026-03-03T15:00:00.000+00:00"  # Notion keeps minutes
    assert props["Duración (min)"]["number"] == 30.5 and props["Meet"]["url"].endswith("abc-defg-hij")


def test_ambiguous_types_are_skipped_until_pinned(fake, tmp_path):
    db = fake.add_database({"Name": {"type": "title"}, "Minutes": {"type": "number"}, "Cost": {"type": "number"},
                            "Team": {"type": "select", "select": {"options": [{"name": "Sales"}]}}})
    m = make_meeting(tmp_path)
    d = dest(db)
    assert "set duration_property" in d.check().message
    r = d.send_meeting(m, MeetingParts(metadata=metadata(m)))
    assert set(fake.pages[r.remote_id]["properties"]) == {"Name"}  # no guessing, and Team is left alone
    m2 = make_meeting(tmp_path, mid="2026-03-04T15-00-00-000Z_abc-defg-hij")
    r2 = dest(db, duration_property="Minutes", status_property="Team").send_meeting(m2, MeetingParts(
        metadata=metadata(m2)))
    props = fake.pages[r2.remote_id]["properties"]
    assert props["Minutes"]["number"] == 30.5 and props["Team"]["select"]["name"] == "Recorded"
    assert "Cost" not in props


def test_select_with_status_options_is_used(fake, tmp_path):
    db = fake.add_database({"Name": {"type": "title"},
                            "State": {"type": "select", "select": {"options": [{"name": "Transcribed"}]}}})
    m = make_meeting(tmp_path)
    files = write_transcript(m, [{"start": 0, "end": 1, "speaker": "Ana", "text": "hola"}])
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(metadata=metadata(m)))
    assert fake.pages[r.remote_id]["properties"]["State"]["select"]["name"] == "Recorded"
    d.send_transcript(m, files, r)
    assert fake.pages[r.remote_id]["properties"]["State"]["select"]["name"] == "Transcribed"


# --- create and update -----------------------------------------------------------------------
def test_creates_a_page_with_properties_and_audio(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    r = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    page = fake.pages[r.remote_id]
    assert r.url == page["url"] and r.audio is True
    assert "Done" not in page["properties"]  # a checkbox is only ever ticked, by the transcript
    [audio] = fake.page_children(r.remote_id)
    assert audio["type"] == "audio"
    up = fake.uploads[audio["upload"]]
    assert (up["filename"], up["content_type"], up["mode"]) == ("audio.ogg", "audio/ogg", "single_part")
    assert fake.upload_bytes(up["id"]) == m.audio_path.read_bytes()
    assert r.extra["audio_sha256"] == hashlib.sha256(m.audio_path.read_bytes()).hexdigest()
    assert r.extra["audio_suffix"] == ".opus" and r.extra["database"] == db


def test_second_send_with_receipt_updates_the_same_page_without_uploading_again(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    m.update(title="Weekly sync (renamed)")
    r2 = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path), previous=r)
    assert r2.remote_id == r.remote_id and r2.audio is True and r2.extra["audio_sha256"] == r.extra["audio_sha256"]
    assert len(fake.db_pages(db)) == 1 and len(fake.uploads) == 1
    assert fake.pages[r.remote_id]["properties"]["Name"]["title"][0]["plain_text"] == "Weekly sync (renamed)"
    assert len([b for b in fake.page_children(r.remote_id) if b["type"] == "audio"]) == 1


def test_a_create_whose_answer_was_lost_is_found_instead_of_duplicated(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    fake.inject("POST", r"^/pages$", 502, after=True)
    with pytest.raises(DestinationError):
        dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    assert len(fake.db_pages(db)) == 1  # it was created; the answer was lost
    assert fake.calls("POST", "/pages") == ["/pages"]  # and the client did not blindly post again
    r = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    assert len(fake.db_pages(db)) == 1 and r.audio is True
    assert len(fake.uploads) == 1  # the audio was already there


def test_a_page_with_the_same_title_and_start_from_someone_else_is_not_taken(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    r = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m)))
    fake.pages[r.remote_id]["created_by"]["id"] = "someone-else"
    r2 = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m)))
    assert r2.remote_id != r.remote_id and len(fake.db_pages(db)) == 2


def test_a_page_moved_to_the_trash_gets_a_new_one(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    r = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    fake.pages[r.remote_id]["archived"] = True
    r2 = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path), previous=r)
    assert r2.remote_id != r.remote_id and r2.audio is True and len(fake.uploads) == 2


def test_metadata_left_out_of_send_only_writes_the_title(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    d = dest(db, send=["audio", "transcript"])
    r = d.send_meeting(m, MeetingParts(audio=m.audio_path))
    assert set(fake.pages[r.remote_id]["properties"]) == {"Name"}


# --- transcript ---------------------------------------------------------------------------------
def test_long_transcript_is_split_in_valid_blocks_and_requests(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    long_text = " ".join(f"palabra{i}" for i in range(800)) + " ✅" * 300  # > 2000 characters
    turns = [{"start": i * 7.0, "end": i * 7.0 + 5, "speaker": "Ana" if i % 2 else "Others", "text": f"turn {i}"}
             for i in range(240)]
    turns.insert(3, {"start": 22.0, "end": 60.0, "speaker": "Ana", "text": long_text})
    files = write_transcript(m, turns)
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    r2 = d.send_transcript(m, files, previous=r)
    assert r2.remote_id == r.remote_id and r2.audio is True and r2.extra["audio_sha256"]
    head, paras = heading_and_paragraphs(fake, r.remote_id)
    assert plain(head) == "Transcript" and head["heading_2"]["is_toggleable"]
    assert len(paras) > 241 and len(fake.calls("PATCH", f"/blocks/{head['id']}/children")) >= 3
    texts = [plain(p) for p in paras]
    assert texts[0] == "[00:00] Others: turn 0"
    end = next(k for k in range(4, len(texts)) if texts[k].startswith("["))
    assert end - 3 >= 5  # the long turn took several paragraphs
    assert " ".join(texts[3:end]).split() == f"[00:22] Ana: {long_text}".split()
    assert fake.pages[r.remote_id]["properties"]["Done"]["checkbox"] is True


def test_sending_the_transcript_again_replaces_it(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(metadata=metadata(m)))
    d.send_transcript(m, write_transcript(m, [{"start": 0, "end": 1, "speaker": "Ana", "text": "first"}]), r)
    fake.inject("PATCH", r"^/blocks/.+/children$", 503, times=1)  # fails half way through the next one
    with pytest.raises(DestinationError):
        d.send_transcript(m, write_transcript(m, [{"start": 0, "end": 1, "speaker": "Ana", "text": "second"}]), r)
    d.send_transcript(m, write_transcript(m, [{"start": 0, "end": 1, "speaker": "Ana", "text": "second"}]), r)
    head, paras = heading_and_paragraphs(fake, r.remote_id)
    assert [plain(p) for p in paras] == ["[00:00] Ana: second"]


def test_transcript_with_the_meeting_and_an_empty_one(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    files = write_transcript(m, [])
    r = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path, transcript=files))
    _, paras = heading_and_paragraphs(fake, r.remote_id)
    assert [plain(p) for p in paras] == ["No speech was detected in the recording."]
    assert fake.pages[r.remote_id]["properties"]["Done"]["checkbox"] is True


def test_transcript_without_a_previous_page_creates_one_and_the_meeting_finds_it(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    files = write_transcript(m, [{"start": 0, "end": 1, "speaker": "Ana", "text": "hi"}])
    rt = dest(db).send_transcript(m, files, None)
    assert rt.audio is False
    rm = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    assert rm.remote_id == rt.remote_id and len(fake.db_pages(db)) == 1 and rm.audio is True
    assert fake.pages[rm.remote_id]["properties"]["Done"]["checkbox"] is True  # not unticked


# --- audio ----------------------------------------------------------------------------------------
def test_large_audio_goes_in_parts_and_comes_back_identical(fake, db, tmp_path):
    data = b"OggS" + hashlib.sha256(b"x").digest() * (25 * MB // 32)
    m = make_meeting(tmp_path, audio=data)
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    [up] = fake.uploads.values()
    assert up["mode"] == "multi_part" and up["number_of_parts"] == 3 and up["status"] == "uploaded"
    assert len(fake.calls("POST", f"/file_uploads/{up['id']}/send")) == 3
    got = d.fetch_audio(m, r, m.dir / "fetched")
    assert got.name == "fetched.opus" and got.read_bytes() == data


def test_audio_over_the_workspace_limit_fails_for_good_before_creating_anything(fake, db, tmp_path):
    fake.upload_limit = 5 * MB
    m = make_meeting(tmp_path, audio=b"OggS" + bytes(6 * MB))
    with pytest.raises(PermanentError, match=r"6\.0 MB.*5\.0 MB"):
        dest(db).send_meeting(m, MeetingParts(metadata=metadata(m), audio=m.audio_path))
    assert fake.uploads == {} and fake.pages == {}


def test_mp3_keeps_its_type(fake, db, tmp_path):
    m = make_meeting(tmp_path, audio=None)
    (m.dir / "audio.mp3").write_bytes(b"ID3" + bytes(500))
    r = dest(db).send_meeting(m, MeetingParts(audio=m.audio_path))
    [up] = fake.uploads.values()
    assert (up["filename"], up["content_type"]) == ("audio.mp3", "audio/mpeg") and r.extra["audio_suffix"] == ".mp3"


@needs_ffmpeg
def test_webm_is_remuxed_to_ogg(fake, db, tmp_path):
    m = make_meeting(tmp_path, audio=None)
    write_stereo(m.dir / "audio.webm", tone(1, 440), tone(1, 880))
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(audio=m.audio_path))
    [up] = fake.uploads.values()
    assert fake.upload_bytes(up["id"]).startswith(b"OggS") and r.extra["audio_suffix"] == ".ogg"
    assert d.fetch_audio(m, r, m.dir / "fetched").read_bytes()[:4] == b"OggS"


def test_fetch_audio_asks_for_a_fresh_link_when_one_expired(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(audio=m.audio_path))
    fake.expire_next_downloads = 1
    got = d.fetch_audio(m, r, m.dir / "fetched")
    assert got.read_bytes() == m.audio_path.read_bytes()
    assert len(fake.downloads) == 2 and all("Authorization" not in h for h in fake.downloads)
    assert not list(m.dir.glob(".*partial"))


def test_fetch_audio_refuses_bytes_that_do_not_match(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    d = dest(db)
    r = d.send_meeting(m, MeetingParts(audio=m.audio_path))
    fake.corrupt_downloads = True
    with pytest.raises(DestinationError, match="not the one that was uploaded"):
        d.fetch_audio(m, r, m.dir / "fetched")
    assert not list(m.dir.glob("*fetched*"))


def test_fetch_audio_from_a_page_without_audio(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    r = dest(db).send_meeting(m, MeetingParts(metadata=metadata(m)))
    with pytest.raises(FileNotFoundError):
        dest(db).fetch_audio(m, r, m.dir / "fetched")


# --- retries and errors -----------------------------------------------------------------------
def test_429_is_retryable_with_notions_retry_after_and_5xx_without(fake, db):
    fake.inject("GET", r"^/databases/", 429, headers={"Retry-After": "7"})
    fake.inject("GET", r"^/users/me$", 503)
    with pytest.raises(DestinationError) as e:
        dest(db).schema()
    assert not isinstance(e.value, PermanentError) and e.value.retry_after == 7.0
    with pytest.raises(DestinationError) as e:
        dest(db).api.upload_limit()
    assert not isinstance(e.value, PermanentError) and e.value.retry_after is None


def test_429_on_a_create_is_retried_by_the_router_without_a_duplicate(fake, db, tmp_path):
    from minuta.router import Router

    m = make_meeting(tmp_path)
    fake.inject("POST", r"^/pages$", 429, headers={"Retry-After": "1.5"})
    sleeps = []
    cfg = {"destinations": {"notion": dest(db).options}, "delivery": {"max_attempts": 3}}
    res = Router(cfg, sleep=sleeps.append, factory=lambda name, opts, secrets: dest(db)).deliver_meeting(m)
    assert res["notion"]["status"] == "ok" and res["notion"]["attempts"] == 2
    assert sleeps == [1.5] and len(fake.db_pages(db)) == 1


def test_network_errors_are_retryable_failures(fake, db, monkeypatch):
    class Down(requests.adapters.BaseAdapter):
        def send(self, *a, **k):
            raise requests.ConnectionError("connection refused")

        def close(self):
            pass

    def session():
        s = requests.Session()
        s.mount("https://", Down())
        return s

    monkeypatch.setattr(notion, "new_session", session)
    with pytest.raises(DestinationError) as e:
        dest(db).schema()
    assert not isinstance(e.value, PermanentError)
    assert not dest(db).check().ok


@pytest.mark.parametrize("status, text", [(401, "rejected the token"), (403, "insert content"),
                                          (404, "Connections"), (400, "validation")])
def test_client_errors_are_permanent_and_say_what_to_do(fake, db, tmp_path, status, text):
    m = make_meeting(tmp_path)
    fake.inject("POST", r"^/pages$", status, body={"code": "validation_error" if status == 400 else "x",
                                                   "message": "nope"})
    with pytest.raises(PermanentError, match=text):
        dest(db).send_meeting(m, MeetingParts(metadata=metadata(m)))


# --- secrets ---------------------------------------------------------------------------------
def test_the_token_never_reaches_logs_meeting_json_or_config(fake, db, tmp_path, cfg, caplog, capsys):
    from minuta.router import Router

    secret = "ntn_" + "s3cr3t" * 8
    fake.token = secret
    code = cli.main(["destinations", "add", "notion", "--non-interactive", "--option", f"token={secret}",
                     "--option", f"database={db}"])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert secret not in out.out + out.err and secret not in paths.config_file().read_text()
    saved = config_mod.load()["destinations"]["notion"]
    assert saved["token_secret"] == "NOTION_TOKEN" and "token" not in saved
    assert credentials.store(config_mod.load()).get("NOTION_TOKEN") == secret
    # A remote error that echoes the token is masked everywhere it is written.
    fake.inject("POST", r"^/pages$", 400, body={"code": "validation_error", "message": f"bad {secret}"})
    m = make_meeting(tmp_path)
    c = config_mod.load()
    caplog.set_level(logging.DEBUG)
    res = Router(c, credentials.store(c)).deliver_meeting(m)
    assert res["notion"]["status"] == "failed" and res["notion"]["permanent"]
    assert secret not in m.meta_path.read_text() and secret not in caplog.text
    assert "***" in res["notion"]["error"]


# --- adding it ---------------------------------------------------------------------------------
def test_registered_as_a_builtin():
    assert destinations.registry()["notion"] is NotionDestination


def test_configure_creates_the_database_inside_a_shared_page(fake, capsys):
    page = fake.share_page()
    opts = NotionDestination.configure("notion", {"type": "notion", "token": TOKEN,
                                                  "parent_page": f"https://www.notion.so/Home-{page.replace('-', '')}"})
    assert "parent_page" not in opts and opts["token"] == TOKEN
    created = fake.databases[opts["database"]]
    assert created["parent"]["page_id"] == page
    assert {n: p["type"] for n, p in created["properties"].items()} == {
        "Title": "title", "Date": "date", "Duration (min)": "number", "Meet": "url", "Transcribed": "checkbox"}
    assert "Database created" in capsys.readouterr().out
    d = NotionDestination("notion", {**opts, "token_secret": "T"}, Secrets(T=TOKEN))
    assert d.check().ok and d.schema().describe() == (
        "title=Title, date=Date, duration=Duration (min), url=Meet, status=Transcribed")


def test_configure_with_a_page_the_integration_cannot_see(fake):
    with pytest.raises(PermanentError, match="Connections"):
        NotionDestination.configure("notion", {"token": TOKEN, "parent_page": "0123456789abcdef0123456789abcdef"})


def test_configure_asks_when_interactive(fake, capsys):
    page = fake.share_page()

    class Ask:
        def password(self, message):
            return TOKEN

        def select(self, message, choices, default):
            assert [c[0] for c in choices] == ["existing", "create"]
            return "create"

        def text(self, message, default=""):
            return page

    opts = NotionDestination.configure("notion", {"type": "notion"}, Ask(), Secrets())
    assert opts["token"] == TOKEN and opts["database"] in fake.databases


def test_cli_add_with_a_parent_page_and_test(fake, cfg, capsys):
    page = fake.share_page()
    code = cli.main(["destinations", "add", "notes", "--type", "notion", "--non-interactive",
                     "--option", f"token={TOKEN}", "--option", f"parent_page={page}"])
    out = capsys.readouterr().out
    assert code == 0 and "notes: added" in out and "title=Title" in out
    saved = config_mod.load()["destinations"]["notes"]
    assert saved["database"] in fake.databases and saved["token_secret"] == "NOTES_TOKEN"
    assert cli.main(["destinations", "test", "notes"]) == 0


def test_cli_add_refuses_a_failing_check(fake, cfg, capsys):
    code = cli.main(["destinations", "add", "notion", "--non-interactive", "--option", "token=ntn_wrong_1234567",
                     "--option", "database=0123456789abcdef0123456789abcdef"])
    assert code == 1 and "rejected the token" in capsys.readouterr().err
    assert "notion" not in (config_mod.load().get("destinations") or {})


def test_setup_flags_add_notion(fake, capsys, monkeypatch):
    db = fake.add_database(PROPS)
    code = cli.main(["setup", "--non-interactive", "--no-service", "--no-browser", "--skip-extension",
                     "--destination", "notion", "--option", f"token={TOKEN}", "--option", f"database={db}"])
    out = capsys.readouterr().out
    assert code == 0 and "Destination notion added" in out and TOKEN not in out
    cfg = config_mod.load()
    assert cfg["destinations"]["notion"]["database"] == db and "local" in cfg["destinations"]
    assert TOKEN not in paths.config_file().read_text()


def test_receipt_round_trips_through_meeting_json(fake, db, tmp_path):
    m = make_meeting(tmp_path)
    r = dest(db).send_meeting(m, MeetingParts(audio=m.audio_path))
    again = Receipt.from_dict(json.loads(json.dumps(r.to_dict())))
    assert dest(db).fetch_audio(m, again, m.dir / "x").read_bytes() == m.audio_path.read_bytes()
    assert BOT_ID not in json.dumps(r.to_dict())


def test_setup_assistant_adds_notion_and_can_free_local_audio(fake, cfg, capsys):
    from minuta.setup import destinations_step

    db = fake.add_database(PROPS)

    class P:
        interactive = True

        def confirm(self, message, default):
            return True

        def password(self, message):
            return TOKEN

        def select(self, message, choices, default):
            return "notion" if any(value == "notion" for value, _ in choices) else "existing"

        def text(self, message, default=""):
            return f"https://www.notion.so/Meetings-{db.replace('-', '')}?v=0123456789abcdef0123456789abcdef"

    class Args:
        notion_database = notion_parent_page = notion_token_env = None

    c = config_mod.load()
    assert destinations_step(c, Args(), P()) == ["notion"]
    assert c["destinations"]["notion"]["database"] == db and "token" not in c["destinations"]["notion"]
    assert c["storage"]["delete_local_audio_after_upload"] is True
    assert c["destinations"]["local"]["send"] == ["transcript", "metadata"]
    assert TOKEN not in capsys.readouterr().out
