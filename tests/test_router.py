"""Router: delivery, retries, isolation between destinations and the local-audio deletion policy."""

from __future__ import annotations

import pytest

from minuta.destinations import Capabilities, Destination, DestinationError, MeetingParts, PermanentError, Receipt
from minuta.meetings import READY, MeetingStore
from minuta.router import Router


class Remote:
    """Shared fake remote storage, so tests can see what each destination received."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.failures: dict[str, int] = {}  # name -> failures left
        self.permanent: set[str] = set()
        self.objects: dict[str, dict] = {}


REMOTE = Remote()


class FakeDestination(Destination):
    kind = "fake"
    capabilities = Capabilities(audio=True, transcript=True, metadata=True, fetch_audio=True)

    def _maybe_fail(self):
        if self.name in REMOTE.permanent:
            raise PermanentError("bad credentials")
        if REMOTE.failures.get(self.name, 0) > 0:
            REMOTE.failures[self.name] -= 1
            raise DestinationError("timeout")

    def send_meeting(self, meeting, parts: MeetingParts, previous=None):
        REMOTE.calls.append((self.name, "meeting", previous))
        self._maybe_fail()
        rid = previous.remote_id if previous and previous.remote_id else f"{self.name}-{len(REMOTE.objects)}"
        REMOTE.objects.setdefault(rid, {}).update({"audio": parts.audio is not None, "meta": parts.metadata})
        return Receipt(remote_id=rid, url=f"https://example.invalid/{rid}", audio=parts.audio is not None)

    def send_transcript(self, meeting, transcript, previous=None):
        REMOTE.calls.append((self.name, "transcript", previous))
        self._maybe_fail()
        rid = previous.remote_id if previous else f"{self.name}-t"
        REMOTE.objects.setdefault(rid, {})["transcript"] = sorted(transcript)
        return Receipt(remote_id=rid, audio=bool(previous and previous.audio))

    def fetch_audio(self, meeting, receipt, dest):
        out = dest.with_suffix(".opus")
        out.write_bytes(b"fetched-audio")
        return out


class MetaOnly(FakeDestination):
    kind = "metaonly"
    capabilities = Capabilities(metadata=True)


def factory(name, opts, secrets=None):
    return {"fake": FakeDestination, "metaonly": MetaOnly}[opts["type"]](name, opts, secrets)


@pytest.fixture(autouse=True)
def fresh_remote():
    global REMOTE
    REMOTE.__init__()
    yield


def make_cfg(cfg, dests, delete=False, mode="on-demand"):
    cfg = {**cfg, "destinations": dests, "storage": {"delete_local_audio_after_upload": delete},
           "transcription": {**cfg["transcription"], "mode": mode}}
    return cfg


@pytest.fixture
def meeting(cfg, tmp_path):
    store = MeetingStore(tmp_path / "meetings")
    m = store.get("2026-01-01T10-00-00-000Z_abc-defg-hij")
    m.update(status=READY, title="Weekly", started_at="2026-01-01T10:00:00Z", duration_seconds=60)
    (m.dir / "audio.opus").write_bytes(b"audio")
    return m


def router(cfg, sleeps=None):
    return Router(cfg, factory=factory, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_delivers_to_every_enabled_destination_and_records_receipts(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}, "b": {"type": "fake", "enabled": False}})
    res = router(c).deliver_meeting(meeting)
    assert set(res) == {"a"}
    st = meeting.read()["destinations"]["a"]["meeting"]
    assert st["status"] == "ok" and st["attempts"] == 1
    assert st["receipt"]["remote_id"] == "a-0" and st["receipt"]["audio"] is True


def test_failure_of_one_destination_does_not_stop_the_others(cfg, meeting):
    c = make_cfg(cfg, {"bad": {"type": "fake"}, "good": {"type": "fake"}})
    REMOTE.permanent.add("bad")
    res = router(c).deliver_meeting(meeting)
    assert res["bad"]["status"] == "failed" and res["bad"]["permanent"] is True
    assert res["good"]["status"] == "ok"


def test_retries_with_exponential_backoff_then_succeeds(cfg, meeting, monkeypatch):
    monkeypatch.setattr("minuta.router.BACKOFF", 1.5)
    c = make_cfg(cfg, {"a": {"type": "fake"}})
    c["delivery"] = {"max_attempts": 3}
    REMOTE.failures["a"] = 2
    sleeps: list[float] = []
    res = router(c, sleeps).deliver_meeting(meeting)
    assert res["a"]["status"] == "ok" and res["a"]["attempts"] == 3
    assert sleeps == [1.5, 3.0]


def test_gives_up_after_max_attempts_and_keeps_the_error(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}})
    REMOTE.failures["a"] = 10
    res = router(c).deliver_meeting(meeting)
    assert res["a"]["status"] == "failed" and res["a"]["attempts"] == 3
    assert "timeout" in res["a"]["error"]


def test_permanent_errors_are_not_retried(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}})
    REMOTE.permanent.add("a")
    sleeps: list[float] = []
    router(c, sleeps).deliver_meeting(meeting)
    assert sleeps == [] and len(REMOTE.calls) == 1


def test_delivered_stage_is_not_sent_twice(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}})
    r = router(c)
    r.deliver_meeting(meeting)
    r.deliver_meeting(meeting)
    r.retry(meeting)
    assert [c for c in REMOTE.calls if c[1] == "meeting"] == [("a", "meeting", None)]


def test_retry_resends_only_the_failed_ones_and_passes_previous_receipt(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}, "b": {"type": "fake"}})
    REMOTE.failures["b"] = 3
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.failed(meeting) == [("b", "meeting")]
    REMOTE.calls.clear()
    out = r.retry(meeting)
    assert out["b:meeting"]["status"] == "ok"
    assert [x[0] for x in REMOTE.calls if x[1] == "meeting"] == ["b"]
    assert meeting.read()["destinations"]["b"]["meeting"]["attempts"] == 4
    assert r.failed(meeting) == []


def test_transcript_goes_with_previous_receipt(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}})
    r = router(c)
    r.deliver_meeting(meeting)
    (meeting.dir / "transcript.json").write_text("{}")
    (meeting.dir / "transcript.md").write_text("# x")
    r.deliver_transcript(meeting)
    name, stage, previous = REMOTE.calls[-1]
    assert stage == "transcript" and previous.remote_id == "a-0"
    assert REMOTE.objects["a-0"]["transcript"] == ["json", "md"]


def test_send_option_and_capabilities_filter_parts(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake", "send": ["metadata"]}, "m": {"type": "metaonly"}})
    router(c).deliver_meeting(meeting)
    assert REMOTE.objects["a-0"]["audio"] is False and REMOTE.objects["a-0"]["meta"]["title"] == "Weekly"
    assert REMOTE.objects["m-1"]["audio"] is False


def test_destination_that_wants_nothing_is_skipped(cfg, meeting):
    c = make_cfg(cfg, {"t": {"type": "fake", "send": ["transcript"]}})
    res = router(c).deliver_meeting(meeting)
    assert res["t"]["status"] == "skipped" and REMOTE.calls == []


def test_unknown_destination_type_is_recorded_not_raised(cfg, meeting):
    c = make_cfg(cfg, {"x": {"type": "nope"}})
    res = Router(c).deliver_meeting(meeting)
    assert res["x"]["status"] == "failed" and "unknown destination type" in res["x"]["error"]


# --- deleting local audio ---------------------------------------------------------
def test_policy_off_never_deletes(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}}, delete=False)
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.apply_delete_policy(meeting) is False and meeting.audio_path is not None


def test_deletes_only_when_every_audio_destination_confirmed(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}, "b": {"type": "fake"}}, delete=True)
    REMOTE.failures["b"] = 3
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.apply_delete_policy(meeting) is False and meeting.audio_path is not None
    r.retry(meeting)
    assert meeting.audio_path is None
    assert meeting.read()["audio"]["deleted"] is True


def test_destinations_without_audio_do_not_count(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}, "m": {"type": "metaonly"}}, delete=True)
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.apply_delete_policy(meeting) is True


def test_no_audio_destination_means_no_deletion(cfg, meeting):
    c = make_cfg(cfg, {"m": {"type": "metaonly"}}, delete=True)
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.apply_delete_policy(meeting) is False


def test_receipt_without_audio_flag_blocks_deletion(cfg, meeting):
    c = make_cfg(cfg, {"local": {"type": "local", "send": ["audio", "transcript", "metadata"]}}, delete=True)
    r = Router(c)  # real local destination without copy_to: the audio only exists here
    r.deliver_meeting(meeting)
    assert meeting.read()["destinations"]["local"]["meeting"]["status"] == "ok"
    assert r.apply_delete_policy(meeting) is False and meeting.audio_path is not None


def test_disabled_destination_does_not_block_deletion(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}, "off": {"type": "fake", "enabled": False}}, delete=True)
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.apply_delete_policy(meeting) is True


def test_suspicious_recording_keeps_its_audio(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}}, delete=True)
    meeting.update(audio_check={"warning": "incomplete recording"})
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.apply_delete_policy(meeting) is False


def test_auto_mode_waits_for_the_transcript_before_deleting(cfg, meeting):
    c = make_cfg(cfg, {"a": {"type": "fake"}}, delete=True, mode="auto")
    r = router(c)
    r.deliver_meeting(meeting)
    assert r.apply_delete_policy(meeting) is False
    (meeting.dir / "transcript.json").write_text("{}")
    assert r.apply_delete_policy(meeting) is True


def test_fetch_audio_from_a_destination_after_deletion(cfg, meeting, tmp_path):
    c = make_cfg(cfg, {"a": {"type": "fake"}}, delete=True)
    r = router(c)
    r.deliver_meeting(meeting)
    r.apply_delete_policy(meeting)
    got = r.fetch_audio(meeting, tmp_path / "fetched")
    assert got.read_bytes() == b"fetched-audio"
