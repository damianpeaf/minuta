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
    safe_name,
    write_atomic,
)


class LocalDestination(Destination):
    """Keep meetings in the data folder, optionally copying them to another folder.

    Without `copy_to` the meeting stays where minuta recorded it and nothing is copied. With
    `copy_to`, each meeting is copied to `<copy_to>/<id> <title>/`: point it at a folder synced by
    Drive, Dropbox, iCloud or an Obsidian vault.
    """

    kind = "local"
    capabilities = Capabilities(audio=True, transcript=True, metadata=True, fetch_audio=True)

    @property
    def copy_to(self) -> Path | None:
        c = (self.options.get("copy_to") or "").strip()
        return Path(c).expanduser() if c else None

    @classmethod
    def configure(cls, name: str, options: dict, prompter=None, secrets=None) -> dict:
        """In a terminal, offers a folder to copy meetings to unless `copy_to` was given."""
        from minuta.i18n import t

        opts = dict(options)
        if prompter is not None and "copy_to" not in opts and prompter.confirm(t("setup.copy"), False):
            opts["copy_to"] = prompter.path(t("setup.copy_to"))
        if opts.get("copy_to"):
            opts["copy_to"] = str(Path(str(opts["copy_to"])).expanduser())
        return opts

    def check(self) -> CheckResult:
        target = self.copy_to
        if target is None:
            return CheckResult(True, "meetings stay in the data folder")
        if target.exists() and not target.is_dir():
            return CheckResult(False, f"{target} exists and is not a folder")
        probe = target if target.exists() else next((p for p in target.parents if p.exists()), None)
        if probe is None or not os.access(probe, os.W_OK):
            return CheckResult(False, f"cannot write to {target}")
        return CheckResult(True, f"copies to {target}")

    def _folder(self, meeting, previous: Receipt | None) -> Path:
        assert self.copy_to is not None
        if previous and previous.remote_id:
            return Path(previous.remote_id)
        return self.copy_to / f"{meeting.id} {safe_name(meeting.title)}"

    def send_meeting(self, meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        if self.copy_to is None:
            return Receipt(remote_id=str(meeting.dir), extra={"in_place": True})
        ok = self.check()
        if not ok.ok:
            raise PermanentError(ok.message)
        folder = self._folder(meeting, previous)
        folder.mkdir(parents=True, exist_ok=True)
        copied_audio = False
        if parts.audio is not None:
            copy_atomic(parts.audio, folder / parts.audio.name)
            copied_audio = True
        if parts.metadata is not None:
            write_atomic(folder / "meeting.json", json.dumps(parts.metadata, ensure_ascii=False, indent=2))
        for path in (parts.transcript or {}).values():
            copy_atomic(path, folder / path.name)
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
