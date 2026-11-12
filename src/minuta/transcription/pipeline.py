"""From a recorded meeting to transcript.json, .md and .vtt.

1. Split the stereo audio: left = you, right = everyone else.
2. Find the stretches with speech in each channel (`audio.speech_segments`). Whisper skips speech
   after long silences, so only those stretches go to the engine. Paid engines receive only the
   speech, glued with 1 s of silence, and the returned times are mapped back to the meeting.
3. Drop text made up over silence, repetition loops and prompt echoes.
4. For Spanish meetings with a fallback engine: phrases that came out in Portuguese or English
   are transcribed again with an engine that forces the language.
5. Merge into turns per speaker; what the other channel said on top does not split a turn.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from minuta.audio import SR, channel, in_range, rms, speech_segments
from minuta.meetings import Meeting
from minuta.transcription import create_engine, price_per_minute, window_seconds
from minuta.transcription.budget import Budget, estimate
from minuta.transcription.common import batches, collapse_loops, compact_length, glossary, prompt
from minuta.transcription.export import write_all
from minuta.transcription.language import FILLER, applies, off_spanish

log = logging.getLogger(__name__)
# Below this a stretch is silence (a muted channel is exact zero) and any text there is made up.
MIN_RMS = 0.002


def _norm(t: str) -> str:
    return "".join(c for c in t.lower() if c.isalnum())


def prompt_echo(text: str, tcfg: dict) -> bool:
    """With very short audio Whisper sometimes returns its prompt: the context sentence or the glossary."""
    terms = {w.lower() for w in glossary(tcfg)}
    words = [w.strip(" .,;:").lower() for w in text.split(",") if w.strip(" .,;:")]
    if terms and len(words) >= 3 and sum(w in terms for w in words) / len(words) >= 0.6:
        return True
    clean, p = _norm(text), _norm(prompt(tcfg))
    return bool(p) and len(clean) >= 25 and clean in p


def merge_turns(segs: list[dict], pause: float = 1.5, interjection: int = 3) -> list[dict]:
    """Join consecutive phrases of the same speaker. What the other channel said on top (it started
    before the turn ended) or a short interjection ("yes", "mm-hmm") does not cut the current
    speaker's turn: it goes after it."""
    turns: list[dict] = []
    for s in sorted(segs, key=lambda s: s["start"]):
        k = len(turns) - 1
        while k >= 0 and turns[k]["speaker"] != s["speaker"]:
            k -= 1
        t = turns[k] if k >= 0 else None
        between = turns[k + 1:]
        on_top = t is not None and all(o["start"] < t["end"] or len(o["text"].split()) <= interjection
                                       for o in between)
        if t and on_top and s["start"] - t["end"] <= pause:
            t["text"] += " " + s["text"]
            t["end"] = max(t["end"], s["end"])
        else:
            turns.append({k: s[k] for k in ("start", "end", "speaker", "text")})
    return turns


def fix_language(x: np.ndarray, segs: list[dict], fallback, tcfg: dict, budget=None, cost=None) -> list[dict]:
    """Re-run off-language phrases with `fallback`. Spanish result: replace. Nothing or only
    filler: it was noise, drop it. Another language again: keep it, maybe it was said that way.
    Returns the changes; a dropped phrase has empty text."""
    flagged = [s for s in segs if off_spanish(s["text"])]
    if not flagged:
        return []
    dur = x.size / SR
    changes = []
    for s in flagged:
        window = [(max(0.0, s["start"] - 0.3), min(dur, s["end"] + 0.3))]
        new = fallback.transcribe_channel(x, window, tcfg, budget=budget, cost=cost)
        text = collapse_loops(" ".join(n["text"].strip() for n in new if n["text"].strip()))
        words = {w.strip(" .,;:¡!¿?").lower() for w in text.split()} - {""}
        if not words or words <= FILLER:
            text = ""
        elif off_spanish(text):
            continue
        changes.append({"start": s["start"], "speaker": s.get("speaker"), "original": s["text"], "text": text})
        s["text"] = text
    segs[:] = [s for s in segs if s["text"]]
    return changes


def accepts_progress(engine) -> bool:
    try:
        return "progress" in inspect.signature(engine.transcribe_channel).parameters
    except (TypeError, ValueError):
        return False


def speech_done(segments, until: float) -> float:
    """Seconds of speech in `segments` before `until` (a time in the meeting)."""
    return sum(max(0.0, min(b, until) - a) for a, b in segments if a < until)


def transcribe_channel(x: np.ndarray, speaker: str, engine, tcfg: dict, budget=None, window=None,
                       fallback=None, changes: list | None = None, rate: float = 0.0, segments=None,
                       progress=None) -> list[dict]:
    """`progress(seconds)`: the engine reached this time of the meeting (engines that report it)."""
    segments = in_range(speech_segments(x), window) if segments is None else segments
    if not segments:
        log.info("no speech in %s's channel", speaker)
        return []

    def cost(sec: float) -> float:
        return estimate(sec, rate)

    kw = {"progress": progress} if progress is not None and accepts_progress(engine) else {}
    out = []
    for s in engine.transcribe_channel(x, segments, tcfg, budget=budget, cost=cost, **kw):
        text = collapse_loops(s["text"].strip())
        if not text or rms(x, s["start"], s["end"]) < MIN_RMS or prompt_echo(text, tcfg):
            continue
        out.append({"start": round(s["start"], 3), "end": round(s["end"], 3), "text": text, "speaker": speaker})
    if fallback is not None:
        fb_rate = getattr(fallback, "usd_per_minute", 0)
        done = fix_language(x, out, fallback, tcfg, budget, lambda sec: estimate(sec, fb_rate))
        if changes is not None:
            changes += done
    return out


def obtain_audio(meeting: Meeting, router) -> tuple[Path, bool]:
    """(audio path, fetched) — fetched audio is a temporary copy to remove afterwards."""
    if meeting.audio_path is not None:
        return meeting.audio_path, False
    if router is None:
        raise RuntimeError("the local audio was deleted and no destination is configured to fetch it")
    return router.fetch_audio(meeting, meeting.dir / "fetched"), True


def estimate_run(meeting: Meeting, cfg: dict, engine_name: str, router=None, window=None) -> dict:
    """Seconds of audio that would be sent and the estimated cost. Free for local engines."""
    rate = price_per_minute(engine_name, cfg)
    if rate == 0:
        return {"engine": engine_name, "seconds": 0.0, "usd": 0.0}
    audio, fetched = obtain_audio(meeting, router)
    try:
        maximum = window_seconds(engine_name, cfg)
        seconds = 0.0
        for n in (0, 1):
            segs = in_range(speech_segments(channel(audio, n)), window)
            seconds += sum(compact_length(b) for b in batches(segs, maximum))
    finally:
        if fetched:
            audio.unlink(missing_ok=True)
    return {"engine": engine_name, "seconds": round(float(seconds), 1), "usd": round(float(estimate(seconds, rate)), 4)}


def transcribe(meeting: Meeting, cfg: dict, engine_name: str | None = None, *, router=None, secrets=None,
               budget: Budget | None = None, window=None, engine=None, fallback=None, on_progress=None) -> dict:
    """`on_progress(step, fraction, info)`: step is "audio", "model", "download", "transcribe", "deliver" or
    "done"; fraction is 0..1 or None when unknown; info holds the step's numbers (bytes, seconds, name)."""

    def report(step: str, fraction: float | None = None, **info) -> None:
        if on_progress is not None:
            on_progress(step, fraction, info)

    t = cfg["transcription"]
    engine_name = engine_name or t.get("engine", "local")
    rate = price_per_minute(engine_name, cfg) if engine_name in ("local", "vercel") else 0.0
    meta = meeting.read()
    report("audio")
    audio, fetched = obtain_audio(meeting, router)
    meeting.update(transcript_status="running")
    try:
        x_me, x_others = channel(audio, 0), channel(audio, 1)
        seg_me, seg_others = in_range(speech_segments(x_me), window), in_range(speech_segments(x_others), window)
        if engine is None:
            if engine_name != "local":  # the local engine reports its own download and loading
                report("model", None, engine=engine_name, model=(t.get(engine_name) or {}).get("model"))
            engine = create_engine(engine_name, cfg, secrets, on_progress=report)
        fb_name = t.get("fallback_engine")
        if fallback is None and fb_name and fb_name != engine_name and applies(t.get("language")):
            fallback = create_engine(fb_name, cfg, secrets)
        total = speech_done(seg_me, float("inf")) + speech_done(seg_others, float("inf"))
        report("transcribe", 0.0, seconds=0.0, total=total)

        def channel_progress(base: float, segments):
            def progress(until: float) -> None:
                done = base + speech_done(segments, until)
                report("transcribe", min(1.0, done / total) if total else None, seconds=done, total=total)
            return progress

        changes: list[dict] = []
        speakers = cfg["speakers"]
        me = transcribe_channel(x_me, speakers["me"], engine, t, budget, window, fallback, changes, rate,
                                segments=seg_me, progress=channel_progress(0.0, seg_me))
        done_me = speech_done(seg_me, float("inf"))
        report("transcribe", done_me / total if total else None, seconds=done_me, total=total)
        others = transcribe_channel(x_others, speakers["others"], engine, t, budget, window, fallback, changes, rate,
                                    segments=seg_others, progress=channel_progress(done_me, seg_others))
        report("transcribe", 1.0, seconds=total, total=total)
        doc = {
            "id": meeting.id,
            "title": meeting.title,
            "code": meta.get("code"),
            "url": meta.get("url"),
            "started_at": meta.get("started_at"),
            "duration_seconds": round(x_me.size / SR, 2),
            "engine": {"name": engine_name, "model": getattr(engine, "model", None)},
            "language": t.get("language") or None,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "language_fixes": sorted(changes, key=lambda c: c["start"]),
            "turns": merge_turns(me + others),
        }
        if window:
            doc["range_seconds"] = list(window)
        if budget is not None:
            doc["cost_usd"] = round(budget.spent, 5)
        write_all(meeting.dir, doc)
        meeting.update(transcript_status="done", transcript={
            "engine": engine_name, "model": doc["engine"]["model"], "generated_at": doc["generated_at"],
            "turns": len(doc["turns"]), "cost_usd": doc.get("cost_usd", 0.0)})
    except Exception as e:
        meeting.update(transcript_status="error", transcript_error=str(e)[:500])
        raise
    finally:
        if fetched:
            audio.unlink(missing_ok=True)
    if router is not None:
        report("deliver")
        router.deliver_transcript(meeting)
        router.apply_delete_policy(meeting)
    report("done", 1.0)
    return doc
