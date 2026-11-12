"""Free transcription on this machine.

mlx-whisper on Apple Silicon Macs; faster-whisper (CTranslate2) everywhere else, on an NVIDIA GPU
when one is usable and on the CPU with int8 otherwise.
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import platform
import sys
from pathlib import Path

import numpy as np

from minuta.audio import SR
from minuta.transcription.common import invented, language_iso, prompt

log = logging.getLogger(__name__)

BATCH_SIZE = 8  # faster-whisper stretches decoded together; more needs more GPU memory
MLX_MODELS = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _preload_cuda_libs() -> None:
    """cuBLAS and cuDNN come as pip packages (the `cuda` extra); CTranslate2 only looks for them in
    system paths unless they are loaded first."""
    try:
        import nvidia.cublas
        import nvidia.cudnn
    except ImportError:
        return
    for p in (nvidia.cublas.__path__[0], nvidia.cudnn.__path__[0]):
        for so in sorted(glob.glob(p + "/lib/*.so*")) + sorted(glob.glob(p + "/bin/*.dll")):
            try:
                ctypes.CDLL(so, mode=getattr(ctypes, "RTLD_GLOBAL", 0))
            except OSError:
                pass


def _cublas_loads() -> bool:
    """A GPU and its driver are not enough: CTranslate2 also needs cuBLAS 12 (the `cuda` extra or
    a system CUDA install). Without it, choosing CUDA fails only when the first audio is encoded."""
    names = ["cublas64_12.dll"] if os.name == "nt" else ["libcublas.so.12"]
    for name in names:
        try:
            ctypes.CDLL(name, mode=getattr(ctypes, "RTLD_GLOBAL", 0))
            return True
        except OSError:
            continue
    return False


def cuda_available() -> bool:
    try:
        _preload_cuda_libs()
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0 and _cublas_loads()
    except Exception:  # noqa: BLE001
        return False


def resolve_settings(opts: dict) -> dict:
    """Model, device and precision after applying "auto"."""
    if is_apple_silicon():
        model = opts.get("model") or "auto"
        model = "turbo" if model == "auto" else model
        return {"backend": "mlx", "model": MLX_MODELS.get(model, model), "device": "gpu", "compute_type": "float16"}
    device = opts.get("device") or "auto"
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    model = opts.get("model") or "auto"
    if model == "auto":
        # turbo is accurate and fast on a GPU; on a CPU it is too slow for hour-long calls.
        model = "large-v3-turbo" if device == "cuda" else "small"
    compute = "float16" if device == "cuda" else "int8"
    return {"backend": "faster-whisper", "model": os.path.expanduser(model), "device": device, "compute_type": compute}


def _offline_if_cached(repo: str) -> None:
    """With the model already downloaded, do not ask Hugging Face: offline, that request hung ~1 min.
    Must run before importing the library, which reads the variable on import."""
    cache = Path(os.environ.get("HF_HUB_CACHE")
                 or Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub")
    if (cache / f"models--{repo.replace('/', '--')}" / "snapshots").is_dir():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")


FASTER_WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def _folder_size(folder: Path) -> int:
    total = 0
    try:
        for f in folder.iterdir():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def fetch_model(repo: str, patterns: list[str] | None, on_progress=None) -> str:
    """The model's folder, downloading it first when it is not in the Hugging Face cache. While it
    downloads, `on_progress("download", fraction, done=bytes, total=bytes, model=repo)` every ~0.3 s."""
    import fnmatch
    import threading

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # our own bar replaces tqdm's
    import warnings

    from huggingface_hub import constants, snapshot_download
    from huggingface_hub.utils import logging as hf_logging

    hf_logging.set_verbosity_error()  # "unauthenticated requests" and the like are not for the user
    warnings.filterwarnings("ignore", module="huggingface_hub")

    try:
        return snapshot_download(repo, allow_patterns=patterns, local_files_only=True)
    except Exception:  # noqa: BLE001, S110 - not cached (or incomplete): download below
        pass
    total = None
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo, files_metadata=True)
        total = sum(f.size or 0 for f in info.siblings or []
                    if patterns is None or any(fnmatch.fnmatch(f.rfilename, p) for p in patterns)) or None
    except Exception:  # noqa: BLE001, S110 - the size is only for the bar
        pass
    blobs = Path(constants.HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}" / "blobs"
    start = _folder_size(blobs)
    result: dict = {}

    def run() -> None:
        try:
            result["path"] = snapshot_download(repo, allow_patterns=patterns)
        except Exception as e:  # noqa: BLE001 - raised again in the caller's thread
            result["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    while worker.is_alive():
        done = max(0, _folder_size(blobs) - start)
        if on_progress is not None:
            on_progress("download", min(1.0, done / total) if total else None, done=done, total=total, model=repo)
        worker.join(0.3)
    if "error" in result:
        raise result["error"]
    if on_progress is not None:
        on_progress("download", 1.0, done=total or 0, total=total, model=repo)
    return result["path"]


def model_repo(settings: dict) -> str | None:
    """The Hugging Face repository of a model setting, or None for a folder on disk."""
    model = settings["model"]
    if os.path.isdir(model):
        return None
    if settings["backend"] == "mlx" or "/" in model:
        return model
    try:
        from faster_whisper.utils import _MODELS
    except ImportError:
        return None
    return _MODELS.get(model)


class FasterWhisperEngine:
    name = "local"
    usd_per_minute = 0.0

    def __init__(self, opts: dict, on_progress=None):
        s = resolve_settings(opts)
        self.model = s["model"]
        if s["device"] == "cuda":
            _preload_cuda_libs()
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)
        repo = model_repo(s)
        path = fetch_model(repo, FASTER_WHISPER_FILES, on_progress) if repo else s["model"]
        if on_progress is not None:
            on_progress("model", None, engine="local", model=self.model)
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        model = WhisperModel(path, device=s["device"], compute_type=s["compute_type"])
        self._pipe = BatchedInferencePipeline(model)

    def transcribe_channel(self, x: np.ndarray, segments, tcfg: dict, budget=None, cost=None,
                           progress=None) -> list[dict]:
        if not segments:
            return []
        segs, _ = self._pipe.transcribe(
            x,
            language=language_iso(tcfg.get("language")),
            initial_prompt=prompt(tcfg) or None,
            clip_timestamps=[{"start": float(a), "end": float(b)} for a, b in segments],
            batch_size=BATCH_SIZE,
            beam_size=5,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        out = []
        for s in segs:
            text = s.text.strip()
            if (s.no_speech_prob > 0.8 and s.avg_logprob < -1) or not text or invented(text):
                continue
            if progress is not None:
                progress(float(s.end))
            out.append({"start": float(s.start), "end": float(s.end), "text": text})
        return out


class MlxWhisperEngine:
    name = "local"
    usd_per_minute = 0.0

    def __init__(self, opts: dict, on_progress=None):
        s = resolve_settings(opts)
        self.model = s["model"]
        repo = model_repo(s)
        self._path = fetch_model(repo, None, on_progress) if repo else self.model
        _offline_if_cached(self.model)
        if on_progress is not None:
            on_progress("model", None, engine="local", model=self.model)
        import mlx.core as mx
        import mlx_whisper

        # Without a limit MLX keeps every buffer it used and the process grows to ~11 GB of GPU memory.
        mx.set_cache_limit(512 * 1024**2)
        self._mlx = mlx_whisper

    def _piece(self, audio: np.ndarray, tcfg: dict) -> list[dict]:
        r = self._mlx.transcribe(
            audio,
            path_or_hf_repo=self._path,
            verbose=None,  # False would still draw tqdm's bar over ours
            language=language_iso(tcfg.get("language")),
            initial_prompt=prompt(tcfg) or None,
            condition_on_previous_text=False,
            # One pass: retrying with other temperatures turns a 1 s stretch into 20 s of GPU.
            temperature=0.0,
            word_timestamps=True,
            hallucination_silence_threshold=2.0,
        )
        out = []
        for s in r["segments"]:
            text = s["text"].strip()
            if s.get("no_speech_prob", 0) > 0.8 and s.get("avg_logprob", 0) < -1:
                continue
            if text and not invented(text):
                out.append({"start": float(s["start"]), "end": float(s["end"]), "text": text})
        return out

    def transcribe_channel(self, x: np.ndarray, segments, tcfg: dict, budget=None, cost=None,
                           progress=None) -> list[dict]:
        out = []
        for a, b in segments:
            for s in self._piece(x[int(a * SR): int(b * SR)], tcfg):
                out.append({"start": a + s["start"], "end": a + min(s["end"], b - a), "text": s["text"]})
            if progress is not None:
                progress(float(b))
        return out


def create(opts: dict, on_progress=None):
    cls = MlxWhisperEngine if is_apple_silicon() else FasterWhisperEngine
    return cls(opts, on_progress=on_progress)


def available() -> tuple[bool, str]:
    """Is the local engine installed? (does not load a model)"""
    import importlib.util

    mod = "mlx_whisper" if is_apple_silicon() else "faster_whisper"
    if importlib.util.find_spec(mod) is None:
        return False, f"{mod} is not installed (reinstall with the 'local' extra)"
    return True, mod
