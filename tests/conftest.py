"""Shared fixtures. Every test runs with its own MINUTA_HOME and without the OS keyring."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from minuta import config as config_mod

ROOT = Path(__file__).resolve().parents[1]
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("MINUTA_HOME", str(h))
    monkeypatch.setenv("MINUTA_NO_KEYRING", "1")
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    return h


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Deliveries are retried without waiting (a test that checks the waits sets BACKOFF itself)."""
    monkeypatch.setattr("minuta.router.BACKOFF", 0)


@pytest.fixture
def cfg(home):
    c = config_mod.deep_merge(config_mod.DEFAULTS, {})
    config_mod.save(c)
    return c


def write_stereo(path: Path, left: np.ndarray, right: np.ndarray, sr: int = 48000, codec: str = "libopus") -> Path:
    """Encode two float32 arrays as a stereo file (webm/opus by default)."""
    pcm = np.stack([left, right], axis=1).astype(np.float32).tobytes()
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "2",
            "-i", "-", "-c:a", codec]
    if codec == "libopus":
        args += ["-b:a", "64k", "-f", "webm"]
    subprocess.run([*args, str(path)], input=pcm, check=True)
    return path


def tone(seconds: float, freq: float, amp: float = 0.1, sr: int = 48000) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def bursts(spans: list[tuple[float, float]], total: float, freq: float = 300.0, amp: float = 0.2,
           sr: int = 48000) -> np.ndarray:
    """Silence with tone bursts at the given (start, end) seconds: crude stand-in for speech."""
    x = np.zeros(int(total * sr), dtype=np.float32)
    for a, b in spans:
        seg = tone(b - a, freq, amp, sr)
        x[int(a * sr): int(a * sr) + seg.size] = seg[: x.size - int(a * sr)]
    return x


@pytest.fixture
def stereo_webm(tmp_path) -> Path:
    """3 s: 440 Hz on the left (you), 880 Hz on the right (others)."""
    return write_stereo(tmp_path / "call.webm", tone(3, 440), tone(3, 880))
