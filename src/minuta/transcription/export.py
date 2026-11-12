"""transcript.md and transcript.vtt from transcript.json."""

from __future__ import annotations

import json
from pathlib import Path


def mmss(sec: float) -> str:
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def vtt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def markdown(doc: dict) -> str:
    lines = [f"# {doc['title']}", ""]
    if doc.get("started_at"):
        lines.append(f"- Started: {doc['started_at']}")
    lines.append(f"- Duration: {round((doc.get('duration_seconds') or 0) / 60)} min")
    if doc.get("url"):
        lines.append(f"- Meet: {doc['url']}")
    lines.append("")
    body = turns_markdown(doc["turns"])
    return "\n".join(lines) + (f"\n{body}\n" if body else "")


def turns_markdown(turns: list[dict]) -> str:
    """The transcript body, one paragraph per turn; obsidian puts the same text in its notes."""
    return "\n\n".join(f"**[{mmss(t['start'])}] {t['speaker']}:** {t['text']}" for t in turns)


def vtt(doc: dict) -> str:
    out = ["WEBVTT", ""]
    for t in doc["turns"]:
        speaker = t["speaker"].replace(">", "")
        out += [f"{vtt_ts(t['start'])} --> {vtt_ts(t['end'])}", f"<v {speaker}>{t['text']}", ""]
    return "\n".join(out)


def write_all(folder: Path, doc: dict) -> dict[str, Path]:
    files = {
        "json": folder / "transcript.json",
        "md": folder / "transcript.md",
        "vtt": folder / "transcript.vtt",
    }
    files["json"].write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    files["md"].write_text(markdown(doc), encoding="utf-8")
    files["vtt"].write_text(vtt(doc), encoding="utf-8")
    return files
