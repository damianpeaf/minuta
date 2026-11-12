"""The rclone destination against a fake rclone program: arguments, validation, errors, verification.

The fake (tests/fake_rclone.py) maps remotes to local folders and logs every argv it receives, so
these tests see exactly what minuta would run. Real rclone runs in test_rclone_live.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from minuta import cli, destinations
from minuta import config as config_mod
from minuta.destinations import DestinationError, MeetingParts, PermanentError, Receipt
from minuta.destinations.rclone import (
    RcloneDestination,
    meeting_folder_name,
    sanitized_metadata,
    validate_folder,
    validate_remote,
)
from minuta.destinations.rclone_setup import NEW_DRIVE
from minuta.meetings import MeetingStore
from minuta.router import Router

HERE = Path(__file__).resolve().parent
posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake rclone is a script with a shebang")

MALICIOUS_REMOTES = ["--config=/etc/x", "-x", "a:b", "a/b", ";rm", "x;rm -rf ~", "../x", "", "  ", ":", "a\nb",
                     "$(id)", "`id`", "a|b", "--", "-"]
MALICIOUS_FOLDERS = ["../x", "a/../b", "..", "--config=/etc/x", "-x", "a/-x", "x;rm -rf ~", ";rm", "$(id)", "`id`",
                     "a\\b", "a:b", "a\nb", "a\x00b", "a|b", "a&b", "a/ b", "a /b"]


# --- fixtures ------------------------------------------------------------------------------
class Fake:
    def __init__(self, tmp: Path, monkeypatch):
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        self.binary = self.bin_dir / "rclone"
        self.binary.write_text(f"#!{sys.executable}\n" + (HERE / "fake_rclone.py").read_text())
        self.binary.chmod(0o755)
        self.log = tmp / "rclone.log"
        self.cloud = tmp / "cloud"
        self.cloud.mkdir()
        self.monkeypatch = monkeypatch
        self.remotes({"gdrive": self.cloud})
        monkeypatch.setenv("FAKE_RCLONE_LOG", str(self.log))
        for v in ("FAKE_RCLONE_FAIL", "FAKE_RCLONE_SLEEP", "FAKE_RCLONE_NOHASH", "FAKE_RCLONE_CORRUPT"):
            monkeypatch.delenv(v, raising=False)

    def remotes(self, mapping: dict) -> None:
        self.monkeypatch.setenv("FAKE_RCLONE_REMOTES", json.dumps({k: str(v) for k, v in mapping.items()}))

    def fail(self, command: str, code: int, stderr: str, times: int = -1) -> None:
        self.monkeypatch.setenv("FAKE_RCLONE_FAIL", json.dumps({command: [code, stderr, times]}))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def dest(self, **opts) -> RcloneDestination:
        options = {"type": "rclone", "remote": "gdrive", "folder": "minuta", "binary": str(self.binary), **opts}
        return RcloneDestination("drive", options)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return Fake(tmp_path, monkeypatch)


def make_meeting(tmp_path, mid="2026-02-02T09-00-00-000Z_xyz-abcd-efg", title="Plan: Q3 / review"):
    m = MeetingStore(tmp_path / "meetings").get(mid)
    m.update(status="ready", title=title, started_at="2026-02-02T09:00:00+00:00", code="xyz-abcd-efg",
             destinations={"other": {"meeting": {"receipt": {"extra": {"token": "abcdef123456"}}}}})
    (m.dir / "audio.opus").write_bytes(b"opus-bytes" * 100)
    return m


def add_transcript(m, text="# hello"):
    (m.dir / "transcript.md").write_text(text)
    (m.dir / "transcript.json").write_text(json.dumps({"text": text}))
    (m.dir / "transcript.vtt").write_text("WEBVTT\n\n" + text)
    return m.transcript_paths


def local_name(m):
    return meeting_folder_name(m.id, m.title, m.read()["started_at"])


# --- validation ----------------------------------------------------------------------------
@pytest.mark.parametrize("value", MALICIOUS_REMOTES)
def test_malicious_remote_names_are_rejected(value):
    with pytest.raises(ValueError):
        validate_remote(value)


@pytest.mark.parametrize("value", MALICIOUS_FOLDERS)
def test_malicious_folders_are_rejected(value):
    with pytest.raises(ValueError):
        validate_folder(value)


@pytest.mark.parametrize("value,expected", [("gdrive", "gdrive"), ("gdrive:", "gdrive"), ("My Drive_2", "My Drive_2"),
                                            ("s3.backup-eu", "s3.backup-eu"), ("me@work+x", "me@work+x"),
                                            (" padded ", "padded")])
def test_valid_remote_names(value, expected):
    assert validate_remote(value) == expected


@pytest.mark.parametrize("value,expected", [("minuta", "minuta"), ("/srv/minuta/", "/srv/minuta"), ("", ""),
                                            ("Reuniones//2026/", "Reuniones/2026"), ("./a/./b", "a/b"),
                                            ("Equipo (ventas) #1", "Equipo (ventas) #1")])
def test_valid_folders_are_normalized(value, expected):
    assert validate_folder(value) == expected


@pytest.mark.parametrize("opts", [{"remote": "--config=/etc/x"}, {"remote": ";rm"}, {"folder": "../.."},
                                  {"folder": "--config=/etc/x"}, {"folder": "x;rm -rf ~"},
                                  {"config": "--config=/etc/x"}, {"binary": "-x"}, {"timeout_seconds": "soon"}])
def test_check_refuses_bad_options_without_running_anything(opts):
    calls = []
    d = RcloneDestination("d", {"type": "rclone", "remote": "gdrive", "folder": "m", **opts},
                          runner=lambda *a, **k: calls.append(a))
    r = d.check()
    assert not r.ok and r.message
    assert calls == []


def test_a_tampered_receipt_is_refused_before_running_anything(tmp_path):
    calls = []
    d = RcloneDestination("d", {"type": "rclone", "remote": "gdrive", "binary": sys.executable},
                          runner=lambda *a, **k: calls.append(a))
    m = make_meeting(tmp_path)
    bad_places = [{"remote": "--config=/etc/x", "base": "", "folder": "f"},
                  {"remote": "gdrive", "base": "../..", "folder": "f"},
                  {"remote": "gdrive", "base": "", "folder": ".."},
                  {"remote": "gdrive;rm", "base": "", "folder": "f"}]
    for extra in bad_places:
        with pytest.raises(PermanentError):
            d.fetch_audio(m, Receipt(remote_id="x", audio=True, extra={**extra, "audio_name": "audio.opus"}),
                          tmp_path / "out")
        with pytest.raises(PermanentError):
            d.send_transcript(m, add_transcript(m), Receipt(remote_id="x", extra=extra))
    with pytest.raises(PermanentError):
        d.fetch_audio(m, Receipt(remote_id="x", audio=True, extra={"remote": "gdrive", "folder": "f",
                                                                   "audio_name": "../../.ssh/id_rsa"}), tmp_path / "o")
    assert calls == []


def test_folder_name_uses_local_time_and_a_clean_title():
    name = meeting_folder_name("2026-02-02T09-00-00-000Z_x", "Plan: Q3 / review <final>?", "2026-02-02T09:00:00Z")
    from datetime import datetime, timezone

    stamp = datetime(2026, 2, 2, 9, 0, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H-%M")
    assert name == f"{stamp} Plan Q3 review final"
    # No title: the id; no started_at: the time inside the id.
    assert meeting_folder_name("2026-02-02T09-00-00-000Z_x", None, None) == f"{stamp} 2026-02-02T09-00-00-000Z_x"
    assert meeting_folder_name("weird", "..", None) == "weird"
    assert "/" not in meeting_folder_name("x", "../../etc/passwd", None)


def test_sanitized_metadata_keeps_public_keys_and_masks_secrets():
    meta = sanitized_metadata({"id": "m", "title": "call token=supersecret123", "destinations": {"x": 1},
                               "audio": {"file": "audio.opus", "api_key": "k"}, "error": "boom"})
    assert set(meta) == {"id", "title", "audio"}
    assert "supersecret123" not in json.dumps(meta) and meta["audio"] == {"file": "audio.opus"}


# --- running the fake ----------------------------------------------------------------------
@posix_only
def test_send_meeting_uploads_verifies_and_returns_a_receipt(fake, tmp_path):
    m = make_meeting(tmp_path)
    files = add_transcript(m)
    d = fake.dest()
    r = d.send_meeting(m, MeetingParts(metadata=m.metadata(), audio=m.audio_path, transcript=files))
    folder = fake.cloud / "minuta" / local_name(m)
    assert sorted(p.name for p in folder.iterdir()) == ["audio.opus", "meeting.json", "transcript.json",
                                                         "transcript.md", "transcript.vtt"]
    assert (folder / "audio.opus").read_bytes() == m.audio_path.read_bytes()
    uploaded_meta = json.loads((folder / "meeting.json").read_text())
    assert uploaded_meta["id"] == m.id and "destinations" not in uploaded_meta
    assert "abcdef123456" not in (folder / "meeting.json").read_text()
    assert r.audio is True and r.remote_id == f"gdrive:minuta/{local_name(m)}" and r.url is None
    assert r.extra["audio_name"] == "audio.opus" and r.extra["verified"] == "hash"
    assert r.extra["files"]["audio.opus"] == 1000
    # Nothing left behind in the meeting folder.
    assert not [p for p in m.dir.iterdir() if p.name.startswith(".rclone")]


@posix_only
def test_every_call_is_an_argument_list_with_paths_after_double_dash(fake, tmp_path, monkeypatch):
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw))
        return subprocess.run(argv, **kw)

    m = make_meeting(tmp_path, title="Q3 'plan' (draft) #1 !")
    d = RcloneDestination("drive", {"type": "rclone", "remote": "My Drive", "folder": "Equipo (ventas)",
                                    "binary": str(fake.binary)}, runner=runner)
    fake.remotes({"My Drive": fake.cloud})
    d.send_meeting(m, MeetingParts(audio=m.audio_path))
    assert seen
    for argv, kw in seen:
        assert isinstance(argv, list) and kw["shell"] is False and kw["stdin"] == subprocess.DEVNULL
        assert argv[0] == str(fake.binary) and "--ask-password=false" in argv
        assert kw["timeout"] > 0
        cmd_at = next(i for i, a in enumerate(argv[1:], 1) if not a.startswith("-") and argv[i - 1] not in (
            "--config", "--contimeout", "--timeout", "--max-depth"))
        assert argv[cmd_at] in ("lsjson", "copyto", "check", "cat")
        dd = argv.index("--")
        assert all(":" in p or Path(p).is_absolute() for p in argv[dd + 1:])
    # The odd characters reach rclone untouched, as one argument each.
    copy = next(a for a, _ in seen if "copyto" in a)
    assert copy[-1] == f"My Drive:Equipo (ventas)/{local_name(m)}/audio.opus"
    assert (fake.cloud / "Equipo (ventas)" / local_name(m) / "audio.opus").exists()


@posix_only
def test_second_send_reuses_the_folder_and_transcript_updates_in_place(fake, tmp_path):
    m = make_meeting(tmp_path)
    d = fake.dest()
    r1 = d.send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    r2 = d.send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path), previous=r1)
    assert r2.remote_id == r1.remote_id and len(list((fake.cloud / "minuta").iterdir())) == 1
    files = add_transcript(m, "# first")
    r3 = d.send_transcript(m, files, previous=r2)
    files = add_transcript(m, "# second, longer")
    r4 = d.send_transcript(m, files, previous=r3)
    folder = fake.cloud / "minuta" / local_name(m)
    assert (folder / "transcript.md").read_text() == "# second, longer"
    assert r4.audio is True and r4.extra["audio_name"] == "audio.opus" and r4.remote_id == r1.remote_id
    assert set(r4.extra["files"]) == {"audio.opus", "meeting.json", "transcript.json", "transcript.md",
                                      "transcript.vtt"}
    # The receipt still points to the old place after the user changes the folder.
    moved = fake.dest(folder="elsewhere")
    r5 = moved.send_transcript(m, files, previous=r4)
    assert r5.remote_id == r1.remote_id and not (fake.cloud / "elsewhere").exists()


@posix_only
def test_another_meeting_with_the_same_name_gets_its_own_folder(fake, tmp_path):
    a = make_meeting(tmp_path, "2026-02-02T09-00-00-000Z_aaa-aaaa-aaa", "Standup")
    b = make_meeting(tmp_path, "2026-02-02T09-00-30-000Z_bbb-bbbb-bbb", "Standup")
    b.update(started_at=a.read()["started_at"])
    d = fake.dest()
    ra = d.send_meeting(a, MeetingParts(metadata={"id": a.id}, audio=a.audio_path))
    rb = d.send_meeting(b, MeetingParts(metadata={"id": b.id}, audio=b.audio_path))
    assert ra.remote_id != rb.remote_id and rb.remote_id.endswith(b.id)


@posix_only
def test_retry_after_a_partial_upload_reuses_the_half_finished_folder(fake, tmp_path):
    m = make_meeting(tmp_path)
    d = fake.dest()
    fake.fail("check", 1, "ERROR : network blip, connection reset", times=1)
    with pytest.raises(DestinationError) as e:
        d.send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))
    assert not isinstance(e.value, PermanentError)
    r = d.send_meeting(m, MeetingParts(metadata={"id": m.id}, audio=m.audio_path))  # no previous receipt
    assert r.remote_id == f"gdrive:minuta/{local_name(m)}"
    assert [p.name for p in (fake.cloud / "minuta").iterdir()] == [local_name(m)]


@posix_only
def test_a_corrupted_upload_is_never_confirmed(fake, tmp_path, monkeypatch):
    m = make_meeting(tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_CORRUPT", "1")
    with pytest.raises(DestinationError, match="do not match"):
        fake.dest().send_meeting(m, MeetingParts(audio=m.audio_path))


@posix_only
def test_without_a_common_hash_the_receipt_says_size(fake, tmp_path, monkeypatch):
    m = make_meeting(tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_NOHASH", "1")
    assert fake.dest().send_meeting(m, MeetingParts(audio=m.audio_path)).extra["verified"] == "size"


@posix_only
@pytest.mark.parametrize("command,code,stderr,permanent", [
    ("copyto", 1, 'Failed to create file system for "gdrive:": didn\'t find section in config file', True),
    ("copyto", 1, "couldn't fetch token: oauth2: \"invalid_grant\" \"Token has been expired or revoked.\"", True),
    ("copyto", 1, "googleapi: Error 403: Insufficient Permission, forbidden", True),
    ("copyto", 1, "InvalidAccessKeyId: The AWS Access Key Id you provided does not exist", True),
    ("copyto", 1, "ssh: handshake failed: ssh: unable to authenticate, attempted methods [none password]", True),
    ("copyto", 1, "NOTICE: x: No host key validation is being performed. Set known_hosts_file\nCRITICAL: Failed to "
                  "create file system: couldn't connect SSH: ssh: unexpected message type 51 (expected 60)", True),
    ("copyto", 1, "NOTICE: x: No host key validation\nCRITICAL: Failed to create file system: couldn't connect SSH: "
                  "dial tcp 127.0.0.1:1: connect: connection refused", False),
    ("copyto", 7, "Fatal error received - not attempting retries", True),
    ("copyto", 1, "googleapi: Error 403: User Rate Limit Exceeded, userRateLimitExceeded", False),
    ("copyto", 5, "temporary error: 503 Service Unavailable", False),
    ("copyto", 1, "dial tcp: lookup www.googleapis.com: no such host", False),
    ("copyto", 1, "something unexpected", False),
])
def test_errors_are_classified(fake, tmp_path, command, code, stderr, permanent):
    m = make_meeting(tmp_path)
    fake.fail(command, code, stderr)
    with pytest.raises(DestinationError) as e:
        fake.dest().send_meeting(m, MeetingParts(audio=m.audio_path))
    assert isinstance(e.value, PermanentError) is permanent, str(e.value)


@posix_only
def test_a_hung_rclone_times_out_and_can_be_retried(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_SLEEP", "5")
    m = make_meeting(tmp_path)
    d = fake.dest(timeout_seconds=1)
    d.LIST_TIMEOUT = 1
    with pytest.raises(DestinationError, match="timed out") as e:
        d.send_meeting(m, MeetingParts(audio=m.audio_path))
    assert not isinstance(e.value, PermanentError)


def test_missing_rclone_explains_how_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    d = RcloneDestination("d", {"type": "rclone", "remote": "gdrive"})
    r = d.check()
    assert not r.ok and "rclone is not installed" in r.message and "rclone.org" in r.message
    with pytest.raises(PermanentError, match="not installed"):
        d.send_meeting(make_meeting(tmp_path), MeetingParts(audio=None, metadata={"id": "x"}))


@posix_only
def test_check_has_no_side_effects_and_reports_problems(fake, tmp_path):
    d = fake.dest()
    r = d.check()
    assert r.ok and "created on the first upload" in r.message
    assert list(fake.cloud.iterdir()) == []
    assert [c for c in fake.calls() if "listremotes" in c] and [c for c in fake.calls() if "gdrive:minuta" in c]
    assert not any("copyto" in c or "check" in c for c in fake.calls())
    (fake.cloud / "minuta").mkdir()
    assert fake.dest().check().message == "uploads to gdrive:minuta"
    r = fake.dest(remote="nope").check()
    assert not r.ok and "no remote named 'nope'" in r.message and "gdrive" in r.message
    assert not fake.dest(config=str(tmp_path / "missing.conf")).check().ok


@posix_only
def test_config_password_goes_through_the_environment_only(fake, tmp_path, caplog):
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw["env"]))
        return subprocess.run(argv, **kw)

    class Secrets:
        def get(self, name):
            return "hunter2-very-secret" if name == "DRIVE_CONFIG_PASSWORD" else None

    d = RcloneDestination("drive", {"type": "rclone", "remote": "gdrive", "binary": str(fake.binary),
                                    "config_password_secret": "DRIVE_CONFIG_PASSWORD"}, Secrets(), runner)
    caplog.set_level("DEBUG")
    assert d.check().ok
    for argv, env in seen:
        assert env["RCLONE_CONFIG_PASS"] == "hunter2-very-secret"
        assert not any("hunter2" in a for a in argv)
    assert "hunter2" not in caplog.text and "hunter2" not in fake.log.read_text()


@posix_only
def test_fetch_audio_brings_the_same_bytes_back(fake, tmp_path):
    m = make_meeting(tmp_path)
    d = fake.dest()
    r = d.send_meeting(m, MeetingParts(audio=m.audio_path))
    got = d.fetch_audio(m, r, tmp_path / "back" / "fetched")
    assert got.name == "fetched.opus" and got.read_bytes() == m.audio_path.read_bytes()
    assert not list(got.parent.glob(".*partial"))
    (fake.cloud / "minuta" / local_name(m) / "audio.opus").write_bytes(b"short")
    with pytest.raises(DestinationError, match="expected 1000"):
        d.fetch_audio(m, r, tmp_path / "back2" / "x.opus")
    assert not list((tmp_path / "back2").iterdir())


@posix_only
def test_router_deletes_local_audio_only_after_a_verified_upload(fake, tmp_path, cfg):
    m = make_meeting(tmp_path)
    original = m.audio_path.read_bytes()
    c = {**cfg, "storage": {"delete_local_audio_after_upload": True},
         "destinations": {"drive": {"type": "rclone", "remote": "gdrive", "folder": "minuta",
                                    "binary": str(fake.binary)}}}
    fake.fail("check", 1, "ERROR : audio.opus: sizes differ")
    router = Router(c)
    res = router.deliver_meeting(m)
    assert res["drive"]["status"] == "failed" and router.apply_delete_policy(m) is False and m.audio_path
    fake.monkeypatch.delenv("FAKE_RCLONE_FAIL")
    assert router.retry(m)["drive:meeting"]["status"] == "ok"
    assert m.audio_path is None  # retry applied the policy
    assert router.fetch_audio(m, m.dir / "again").read_bytes() == original


def test_rclone_is_a_registered_destination():
    assert destinations.registry()["rclone"] is RcloneDestination
    assert "rclone" in RcloneDestination.describe()


@posix_only
def test_cli_add_and_test_without_questions(fake, cfg, capsys):
    code = cli.main(["destinations", "add", "drive", "--type", "rclone", "--option", "remote=gdrive",
                     "--option", "folder=Reuniones", "--option", f"binary={fake.binary}"])
    out = capsys.readouterr().out
    assert code == 0, out
    saved = config_mod.load()["destinations"]["drive"]
    assert saved["remote"] == "gdrive" and saved["folder"] == "Reuniones"
    assert cli.main(["destinations", "test", "drive"]) == 0
    code = cli.main(["destinations", "add", "evil", "--type", "rclone", "--option", "remote=--config=/etc/x",
                     "--option", f"binary={fake.binary}"])
    assert code == 1 and "evil" not in config_mod.load()["destinations"]


# --- interactive setup ---------------------------------------------------------------------
class Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def _next(self, message, default):
        self.asked.append(message)
        a = self.answers.pop(0)
        return default if a is None else a

    def select(self, message, choices, default):
        return self._next(message, default)

    def text(self, message, default=""):
        return self._next(message, default)


@posix_only
def test_setup_picks_an_existing_remote_and_a_folder(fake):
    opts = RcloneDestination.configure("drive", {"type": "rclone", "binary": str(fake.binary)},
                                       Answers("gdrive", "../bad", "Reuniones/2026"))
    assert opts["remote"] == "gdrive" and opts["folder"] == "Reuniones/2026"


@posix_only
def test_setup_creates_a_drive_remote_with_rclone_itself(fake, capsys):
    runs = []

    def runner(argv, **kw):
        if "create" in argv:
            runs.append((argv, kw))
            fake.remotes({"gdrive": fake.cloud, "work": fake.cloud})  # what rclone config create would do
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.run(argv, **kw)

    opts = RcloneDestination.configure("drive", {"type": "rclone", "binary": str(fake.binary)},
                                       Answers(NEW_DRIVE, "work", None, None), runner=runner)
    assert runs[0][0][1:] == ["config", "create", "work", "drive", "scope=drive.file"]
    assert "capture_output" not in runs[0][1] and runs[0][1]["shell"] is False  # the user's terminal
    assert opts["remote"] == "work" and opts["folder"] == "minuta"


def test_setup_without_rclone_explains_and_stops(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert RcloneDestination.configure("drive", {"type": "rclone"}, Answers()) is None
    assert "rclone is not installed" in capsys.readouterr().out


def test_setup_asks_nothing_when_the_remote_is_given():
    opts = {"type": "rclone", "remote": "gdrive"}
    assert RcloneDestination.configure("drive", opts, Answers()) == opts


@posix_only
def test_cli_add_in_a_terminal_runs_the_assistant(fake, cfg, capsys, monkeypatch):
    from minuta import setup

    monkeypatch.setattr(cli, "interactive", lambda args: True)
    monkeypatch.setattr(setup, "Prompter", lambda interactive: Answers("gdrive", "Meetings"))
    code = cli.main(["destinations", "add", "drive", "--type", "rclone", "--option", f"binary={fake.binary}"])
    assert code == 0, capsys.readouterr().out
    saved = config_mod.load()["destinations"]["drive"]
    assert saved["remote"] == "gdrive" and saved["folder"] == "Meetings"
