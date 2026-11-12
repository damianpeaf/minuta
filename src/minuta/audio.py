"""Audio files: join the browser's chunks, convert, split channels, find speech.

The browser records one stereo file: left channel = you, right channel = everyone else.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

SR = 16000
SIGNAL = 0.0001  # peak above this: the channel has sound at all


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not installed or not on PATH")
    return exe


def ffmpeg(*args: str) -> None:
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr.strip()[-500:]}")


def join_chunks(chunks_dir: Path, out: Path) -> Path:
    """MediaRecorder chunks concatenated in order form a valid webm file."""
    chunks = sorted(chunks_dir.glob("*.webm")) if chunks_dir.exists() else []
    if not chunks:
        raise RuntimeError("the meeting has no audio")
    with open(out, "wb") as f:
        for c in chunks:
            f.write(c.read_bytes())
    if out.stat().st_size == 0:
        raise RuntimeError("the meeting has no audio")
    return out


def build_audio(meeting_dir: Path, fmt: str = "opus") -> Path:
    """Chunks to one file. `opus` keeps the recorded stream as is (no re-encoding) in an Ogg
    container; `mp3` re-encodes keeping both channels. The chunks are removed once it worked."""
    chunks = meeting_dir / "chunks"
    raw = meeting_dir / "recording.webm"
    if chunks.exists() and any(chunks.glob("*.webm")):
        join_chunks(chunks, raw)
    elif not raw.exists():
        raise RuntimeError("the meeting has no audio")
    if fmt == "mp3":
        out = meeting_dir / "audio.mp3"
        ffmpeg("-i", str(raw), "-ac", "2", "-c:a", "libmp3lame", "-b:a", "96k", str(out))
    else:
        out = meeting_dir / "audio.opus"
        try:
            ffmpeg("-i", str(raw), "-c:a", "copy", str(out))
        except RuntimeError:
            # A recording that cannot be remuxed is still playable as webm.
            out.unlink(missing_ok=True)
            out = meeting_dir / "audio.webm"
            raw.replace(out)
    raw.unlink(missing_ok=True)
    shutil.rmtree(chunks, ignore_errors=True)
    return out


def duration_seconds(audio: Path) -> float | None:
    probe = shutil.which("ffprobe")
    if probe:
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
                           capture_output=True, text=True)
        try:
            return round(float(r.stdout.strip()), 2)
        except ValueError:
            pass
    try:
        return round(channel(audio, 0).size / SR, 2)
    except Exception:  # noqa: BLE001
        return None


def channel(audio: Path, n: int) -> np.ndarray:
    """One channel as mono float32 at 16 kHz."""
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(audio),
         "-af", f"pan=mono|c0=c{n}", "-ar", str(SR), "-ac", "1", "-f", "f32le", "-"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def rms(x: np.ndarray, start: float, end: float) -> float:
    s = x[int(start * SR): int(end * SR)]
    return float(np.sqrt(np.mean(s**2))) if s.size else 0.0


def speech_segments(x: np.ndarray, threshold: float = 0.003, pause: float = 0.8, margin: float = 0.3,
                    maximum: float = 28.0) -> list[tuple[float, float]]:
    """Energy-based voice activity detection.

    Whisper skips speech that follows long silences (a muted channel is exact zero), so only the
    stretches with voice are handed to it. Nearby stretches are grouped up to `maximum` seconds so
    the model has context; longer ones are split."""
    frame = int(0.03 * SR)
    n = x.size // frame
    if n == 0:
        return []
    energy = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1))
    active = np.flatnonzero(energy > threshold)
    if active.size == 0:
        return []
    spans = []
    start = end = active[0]
    for i in active[1:]:
        if (i - end) * 0.03 > pause:
            spans.append([start * 0.03, (end + 1) * 0.03])
            start = i
        end = i
    spans.append([start * 0.03, (end + 1) * 0.03])
    # Under 0.3 s of energy is a click or the mute button flickering, not a phrase.
    spans = [s for s in spans if s[1] - s[0] >= 0.3]
    dur = x.size / SR
    groups: list[list[float]] = []
    for a, b in spans:
        a, b = max(0.0, a - margin), min(dur, b + margin)
        if groups and b - groups[-1][0] <= maximum and a - groups[-1][1] <= 3.0:
            groups[-1][1] = b
        else:
            groups.append([a, b])
    out = []
    for a, b in groups:
        while b - a > maximum:
            out.append((a, a + maximum))
            a += maximum
        out.append((a, b))
    return [(a, b) for a, b in out if b - a >= 0.4]


def in_range(segments, window: tuple[float, float] | None):
    """Clip segments to [start, end) seconds, to transcribe only part of a meeting."""
    if not window:
        return segments
    lo, hi = window
    return [(max(a, lo), min(b, hi)) for a, b in segments
            if b > lo and a < hi and min(b, hi) - max(a, lo) >= 0.4]


def profile(audio: Path) -> dict:
    """Peak and seconds of speech per channel; used to check a recording before trusting it."""
    channels = []
    for n in (0, 1):
        x = channel(audio, n)
        segs = speech_segments(x)
        channels.append({"channel": n, "peak": float(np.max(np.abs(x))) if x.size else 0.0,
                         "speech_seconds": round(sum(b - a for a, b in segs), 2), "segments": len(segs)})
    return {"channels": channels}


def check_recording(audio: Path, diagnostics: dict) -> dict:
    """Did we record the other participants when we should have?"""
    try:
        prof = profile(audio)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "warning": f"could not verify the audio: {e}"}
    expected_remote = bool(diagnostics.get("max_remote_tracks") or diagnostics.get("max_remote_outputs")
                           or diagnostics.get("max_participants", 0) > 1)
    has_remote = prof["channels"][1]["peak"] > SIGNAL
    warning = None
    if expected_remote and not has_remote:
        warning = "incomplete recording: no audio from the other participants was captured"
    elif diagnostics.get("capture_error"):
        warning = f"there was a problem capturing audio: {diagnostics['capture_error']}"
    return {"ok": warning is None, "warning": warning, "expected_remote": expected_remote, **prof}
