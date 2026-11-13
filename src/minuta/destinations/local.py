"""The built-in `local` destination."""

from __future__ import annotations

import json
import os
from pathlib import Path

from minuta.destinations.base import (
    Capabilities,
    CheckResult,
    Destination,
    MeetingParts,
    PermanentError,
    Receipt,
    copy_atomic,
    is_private,
    make_dirs,
    meeting_folder_name,
    write_atomic,
)


class LocalDestination(Destination):
    """Keep meetings in the data folder, optionally copying them to another folder.

    Without `copy_to` the meeting stays where minuta recorded it and nothing is copied. With
    `copy_to`, each meeting is copied to `<copy_to>/<YYYY-MM-DD HH-MM> <title>/` (local time): point it
    at a folder synced by Drive, Dropbox, iCloud or an Obsidian vault. The copies are private to the
    current user (folders 0700, files 0600) unless `private = false`.
    """

    kind = "local"
    capabilities = Capabilities(audio=True, transcript=True, metadata=True, fetch_audio=True)

    @property
    def copy_to(self) -> Path | None:
        c = (self.options.get("copy_to") or "").strip()
        return Path(c).expanduser() if c else None

    @classmethod
    def form(cls, name: str, known: dict, secrets=None, **kw) -> list:
        """Offers a folder to copy meetings to unless `copy_to` was given."""
        from minuta.forms import Field

        if "copy_to" in known:
            return []
        if "_copy" not in known:
            return [Field("_copy", "bool", "setup.copy", default=False)]
        return [Field("copy_to", "path", "setup.copy_to", required=True)] if known["_copy"] else []

    @classmethod
    def configure(cls, name: str, options: dict, prompter=None, secrets=None) -> dict | None:
        opts = cls.ask(name, options, prompter, secrets)
        if opts is None:
            return None
        opts.pop("_copy", None)
        if opts.get("copy_to"):
            opts["copy_to"] = str(Path(str(opts["copy_to"])).expanduser())
        return opts

    def check(self) -> CheckResult:
        from minuta.i18n import t

        target = self.copy_to
        if target is None:
            return CheckResult(True, t("local.in_place"))
        if target.exists() and not target.is_dir():
            return CheckResult(False, t("local.not_a_folder", path=target))
        probe = target if target.exists() else next((p for p in target.parents if p.exists()), None)
        if probe is None or not os.access(probe, os.W_OK):
            return CheckResult(False, t("local.cannot_write", path=target))
        return CheckResult(True, t("local.copies_to", path=target))

    def _folder(self, meeting, previous: Receipt | None) -> Path:
        """`2026-11-12 16-50 Title` in local time; a folder another meeting already took gets a number."""
        assert self.copy_to is not None
        if previous and previous.remote_id:
            return Path(previous.remote_id)
        d = meeting.read()
        base = meeting_folder_name(meeting.id, d.get("title"), d.get("started_at"), d.get("code"))
        for n in range(1, 100):
            folder = self.copy_to / (base if n == 1 else f"{base} ({n})")
            if not folder.exists() or not any(folder.iterdir()) or self._owner(folder) == meeting.id:
                return folder
        return self.copy_to / f"{base} {meeting.id}"

    @staticmethod
    def _owner(folder: Path) -> str | None:
        try:
            return json.loads((folder / "meeting.json").read_text(encoding="utf-8")).get("id")
        except (OSError, ValueError):
            return None

    def send_meeting(self, meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        if self.copy_to is None:
            return Receipt(remote_id=str(meeting.dir), extra={"in_place": True})
        ok = self.check()
        if not ok.ok:
            raise PermanentError(ok.message)
        private = is_private(self.options)
        folder = make_dirs(self._folder(meeting, previous), private)
        copied_audio = False
        if parts.audio is not None:
            copy_atomic(parts.audio, folder / parts.audio.name, private)
            copied_audio = True
        if parts.metadata is not None:
            write_atomic(folder / "meeting.json", json.dumps(parts.metadata, ensure_ascii=False, indent=2), private)
        for path in (parts.transcript or {}).values():
            copy_atomic(path, folder / path.name, private)
        return Receipt(remote_id=str(folder), url=folder.as_uri(),
                       audio=copied_audio or bool(previous and previous.audio))

    def fetch_audio(self, meeting, receipt: Receipt, dest: Path) -> Path:
        folder = Path(receipt.remote_id or "")
        for name in ("audio.opus", "audio.mp3", "audio.webm"):
            src = folder / name
            if src.exists():
                target = dest.with_suffix(src.suffix) if dest.suffix == "" else dest
                copy_atomic(src, target)
                return target
        raise FileNotFoundError(f"no audio in {folder}")
