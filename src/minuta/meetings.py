"""One folder per meeting: `meetings/<id>/meeting.json`, the audio and the transcript files."""

from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from minuta.paths import private_dir

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
AUDIO_NAMES = ("audio.opus", "audio.mp3", "audio.webm")
TRANSCRIPT_FILES = ("transcript.json", "transcript.md", "transcript.vtt")
# What destinations receive as a meeting's details (never diagnostics, events or delivery state).
METADATA_KEYS = ("id", "title", "code", "url", "started_at", "ended_at", "duration_seconds", "audio", "audio_check")

# Statuses a meeting goes through.
RECORDING, QUEUED, PROCESSING, READY, ERROR, DISCARDED = (
    "recording", "queued", "processing", "ready", "error", "discarded",
)

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value) -> datetime | None:
    """An ISO 8601 time ("Z" accepted), or None. A time without a zone is taken as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def valid_id(meeting_id) -> bool:
    return isinstance(meeting_id, str) and bool(ID_RE.match(meeting_id)) and ".." not in meeting_id


class Meeting:
    def __init__(self, root: Path, meeting_id: str):
        if not valid_id(meeting_id):
            raise ValueError(f"invalid meeting id: {meeting_id!r}")
        self.id = meeting_id
        self.dir = root / meeting_id

    # --- meeting.json --------------------------------------------------------
    @property
    def meta_path(self) -> Path:
        return self.dir / "meeting.json"

    def exists(self) -> bool:
        return self.meta_path.exists()

    def read(self) -> dict:
        with _lock:
            if not self.meta_path.exists():
                return {}
            return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def mkdir(self) -> Path:
        """The meeting's folder, private to the current user."""
        return private_dir(self.dir)

    def _write(self, data: dict) -> None:
        self.mkdir()
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.meta_path)

    def update(self, **changes) -> dict:
        with _lock:
            data = self.read() or {"id": self.id, "created_at": now_iso()}
            data.update(changes)
            self._write(data)
            return data

    def update_path(self, keys: list[str], value) -> dict:
        """Set a nested key, e.g. ["destinations", "local", "meeting"]."""
        with _lock:
            data = self.read() or {"id": self.id, "created_at": now_iso()}
            node = data
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = value
            self._write(data)
            return data

    # --- convenience ----------------------------------------------------------
    def metadata(self) -> dict:
        data = self.read()
        return {k: data[k] for k in METADATA_KEYS if k in data}

    @property
    def status(self) -> str | None:
        return self.read().get("status")

    @property
    def title(self) -> str:
        d = self.read()
        return d.get("title") or d.get("code") or self.id

    @property
    def chunks_dir(self) -> Path:
        return self.dir / "chunks"

    @property
    def audio_path(self) -> Path | None:
        for name in AUDIO_NAMES:
            p = self.dir / name
            if p.exists():
                return p
        return None

    @property
    def transcript_paths(self) -> dict[str, Path]:
        return {n.split(".")[-1]: self.dir / n for n in TRANSCRIPT_FILES if (self.dir / n).exists()}

    def has_transcript(self) -> bool:
        return (self.dir / "transcript.json").exists()

    def append_event(self, event: dict) -> None:
        with _lock:
            self.mkdir()
            with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def delete_audio(self) -> None:
        with _lock:
            shutil.rmtree(self.chunks_dir, ignore_errors=True)
            for name in AUDIO_NAMES:
                (self.dir / name).unlink(missing_ok=True)
            audio = self.read().get("audio") or {}
            self.update(audio={**audio, "deleted": True, "deleted_at": now_iso()})


class MeetingLookupError(LookupError):
    """A meeting reference that names no meeting, or more than one. `key` and `kw` are for i18n.t."""

    def __init__(self, message: str, key: str, **kw):
        super().__init__(message)
        self.key, self.kw = key, kw


class MeetingStore:
    def __init__(self, root: Path):
        self.root = root

    def get(self, meeting_id: str) -> Meeting:
        return Meeting(self.root, meeting_id)

    def all(self) -> list[Meeting]:
        if not self.root.exists():
            return []
        out = [Meeting(self.root, d.name) for d in self.root.iterdir() if d.is_dir() and valid_id(d.name)]
        out = [m for m in out if m.exists()]
        # Ids start with an ISO timestamp, so sorting by id is chronological; started_at breaks ties.
        return sorted(out, key=lambda m: (m.read().get("started_at") or "", m.id))

    def resolve(self, ref: str) -> Meeting:
        """`latest`, an exact id or an unambiguous id prefix."""
        meetings = [m for m in self.all() if m.status != DISCARDED]
        if ref in ("latest", "last"):
            if not meetings:
                raise MeetingLookupError("there are no meetings yet", "lookup.none")
            return meetings[-1]
        if valid_id(ref) and (self.root / ref / "meeting.json").exists():
            return self.get(ref)
        matches = [m for m in meetings if m.id.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MeetingLookupError(f"no meeting matches {ref!r}", "lookup.no_match", ref=ref)
        raise MeetingLookupError(f"{ref!r} matches {len(matches)} meetings; use more of the id", "lookup.ambiguous",
                                 ref=ref, n=len(matches))
