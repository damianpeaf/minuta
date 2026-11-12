"""Hands each finished meeting to every enabled destination.

Per destination and stage (`meeting`, then `transcript`) the outcome is recorded in
`meeting.json` under `destinations.<name>.<stage>`:

    {"status": "ok" | "failed" | "skipped", "attempts": 2, "receipt": {...},
     "error": "...", "permanent": false, "updated_at": "..."}

A stage already `ok` is never sent again (that plus the adapters' own idempotency is what makes
`minuta retry` safe). A failing destination never stops the others. This is the only place that
retries: adapters raise, and say how long to wait when the remote side asked (`retry_after`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from minuta import destinations as dest_registry
from minuta.destinations import Destination, MeetingParts, PermanentError, Receipt
from minuta.meetings import DISCARDED, Meeting, now_iso
from minuta.redact import redact

log = logging.getLogger(__name__)

MEETING, TRANSCRIPT = "meeting", "transcript"
OK, FAILED, SKIPPED = "ok", "failed", "skipped"
BACKOFF = 2.0  # seconds before the 2nd attempt, doubled for each one after
MAX_WAIT = 60.0  # a longer Retry-After ends this delivery; `minuta retry` sends it later


class Router:
    def __init__(self, cfg: dict, secrets=None, sleep: Callable[[float], None] = time.sleep,
                 factory: Callable[..., Destination] = dest_registry.create):
        self.cfg = cfg
        self.secrets = secrets
        self.sleep = sleep
        self.factory = factory
        self.max_attempts = max(1, int((cfg.get("delivery") or {}).get("max_attempts", 3)))

    # --- configuration ---------------------------------------------------------
    def enabled(self) -> list[tuple[str, dict]]:
        out = []
        for name, opts in (self.cfg.get("destinations") or {}).items():
            if isinstance(opts, dict) and opts.get("enabled", True):
                out.append((name, opts))
        return out

    def build(self, name: str, opts: dict) -> Destination:
        return self.factory(name, opts, self.secrets)

    @staticmethod
    def wants(dest: Destination, opts: dict, part: str) -> bool:
        send = opts.get("send") or ["audio", "transcript", "metadata"]
        return part in send and bool(getattr(dest.capabilities, part, False))

    # --- state helpers ---------------------------------------------------------
    @staticmethod
    def state(meeting: Meeting, name: str, stage: str) -> dict:
        return ((meeting.read().get("destinations") or {}).get(name) or {}).get(stage) or {}

    @staticmethod
    def _save(meeting: Meeting, name: str, stage: str, state: dict) -> None:
        meeting.update_path(["destinations", name, stage], {**state, "updated_at": now_iso()})

    # --- delivery --------------------------------------------------------------
    def _attempt(self, meeting: Meeting, name: str, stage: str, call: Callable[[Receipt | None], Receipt],
                 previous: Receipt | None) -> dict:
        prior = self.state(meeting, name, stage)
        attempts = int(prior.get("attempts", 0))
        error = None
        for i in range(self.max_attempts):
            attempts += 1
            try:
                receipt = call(previous)
                state = {"status": OK, "attempts": attempts, "receipt": receipt.to_dict(), "error": None}
                self._save(meeting, name, stage, state)
                log.info("%s: %s delivered to %s", meeting.id, stage, name)
                return state
            except PermanentError as e:
                error = redact(str(e)) or type(e).__name__
                state = {"status": FAILED, "attempts": attempts, "error": error, "permanent": True}
                self._save(meeting, name, stage, state)
                log.warning("%s: %s to %s failed for good: %s", meeting.id, stage, name, error)
                return state
            except Exception as e:  # noqa: BLE001 - any adapter failure is recorded, never raised
                error = redact(f"{type(e).__name__}: {e}")
                log.warning("%s: %s to %s failed (attempt %d): %s", meeting.id, stage, name, attempts, error)
                if i + 1 == self.max_attempts:
                    break
                wait = getattr(e, "retry_after", None)
                wait = BACKOFF * 2**i if wait is None else wait
                if wait > MAX_WAIT:
                    error += f" (asked to wait {wait:.0f} s)"
                    break
                self.sleep(wait)
        state = {"status": FAILED, "attempts": attempts, "error": error, "permanent": False}
        self._save(meeting, name, stage, state)
        return state

    def _pending(self, meeting: Meeting, stage: str, only: list[str] | None,
                 results: dict[str, dict]) -> Iterator[tuple[str, dict, Destination]]:
        """Enabled destinations (all, or those in `only`) that still need `stage`. Those already done
        and those that cannot even be built go straight into `results`."""
        for name, opts in self.enabled():
            if only and name not in only:
                continue
            done = self.state(meeting, name, stage)
            if done.get("status") == OK:
                results[name] = done
                continue
            try:
                dest = self.build(name, opts)
            except Exception as e:  # noqa: BLE001
                results[name] = {"status": FAILED, "error": redact(str(e)), "permanent": True}
                self._save(meeting, name, stage, results[name])
                continue
            yield name, opts, dest

    def deliver_meeting(self, meeting: Meeting, only: list[str] | None = None) -> dict[str, dict]:
        results: dict[str, dict] = {}
        if meeting.status == DISCARDED:
            return results
        for name, opts, dest in self._pending(meeting, MEETING, only, results):
            parts = MeetingParts(
                metadata=meeting.metadata() if self.wants(dest, opts, "metadata") else None,
                audio=meeting.audio_path if self.wants(dest, opts, "audio") else None,
                transcript=(meeting.transcript_paths or None) if self.wants(dest, opts, "transcript") else None,
            )
            if parts.audio is None and parts.metadata is None and parts.transcript is None:
                results[name] = {"status": SKIPPED, "attempts": 0, "error": None}
                self._save(meeting, name, MEETING, results[name])
                continue
            previous = Receipt.from_dict(self.state(meeting, name, MEETING).get("receipt"))
            results[name] = self._attempt(meeting, name, MEETING,
                                          lambda prev, d=dest, p=parts: d.send_meeting(meeting, p, prev), previous)
            if parts.transcript and results[name]["status"] == OK:
                # The transcript went along with the meeting: no separate stage needed.
                self._save(meeting, name, TRANSCRIPT, {**results[name]})
        return results

    def deliver_transcript(self, meeting: Meeting, only: list[str] | None = None) -> dict[str, dict]:
        results: dict[str, dict] = {}
        files = meeting.transcript_paths
        if not files:
            return results
        for name, opts, dest in self._pending(meeting, TRANSCRIPT, only, results):
            if not self.wants(dest, opts, "transcript"):
                continue
            previous = (Receipt.from_dict(self.state(meeting, name, TRANSCRIPT).get("receipt"))
                        or Receipt.from_dict(self.state(meeting, name, MEETING).get("receipt")))
            results[name] = self._attempt(meeting, name, TRANSCRIPT,
                                          lambda prev, d=dest: d.send_transcript(meeting, files, prev), previous)
        return results

    def failed(self, meeting: Meeting) -> list[tuple[str, str]]:
        out = []
        for name, stages in (meeting.read().get("destinations") or {}).items():
            for stage, st in (stages or {}).items():
                if isinstance(st, dict) and st.get("status") == FAILED:
                    out.append((name, stage))
        return out

    def needs_delivery(self, meeting: Meeting) -> bool:
        """Something failed, or an enabled destination never received this meeting."""
        if self.failed(meeting):
            return True
        done = meeting.read().get("destinations") or {}
        return any(MEETING not in (done.get(name) or {}) for name, _ in self.enabled())

    def retry(self, meeting: Meeting) -> dict[str, dict]:
        """Send again whatever failed (and anything enabled after the meeting was delivered)."""
        out = {f"{k}:meeting": v for k, v in self.deliver_meeting(meeting).items()}
        out.update({f"{k}:transcript": v for k, v in self.deliver_transcript(meeting).items()})
        self.apply_delete_policy(meeting)
        return out

    # --- local audio -----------------------------------------------------------
    def audio_holders(self, meeting: Meeting) -> tuple[list[str], list[str]]:
        """(destinations that should hold the audio, those that confirmed they do)."""
        expected, confirmed = [], []
        for name, opts in self.enabled():
            try:
                dest = self.build(name, opts)
            except Exception:  # noqa: BLE001
                expected.append(name)
                continue
            if not self.wants(dest, opts, "audio"):
                continue
            expected.append(name)
            st = self.state(meeting, name, MEETING)
            receipt = Receipt.from_dict(st.get("receipt"))
            if st.get("status") == OK and receipt and receipt.audio:
                confirmed.append(name)
        return expected, confirmed

    def apply_delete_policy(self, meeting: Meeting) -> bool:
        """Delete the local audio only if the policy is on and every enabled destination that
        receives audio confirmed it holds a copy. Returns True if the audio was deleted."""
        if not (self.cfg.get("storage") or {}).get("delete_local_audio_after_upload"):
            return False
        if meeting.audio_path is None:
            return False
        data = meeting.read()
        if (data.get("audio_check") or {}).get("warning"):
            return False  # a suspicious recording keeps its original
        if self.cfg["transcription"].get("mode") == "auto" and not meeting.has_transcript():
            return False
        expected, confirmed = self.audio_holders(meeting)
        if not expected or set(expected) != set(confirmed):
            return False
        meeting.delete_audio()
        log.info("%s: local audio deleted, held by %s", meeting.id, ", ".join(confirmed))
        return True

    def fetch_audio(self, meeting: Meeting, dest_path: Path) -> Path:
        """Get the audio back from a destination that holds it (after local deletion)."""
        errors = []
        for name, opts in self.enabled():
            st = self.state(meeting, name, MEETING)
            receipt = Receipt.from_dict(st.get("receipt"))
            if st.get("status") != OK or not receipt or not receipt.audio:
                continue
            try:
                dest = self.build(name, opts)
                if not dest.capabilities.fetch_audio:
                    continue
                return dest.fetch_audio(meeting, receipt, dest_path)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {redact(str(e))}")
        raise RuntimeError("no destination could give the audio back" + (f" ({'; '.join(errors)})" if errors else ""))
