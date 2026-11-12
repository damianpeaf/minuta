"""A real, short transcription with the local engine and a small model.

Synthetic voices come from ffmpeg's flite filter. Downloads the model on first run.

uv run --extra local pytest -m slow tests/test_local_transcription_real.py
"""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from minuta.meetings import MeetingStore
from minuta.transcription.pipeline import transcribe

from .conftest import write_stereo

pytestmark = pytest.mark.slow


def has_flite() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return " flite " in out


def speak(text: str, sr: int = 48000) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"flite=text='{text}'",
                        "-ar", str(sr), "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.float32) * 0.8


@pytest.mark.skipif(not has_flite(), reason="ffmpeg without flite")
def test_local_engine_transcribes_both_channels(cfg, tmp_path):
    from minuta.transcription.local import is_apple_silicon

    pytest.importorskip("mlx_whisper" if is_apple_silicon() else "faster_whisper")
    sr = 48000
    me = speak("Good morning everyone. Let us review the budget for the new website.")
    others = speak("Thank you. The report is ready and the numbers look good.")
    total = int(sr * (2 + len(me) / sr + 2 + len(others) / sr + 2))
    left, right = np.zeros(total, np.float32), np.zeros(total, np.float32)
    left[sr: sr + len(me)] = me
    start = sr + len(me) + 2 * sr
    right[start: start + len(others)] = others

    m = MeetingStore(tmp_path / "meetings").get("2026-06-06T10-00-00-000Z_real-asr-test")
    m.update(status="ready", title="Real ASR")
    write_stereo(m.dir / "audio.opus", left, right)
    cfg["speakers"] = {"me": "Ana", "others": "Others"}
    cfg["transcription"].update(language="en", local={"model": "small", "device": "auto"})
    doc = transcribe(m, cfg, "local")
    by = {t["speaker"]: t["text"].lower() for t in doc["turns"]}
    assert set(by) == {"Ana", "Others"}, doc["turns"]
    assert "morning" in by["Ana"] and "budget" in by["Ana"]
    assert "report" in by["Others"] and "ready" in by["Others"]
    first, second = doc["turns"]
    assert first["speaker"] == "Ana" and second["start"] > first["end"]
