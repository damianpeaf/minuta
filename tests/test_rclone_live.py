"""The rclone destination with the real rclone program against a local folder, SFTP and S3.

    uv run pytest -m rclone_live

SFTP and S3 run in throwaway Docker containers on 127.0.0.1 (see tests/rclone_backends.py). Skipped
when rclone or Docker is missing. Google Drive, Dropbox and OneDrive need real accounts and are not
tested here; they go through the very same code, only the rclone remote differs.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from minuta.destinations import DestinationError, MeetingParts, PermanentError
from minuta.destinations.rclone import RcloneDestination, meeting_folder_name
from minuta.meetings import MeetingStore
from minuta.router import Router

from .rclone_backends import Backends, docker_ok, find_rclone

pytestmark = pytest.mark.rclone_live

RCLONE = find_rclone()
needs_rclone = pytest.mark.skipif(RCLONE is None, reason="rclone not found (see tests/rclone_backends.py)")


@pytest.fixture(scope="module")
def backends(tmp_path_factory):
    if RCLONE is None:
        pytest.skip("rclone not found")
    b = Backends(RCLONE, tmp_path_factory.mktemp("rclone"))
    b.local()
    if docker_ok():
        b.sftp()
        b.s3()
    try:
        yield b
    finally:
        b.close()


@pytest.fixture(params=["loc", "sftp", "s3"])
def remote(request, backends):
    if request.param not in backends.sections:
        pytest.skip("docker not available")
    return request.param


def make_meeting(tmp_path, n=0, size=3 * 1024 * 1024 + 17):
    mid = f"2026-03-0{n + 1}T10-30-00-000Z_abc-defg-hij"
    m = MeetingStore(tmp_path / "meetings").get(mid)
    m.update(status="ready", title=f"Weekly sync #{n}: plans/ideas", started_at=f"2026-03-0{n + 1}T10:30:00+00:00",
             duration_seconds=1800, audio={"file": "audio.opus", "bytes": size})
    (m.dir / "audio.opus").write_bytes(os.urandom(size))
    write_transcript(m, "first version")
    return m


def write_transcript(m, text):
    (m.dir / "transcript.md").write_text(f"# Transcript\n\n{text}\n", encoding="utf-8")
    (m.dir / "transcript.json").write_text(json.dumps({"segments": [{"text": text}]}), encoding="utf-8")
    (m.dir / "transcript.vtt").write_text(f"WEBVTT\n\n00:00.000 --> 00:01.000\n{text}\n", encoding="utf-8")


def cfg_for(cfg, backends, remote, **extra_dests):
    dests = {"cloud": {"type": "rclone", "remote": remote, "folder": backends.base[remote],
                       "config": str(backends.config), "binary": RCLONE}, **extra_dests}
    return {**cfg, "destinations": dests, "storage": {"delete_local_audio_after_upload": True},
            "transcription": {**cfg["transcription"], "mode": "on-demand"}}


def remote_files(backends, path) -> dict[str, dict]:
    out = backends.run("lsjson", "--hash", "--files-only", path).stdout
    return {e["Name"]: e for e in json.loads(out)}


def assert_same(backends, path, local_dir, names):
    """Every file on the remote has the local size and bytes, and the local md5 when the backend has one."""
    listed = remote_files(backends, path)
    for name in names:
        local = (local_dir / name).read_bytes()
        assert listed[name]["Size"] == len(local), name
        md5 = (listed[name].get("Hashes") or {}).get("md5")
        if md5:
            assert md5 == hashlib.md5(local).hexdigest(), name  # noqa: S324
        got = backends.run("cat", f"{path}/{name}").stdout.encode() if name.endswith((".md", ".json", ".vtt")) \
            else None
        if got is not None:
            assert got == local, name


@needs_rclone
def test_check_lists_without_writing(backends, remote):
    d = RcloneDestination("cloud", {"type": "rclone", "remote": remote, "folder": backends.base[remote] + "/fresh",
                                    "config": str(backends.config), "binary": RCLONE})
    r = d.check()
    assert r.ok, r.message
    res = backends.run("lsjson", f"{remote}:{backends.base[remote]}/fresh", check=False)
    assert res.returncode != 0 or json.loads(res.stdout) == []  # nothing was created


@needs_rclone
def test_full_cycle_through_the_router(backends, remote, cfg, tmp_path):
    m = make_meeting(tmp_path)
    audio = m.audio_path.read_bytes()
    router = Router(cfg_for(cfg, backends, remote))

    res = router.deliver_meeting(m)
    st = res["cloud"]
    assert st["status"] == "ok", st
    receipt = st["receipt"]
    name = meeting_folder_name(m.id, m.title, m.read()["started_at"])
    path = f"{remote}:{backends.base[remote]}/{name}"
    assert receipt["remote_id"] == path and receipt["audio"] is True
    assert receipt["extra"]["verified"] == ("size" if remote == "sftp" else "hash")
    assert_same(backends, path, m.dir, ["audio.opus", "transcript.md", "transcript.json", "transcript.vtt"])
    meta = json.loads(backends.run("cat", f"{path}/meeting.json").stdout)
    assert meta["id"] == m.id and "destinations" not in meta and "status" not in meta

    # Same meeting again with the stored receipt: same folder, nothing duplicated.
    d = router.build("cloud", router.cfg["destinations"]["cloud"])
    from minuta.destinations import Receipt

    prev = Receipt.from_dict(receipt)
    again = d.send_meeting(m, MeetingParts(audio=m.audio_path), previous=prev)
    assert again.remote_id == path
    folders = [e["Name"] for e in json.loads(backends.run("lsjson", f"{remote}:{backends.base[remote]}").stdout)]
    assert [f for f in folders if f == name or m.id in f] == [name]

    # A new transcript replaces the old one in place.
    write_transcript(m, "second, corrected version with more words")
    upd = d.send_transcript(m, m.transcript_paths, previous=again)
    assert upd.remote_id == path and upd.audio is True
    assert_same(backends, path, m.dir, ["transcript.md", "transcript.json", "transcript.vtt"])

    # The policy deletes the local audio only now, and the audio comes back byte for byte.
    assert router.apply_delete_policy(m) is True and m.audio_path is None
    back = router.fetch_audio(m, tmp_path / "restored" / "audio")
    assert back.read_bytes() == audio and back.suffix == ".opus"


@needs_rclone
def test_audio_stays_local_while_any_destination_lacks_it(backends, cfg, tmp_path):
    if "sftpbad" not in backends.sections:
        pytest.skip("docker not available")
    m = make_meeting(tmp_path, n=1)
    bad = {"type": "rclone", "remote": "sftpbad", "folder": "upload/minuta", "config": str(backends.config),
           "binary": RCLONE}
    c = cfg_for(cfg, backends, "loc", broken=bad)
    c["delivery"] = {"max_attempts": 1}
    router = Router(c)
    res = router.deliver_meeting(m)
    assert res["cloud"]["status"] == "ok"
    assert res["broken"]["status"] == "failed" and res["broken"]["permanent"] is True
    assert router.apply_delete_policy(m) is False and m.audio_path is not None
    # Automatic transcription and no transcript yet: also keep it.
    c2 = cfg_for(cfg, backends, "loc")
    c2["transcription"]["mode"] = "auto"
    m2 = make_meeting(tmp_path / "other", n=2)
    for f in ("transcript.md", "transcript.json", "transcript.vtt"):
        (m2.dir / f).unlink()
    r2 = Router(c2)
    assert r2.deliver_meeting(m2)["cloud"]["status"] == "ok"
    assert r2.apply_delete_policy(m2) is False and m2.audio_path is not None


@needs_rclone
@pytest.mark.parametrize("remote_name", ["sftpbad", "s3bad"])
def test_wrong_credentials_are_permanent(backends, remote_name, tmp_path):
    if remote_name not in backends.sections:
        pytest.skip("docker not available")
    base = "upload/minuta" if remote_name == "sftpbad" else "minuta-test/minuta"
    d = RcloneDestination("x", {"type": "rclone", "remote": remote_name, "folder": base,
                                "config": str(backends.config), "binary": RCLONE})
    assert not d.check().ok
    m = make_meeting(tmp_path, n=3, size=1000)
    with pytest.raises(PermanentError):
        d.send_meeting(m, MeetingParts(audio=m.audio_path))


@needs_rclone
def test_unknown_remote_and_unreachable_server(backends, tmp_path):
    d = RcloneDestination("x", {"type": "rclone", "remote": "nosuch", "config": str(backends.config),
                                "binary": RCLONE})
    r = d.check()
    assert not r.ok and "no remote named 'nosuch'" in r.message
    m = make_meeting(tmp_path, n=4, size=1000)
    with pytest.raises(PermanentError, match="no remote named"):
        d.send_meeting(m, MeetingParts(audio=m.audio_path))
    # A server that is down is worth retrying later.
    backends.sections["down"] = {"type": "sftp", "host": "127.0.0.1", "port": "1", "user": "x",
                                 "pass": backends.obscure("x")}
    backends.write()
    down = RcloneDestination("x", {"type": "rclone", "remote": "down", "folder": "f", "config": str(backends.config),
                                   "binary": RCLONE, "timeout_seconds": 60})
    with pytest.raises(DestinationError) as e:
        down.send_meeting(m, MeetingParts(audio=m.audio_path))
    assert not isinstance(e.value, PermanentError), str(e.value)
