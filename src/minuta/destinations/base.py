"""The interface every destination implements, and helpers adapters share.

A destination receives finished meetings (audio, metadata) and, later, their transcripts. The
router calls it; adapters never call each other and never touch `meeting.json` themselves.

Rules for adapters:

- `check()` validates options and credentials without side effects (no uploads, no writes).
- `send_meeting()` must be idempotent. The router passes the receipt of a previous successful or
  partial attempt (`previous`); use its `remote_id` to update instead of creating a duplicate.
  `send_transcript()` defaults to `send_meeting()` with only the transcript (`parts.event` says which).
- Raise `DestinationError` (or any exception) to report a failure. Adapters do not retry: the router
  does, with backoff, waiting `retry_after` seconds when the remote side asked for it. Raise
  `PermanentError` when retrying cannot help (bad credentials).
- Set `Receipt.audio = True` only once the remote side really holds a copy of the audio: that is
  what allows `delete_local_audio_after_upload` to delete the local file.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from minuta.credentials import SecretStore
    from minuta.meetings import Meeting
    from minuta.setup import Prompter

PARTS = ("audio", "transcript", "metadata")
MEETING_READY, TRANSCRIPT_READY = "meeting.ready", "transcript.ready"


class DestinationError(RuntimeError):
    """A failure worth retrying (network, rate limit, remote error). `retry_after`: seconds the
    remote side asked to wait before the next attempt, if it said."""

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(DestinationError):
    """Retrying will not help until the user changes something (credentials, options)."""


@dataclass(frozen=True)
class Capabilities:
    audio: bool = False  # can store the audio file
    transcript: bool = False  # can store the transcript
    metadata: bool = False  # can store title, date, duration, participants count...
    fetch_audio: bool = False  # can give the audio back, so it can be transcribed after local deletion


@dataclass
class Receipt:
    """What the remote side gave back. Saved in meeting.json under destinations.<name>."""

    remote_id: str | None = None
    url: str | None = None
    audio: bool = False  # the remote side holds a copy of the audio
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> Receipt | None:
        if not d:
            return None
        return cls(remote_id=d.get("remote_id"), url=d.get("url"), audio=bool(d.get("audio")),
                   extra=dict(d.get("extra") or {}))


@dataclass
class CheckResult:
    ok: bool
    message: str = ""


@dataclass
class MeetingParts:
    """What the router hands to `send_meeting`, already filtered by the destination's `send` option
    and its capabilities. A part the destination should not receive is None."""

    metadata: dict | None = None
    audio: Path | None = None
    transcript: dict[str, Path] | None = None  # {"json": Path, "md": Path, "vtt": Path}
    event: str = MEETING_READY  # TRANSCRIPT_READY when only the transcript is sent, later


class Destination(ABC):
    kind: ClassVar[str]  # registry key, e.g. "local"
    capabilities: ClassVar[Capabilities] = Capabilities()
    # Option names that hold secrets: the setup stores them with SecretStore, not in config.toml.
    secret_options: ClassVar[tuple[str, ...]] = ()

    def __init__(self, name: str, options: dict, secrets: SecretStore | None = None):
        self.name = name  # the user's name for this destination (config key)
        self.options = options
        self.secrets = secrets

    @classmethod
    def describe(cls) -> str:
        return (cls.__doc__ or cls.kind).strip().splitlines()[0]

    def secret(self, option: str) -> str | None:
        """A secret option: as typed while the destination is being added, or else the stored secret
        that `<option>_secret` names."""
        if self.options.get(option):
            return str(self.options[option])
        ref = self.options.get(f"{option}_secret")
        return self.secrets.get(ref) if (ref and self.secrets is not None) else None

    def number(self, option: str, default: float, low: float, high: float) -> float:
        """A numeric option within [low, high]; ValueError with a message for the user otherwise."""
        try:
            value = float(self.options.get(option, default))
        except (TypeError, ValueError) as e:
            raise ValueError(f"{option} must be a number") from e
        if not low <= value <= high:
            raise ValueError(f"{option} must be between {low:g} and {high:g}")
        return value

    def check(self) -> CheckResult:
        return CheckResult(True)

    @classmethod
    def configure(cls, name: str, options: dict, prompter: Prompter | None = None,
                  secrets: SecretStore | None = None) -> dict | None:
        """Called once when the destination is added, before `check()`, by `minuta destinations add` and
        the setup assistant (both through `minuta.setup.add_destination`).

        `options` holds `type`, `enabled`, `send` and whatever the user gave (`--option`), secrets
        still in plain text. Ask for what is missing through `prompter` (`text`, `password`,
        `select`, `confirm`, `path`); it is None when nobody can answer, and then nothing may be asked.
        Unlike `check()`, it may create things on the remote side, such as a folder or a database.
        Return the options to save (secret options left in them are moved to the secret store by the
        caller), or None to cancel. Raise `DestinationError` or `ValueError` to refuse with a message.
        The default asks nothing."""
        return dict(options)

    @abstractmethod
    def send_meeting(self, meeting: Meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        ...

    def send_transcript(self, meeting: Meeting, transcript: dict[str, Path],
                        previous: Receipt | None = None) -> Receipt:
        return self.send_meeting(meeting, MeetingParts(transcript=transcript, event=TRANSCRIPT_READY), previous)

    def fetch_audio(self, meeting: Meeting, receipt: Receipt, dest: Path) -> Path:
        raise NotImplementedError(f"{self.kind} cannot give audio back")


# --- helpers ------------------------------------------------------------------------------
def retry_after_seconds(value: str | None) -> float | None:
    """An HTTP `Retry-After` header (seconds or a date) as seconds from now, or None."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def safe_name(text: str | None, limit: int = 80, fallback: str = "meeting") -> str:
    """A file or folder name every OS, cloud service and Obsidian accepts: letters, digits, spaces and
    `. , ( ) ' + = @ ! -`; no leading dot or dash."""
    text = re.sub(r"[^\w\s.,()'+=@!-]", " ", text or "", flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().lstrip(".-").strip()
    return text[:limit].rstrip(" .") or fallback


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def _partial(path: Path) -> Path:
    return path.with_name(f".{path.name}.partial")


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary name, so a synced folder or a reader never sees half a file."""
    tmp = _partial(path)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def copy_atomic(src: Path, dst: Path) -> None:
    tmp = _partial(dst)
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
