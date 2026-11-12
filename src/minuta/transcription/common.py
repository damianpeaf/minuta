"""Shared pieces for transcription engines.

Engines implement `transcribe_channel(x, segments, tcfg, budget=None, cost=None)` and return
segments `{"start", "end", "text"}` with absolute times inside the channel.
"""

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from minuta.audio import SR, ffmpeg_bin

# Windows of up to 10 min: at 48 kbps mp3 that is ~3.6 MB, far from API limits.
WINDOW = 600.0
PARALLEL = 4
# Silence put between speech stretches when compacting: longer than the pause `phrases` splits
# on, so two stretches never glue into one phrase. Silence is billed too, so keep it short.
GAP = 1.0

# Phrases Whisper makes up over silence or noise.
INVENTED = (
    "subtítulos realizados por la comunidad de amara.org",
    "subtítulos por la comunidad de amara.org",
    "gracias por ver el video",
    "gracias por ver",
    "suscríbete",
    "suscríbete al canal",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subtitles by the amara.org community",
)


def invented(text: str) -> bool:
    return text.lower().strip(" .!¡?¿") in INVENTED


_LOOP = re.compile(r"((?:[^\s]+\s+){1,4}?)\1{3,}", re.IGNORECASE)


def collapse_loops(text: str) -> str:
    """Whisper sometimes loops ("no, no, no, ..." a hundred times). A run of up to four words
    repeated more than three times in a row is cut to three."""
    return _LOOP.sub(lambda m: m.group(1) * 3, text + " ").strip()


def language_iso(language: str | None) -> str | None:
    """"es-419" -> "es": Whisper-style APIs only accept the two-letter code."""
    return language.split("-")[0].lower() if language else None


def glossary(tcfg: dict) -> list[str]:
    g = tcfg.get("glossary") or []
    if isinstance(g, str):
        g = g.split(",")
    return [t.strip() for t in g if t and t.strip()]


def prompt(tcfg: dict) -> str:
    """Whisper's prompt: the user's context sentence plus the glossary. A sentence in the meeting
    language nudges the model to stay in that language; a bare word list does not."""
    parts = [tcfg.get("context") or ""]
    if glossary(tcfg):
        parts.append(", ".join(glossary(tcfg)) + ".")
    return " ".join(p for p in parts if p).strip()


def to_mp3(x: np.ndarray) -> bytes:
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", "-"],
        input=x.astype(np.float32).tobytes(), capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg mp3: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stdout


_NO_MARKS = re.compile(r"[^\w']+", re.UNICODE)


def _norm(t: str) -> str:
    return _NO_MARKS.sub("", t.lower())


def with_punctuation(words: list[dict], text: str) -> list[dict]:
    """Some models give timed words without punctuation and the punctuation only in the full
    text. If the text has exactly the same words in the same order, take them from there."""
    tokens = text.split()
    if len(tokens) != len(words):
        return words
    if any(_norm(t) != _norm(w["text"]) for t, w in zip(tokens, words, strict=True)):
        return words
    return [{**w, "text": t} for w, t in zip(words, tokens, strict=True)]


def phrases(words: list[dict], pause: float = 0.7, longest: float = 25.0) -> list[dict]:
    """Join single words into phrases. Some models return one segment per word; mixed like that
    with the other channel, words of two people talking at once would interleave. A phrase ends
    at a pause, or after `longest` seconds at the next closing mark."""
    out: list[dict] = []
    for w in sorted(words, key=lambda w: w["start"]):
        f = out[-1] if out else None
        closed = f is not None and f["end"] - f["start"] >= longest and f["text"].rstrip()[-1:] in ".?!,"
        if f and w["start"] - f["end"] <= pause and not closed:
            f["text"] += " " + w["text"]
            f["end"] = max(f["end"], w["end"])
        else:
            out.append(dict(w))
    return out


def are_words(segs: list[dict]) -> bool:
    """Did the model return single words instead of phrases?"""
    return len(segs) >= 3 and sum(len(s["text"].split()) for s in segs) / len(segs) <= 1.5


def batches(segments, maximum: float = WINDOW, gap: float = GAP) -> list[list[tuple[float, float]]]:
    """Group segments into batches whose compacted audio (speech only, `gap` between) lasts up to
    `maximum` seconds."""
    out: list[list[tuple[float, float]]] = []
    length = 0.0
    for a, b in segments:
        d = b - a
        if out and length + gap + d <= maximum:
            out[-1].append((a, b))
            length += gap + d
        else:
            out.append([(a, b)])
            length = d
    return out


def compact_length(batch, gap: float = GAP) -> float:
    return sum(b - a for a, b in batch) + gap * (len(batch) - 1)


def compact(x: np.ndarray, batch, gap: float = GAP):
    """The batch's audio with only the speech, and a function mapping a time in the compacted
    audio back to the real time in the channel."""
    silence = np.zeros(int(gap * SR), dtype=np.float32)
    pieces, mapping, t = [], [], 0.0
    for i, (a, b) in enumerate(batch):
        if i:
            pieces.append(silence)
            t += gap
        piece = x[int(a * SR): int(b * SR)]
        pieces.append(piece)
        mapping.append((t, a, b))
        t += piece.size / SR

    def real(tc: float) -> float:
        chosen = mapping[0]
        for m in mapping:
            if m[0] <= tc:
                chosen = m
        start_c, a, b = chosen
        return min(b, a + max(0.0, tc - start_c))

    return np.concatenate(pieces), real


def by_windows(x, segments, request, maximum: float = WINDOW, budget=None, cost=None,
               parallel: int = PARALLEL, progress=None) -> list[dict]:
    """`request(mp3_bytes, seconds) -> [{"start","end","text"}]` relative to the audio sent.
    Only speech is sent (see `compact`). With a budget, each batch reserves `cost(seconds)` before
    it leaves and is not sent if the cap would be exceeded."""

    def one(batch):
        audio, real = compact(x, batch)
        sec = audio.size / SR
        if budget is not None and cost is not None:
            budget.reserve(cost(sec))
        out = []
        for s in request(to_mp3(audio), sec):
            if s["text"] and not invented(s["text"]):
                start, end = real(s["start"]), real(min(s["end"], sec))
                out.append({"start": start, "end": max(start, end), "text": s["text"]})
        return out

    groups = list(batches(segments, maximum))
    out: list[dict] = []
    with ThreadPoolExecutor(parallel) as ex:
        for batch, r in zip(groups, ex.map(one, groups), strict=True):
            out += r
            if progress is not None and batch:
                progress(float(batch[-1][1]))
    return out
