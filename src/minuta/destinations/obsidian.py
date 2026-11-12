"""The built-in `obsidian` destination: one Markdown note per meeting, inside a vault.

    [destinations.notes]
    type = "obsidian"
    vault = "~/Documents/Vault"
    folder = "Meetings"           # inside the vault
    tags = ["meeting"]
    send = ["metadata", "transcript"]

The note gets YAML frontmatter (title, date, duration, Meet link, minuta id, tags) when it is
created. Later deliveries only rewrite the block between the `minuta:transcript` markers, so
anything you write around it, or add to the frontmatter, is kept.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from minuta.destinations.base import (
    Capabilities,
    CheckResult,
    Destination,
    MeetingParts,
    PermanentError,
    Receipt,
    safe_name,
    write_atomic,
)
from minuta.meetings import parse_time
from minuta.transcription.export import turns_markdown

START, END = "<!-- minuta:transcript:start -->", "<!-- minuta:transcript:end -->"
PENDING = "_Transcript pending._"


def _one_line(text) -> str:
    return " ".join(str(text).split())


def _heading(text: str) -> str:
    """A title as Markdown text: no link, image or HTML syntax, and no extra heading level."""
    text = re.sub(r"([\[\]()!<>])", r"\\\1", _one_line(text))
    return "\\" + text if text.startswith("#") else text


def _yaml(value) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid YAML scalar


class ObsidianDestination(Destination):
    """Write each meeting as a Markdown note with frontmatter in an Obsidian vault."""

    kind = "obsidian"
    capabilities = Capabilities(transcript=True, metadata=True)

    @property
    def vault(self) -> Path | None:
        v = str(self.options.get("vault") or "").strip()
        return Path(v).expanduser() if v else None

    @property
    def folder(self) -> Path:
        assert self.vault is not None
        sub = str(self.options.get("folder", "Meetings") or "").strip().strip("/\\")
        target = (self.vault / sub).resolve() if sub else self.vault.resolve()
        if target != self.vault.resolve() and self.vault.resolve() not in target.parents:
            raise PermanentError("folder must stay inside the vault")
        return target

    @property
    def tags(self) -> list[str]:
        tags = self.options.get("tags", ["meeting"])
        if isinstance(tags, str):
            tags = tags.split(",")
        return [str(t).strip().lstrip("#") for t in tags if str(t).strip()]

    @classmethod
    def configure(cls, name: str, options: dict, prompter=None, secrets=None) -> dict:
        """In a terminal, asks for the vault and the folder inside it unless they were given."""
        from minuta.i18n import t

        opts = dict(options)
        if prompter is not None:
            if not opts.get("vault"):
                opts["vault"] = prompter.path(t("setup.obsidian.vault"))
            if "folder" not in opts:
                opts["folder"] = prompter.text(t("setup.obsidian.folder"), "Meetings")
        if opts.get("vault"):
            opts["vault"] = str(Path(str(opts["vault"])).expanduser())
        return opts

    def check(self) -> CheckResult:
        vault = self.vault
        if vault is None:
            return CheckResult(False, "vault is required (the vault's folder)")
        if not vault.is_dir():
            return CheckResult(False, f"{vault} is not a folder")
        try:
            folder = self.folder
        except PermanentError as e:
            return CheckResult(False, str(e))
        probe = folder if folder.exists() else vault
        if not os.access(probe, os.W_OK):
            return CheckResult(False, f"cannot write to {probe}")
        hint = "" if (vault / ".obsidian").is_dir() else " (no .obsidian folder: is it a vault?)"
        return CheckResult(True, f"notes go to {folder}{hint}")

    # --- note ------------------------------------------------------------------
    def _note_path(self, meeting, data: dict, previous: Receipt | None) -> Path:
        if previous and previous.remote_id and Path(previous.remote_id).is_file():
            return Path(previous.remote_id)
        when = parse_time(data.get("started_at"))
        date = when.strftime("%Y-%m-%d") if when else safe_name(meeting.id[:10])
        base = f"{date} {safe_name(data.get('title') or data.get('code') or meeting.id, fallback='Meeting')}"
        folder = self.folder
        for n in range(1, 100):
            path = folder / (f"{base}.md" if n == 1 else f"{base} ({n}).md")
            if not path.resolve().is_relative_to(folder):
                raise PermanentError(f"note name leaves the folder: {path.name!r}")
            if not path.exists() or self._owns(path, meeting.id):
                return path
        raise PermanentError(f"too many notes named {base}")

    @staticmethod
    def _owns(path: Path, meeting_id: str) -> bool:
        try:
            head = path.read_text(encoding="utf-8")[:2000]
        except OSError:
            return False
        return f"minuta_id: {_yaml(meeting_id)}" in head

    def _frontmatter(self, meeting, data: dict) -> str:
        lines = ["---", f"title: {_yaml(_one_line(data.get('title') or meeting.title))}"]
        if data.get("started_at"):
            lines.append(f"date: {_yaml(data['started_at'])}")
        if data.get("duration_seconds"):
            lines.append(f"duration_minutes: {round(float(data['duration_seconds']) / 60)}")
        if data.get("url"):
            lines.append(f"meet: {_yaml(data['url'])}")
        lines.append(f"minuta_id: {_yaml(meeting.id)}")
        if self.tags:
            lines.append("tags: [" + ", ".join(_yaml(t) for t in self.tags) + "]")
        lines.append("---")
        return "\n".join(lines)

    @staticmethod
    def _transcript_block(files: dict[str, Path] | None) -> str:
        body = PENDING
        if files and files.get("json"):
            doc = json.loads(Path(files["json"]).read_text(encoding="utf-8"))
            body = turns_markdown(doc.get("turns", [])) or "_Nothing was said._"
        return f"{START}\n## Transcript\n\n{body}\n{END}"

    def send_meeting(self, meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        files = parts.transcript
        ok = self.check()
        if not ok.ok:
            raise PermanentError(ok.message)
        data = meeting.read()
        path = self._note_path(meeting, data, previous)
        block = self._transcript_block(files)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if START in text and END in text and text.index(START) < text.index(END):
                if block.endswith(PENDING + "\n" + END) and PENDING not in text:
                    return Receipt(remote_id=str(path), url=path.as_uri())  # never replace a transcript with "pending"
                a, b = text.index(START), text.index(END) + len(END)
                text = text[:a] + block + text[b:]
            else:
                text = text.rstrip("\n") + "\n\n" + block + "\n"
        else:
            title = _heading(data.get("title") or meeting.title)
            text = f"{self._frontmatter(meeting, data)}\n\n# {title}\n\n{block}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, text)
        return Receipt(remote_id=str(path), url=path.as_uri())
