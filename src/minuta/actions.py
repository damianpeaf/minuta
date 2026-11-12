"""What each menu action does, as data. The CLI formats it; the server and the extension can use it too.

Nothing here prints or translates. Long operations report through `on_progress(step, fraction, info)`:
`step` names the stage, `fraction` is 0..1 or None when unknown, `info` holds its numbers.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from pathlib import Path

from minuta import __version__, client, credentials, pairing, paths
from minuta import config as config_mod
from minuta.meetings import DISCARDED, RECORDING, Meeting, MeetingStore

Progress = Callable[[str, "float | None", dict], None]
RECORDING_MODES = config_mod.RECORDING_MODES
# Seconds of work per second of meeting, roughly, to say how long a transcription takes.
SPEED = {"mlx": 0.08, "cuda": 0.05, "cpu": 0.35, "vercel": 0.06}


def store_for(cfg: dict) -> MeetingStore:
    return MeetingStore(config_mod.meetings_dir(cfg))


def router_for(cfg: dict):
    from minuta.router import Router

    return Router(cfg, credentials.store(cfg))


# --- status --------------------------------------------------------------------------------
def status(cfg: dict) -> dict:
    """Service, server, extension and meetings. Same shape as `minuta status --json`."""
    from minuta.service import ServiceManager

    port = int(cfg["server"]["port"])
    svc = ServiceManager().status()
    health = client.health(port)
    st = client.status(port) if health else None
    state = pairing.read_state()
    meetings = [m for m in store_for(cfg).all() if m.status != DISCARDED]
    router = router_for(cfg)
    last_seen = (st or {}).get("last_seen") or state.get("last_seen")
    return {
        "version": __version__,
        "config": str(paths.config_file()),
        "data": str(config_mod.data_dir(cfg)),
        "service": {"installed": svc.installed, "running": svc.running, "detail": svc.detail},
        "server": {"port": port, "answering": bool(health)},
        "extension": {"paired": bool(state.get("allowed_origins")),
                      "last_seen_seconds_ago": round(time.time() - last_seen) if last_seen else None},
        "recording_now": (st or {}).get("recording", []),
        "recording_mode": recording_mode(cfg),
        "meetings": {"total": len(meetings), "without_transcript": sum(1 for m in meetings if not m.has_transcript()),
                     "with_failed_delivery": sum(1 for m in meetings if router.failed(m))},
    }


# --- meetings ------------------------------------------------------------------------------
def meeting_row(m: Meeting) -> dict:
    d = m.read()
    return {"id": m.id, "title": m.title, "code": d.get("code"), "started_at": d.get("started_at"),
            "status": d.get("status"), "duration_seconds": d.get("duration_seconds"),
            "transcript": m.has_transcript(), "audio": m.audio_path is not None,
            "destinations": {k: {s: (v or {}).get("status") for s, v in (st or {}).items()}
                             for k, st in (d.get("destinations") or {}).items()}}


def meetings(cfg: dict, include_discarded: bool = False) -> list[dict]:
    """Oldest first. The shape of `minuta list --json`, plus `code`."""
    return [meeting_row(m) for m in store_for(cfg).all() if include_discarded or m.status != DISCARDED]


def transcribable(cfg: dict) -> list[Meeting]:
    """Meetings that can be transcribed now, newest first."""
    return [m for m in reversed(store_for(cfg).all()) if m.status not in (DISCARDED, RECORDING)]


def short_ids(ids: list[str], minimum: int = 16) -> dict[str, str]:
    """The shortest prefix (at least `minimum` characters) that names each id alone."""
    out = {}
    for i in ids:
        n = min(minimum, len(i))
        while n < len(i) and sum(1 for j in ids if j.startswith(i[:n])) > 1:
            n += 1
        out[i] = i[:n]
    return out


# --- destinations --------------------------------------------------------------------------
TARGET_OPTIONS = {"local": ("copy_to",), "obsidian": ("vault", "folder"), "webhook": ("url",),
                  "command": ("command", "argv"), "rclone": ("remote", "folder"), "notion": ("database_id",)}


def destination_target(opts: dict) -> str | None:
    """Where a destination sends meetings, from its options (never a secret). None: the data folder."""
    kind = opts.get("type")
    if kind == "local":
        return opts.get("copy_to") or None
    if kind == "obsidian":
        vault, folder = opts.get("vault") or "", str(opts.get("folder", "Meetings") or "")
        return str(Path(vault) / folder) if folder else vault
    if kind == "webhook":
        from urllib.parse import urlsplit

        u = urlsplit(str(opts.get("url") or ""))
        return f"{u.scheme}://{u.netloc}{u.path}" if u.netloc else None
    if kind == "rclone":
        return f"{opts.get('remote', '')}:{opts.get('folder', '')}".rstrip(":") or None
    if kind == "command":
        cmd = opts.get("command") or opts.get("argv")
        return " ".join(cmd) if isinstance(cmd, list) else (str(cmd) if cmd else None)
    if kind == "notion":
        return opts.get("database_id") or opts.get("database") or None
    for key in ("url", "folder", "path"):
        if opts.get(key):
            return str(opts[key])
    return None


def destinations(cfg: dict) -> dict:
    from minuta import destinations as reg

    configured = []
    for name, opts in (cfg.get("destinations") or {}).items():
        configured.append({"name": name, "type": opts.get("type"), "enabled": bool(opts.get("enabled", True)),
                           "send": list(opts.get("send") or ["audio", "transcript", "metadata"]),
                           "target": destination_target(opts)})
    types = [{"type": k, "description": c.describe()} for k, c in sorted(reg.registry().items())]
    return {"configured": configured, "types": types}


# --- transcription -------------------------------------------------------------------------
def engine_options(cfg: dict, meeting: Meeting) -> list[dict]:
    """The engines this meeting can be transcribed with, the configured one first, each with a rough
    time and, for paid ones, a cost estimate (an upper bound: the whole meeting, not only speech)."""
    from minuta.transcription import price_per_minute
    from minuta.transcription.vercel import KEY_NAME, PRESETS

    seconds = float(meeting.read().get("duration_seconds") or 0)
    tr = cfg["transcription"]
    out = []
    local = {"engine": "local", "model": None, "paid": False, "usd": 0.0}
    try:
        from minuta.transcription.local import available, resolve_settings

        ok, _ = available()
        s = resolve_settings(tr.get("local") or {}) if ok else {"backend": "", "device": "cpu", "model": "-"}
        local["model"] = s["model"].rsplit("/", 1)[-1]
        speed = SPEED["mlx"] if s["backend"] == "mlx" else SPEED.get(s["device"], SPEED["cpu"])
        local.update(available=ok, seconds=seconds * speed)
    except Exception:  # noqa: BLE001 - shown as not available
        local.update(available=False, seconds=None)
    out.append(local)
    if credentials.store(cfg).get(KEY_NAME):
        models = [tr["vercel"].get("model")] + [p["model"] for p in PRESETS.values()]
        for model in dict.fromkeys(m for m in models if m):
            vcfg = copy.deepcopy(cfg)
            vcfg["transcription"]["vercel"]["model"] = model
            rate = price_per_minute("vercel", vcfg)
            out.append({"engine": "vercel", "model": model, "paid": True, "available": True,
                        "seconds": seconds * SPEED["vercel"], "usd": round(seconds / 60 * rate, 4)})
    configured = (tr.get("engine"), tr["vercel"].get("model") if tr.get("engine") == "vercel" else None)
    out.sort(key=lambda o: (o["engine"], o["model"] if o["engine"] == "vercel" else None) != configured)
    return out


def with_engine(cfg: dict, engine: str, model: str | None) -> dict:
    """A copy of the config that transcribes with `engine` (and, for vercel, `model`)."""
    out = copy.deepcopy(cfg)
    out["transcription"]["engine"] = engine
    if engine == "vercel" and model:
        out["transcription"]["vercel"]["model"] = model
    return out


def transcribe(cfg: dict, meeting: Meeting, engine: str, *, budget=None, window=None,
               on_progress: Progress | None = None) -> dict:
    """Transcribe and hand the transcript to the destinations. Returns `transcript_summary()`."""
    from minuta.transcription.pipeline import transcribe as run

    router = router_for(cfg)
    run(meeting, cfg, engine, router=router, secrets=credentials.store(cfg), budget=budget, window=window,
        on_progress=on_progress)
    return transcript_summary(cfg, meeting)


def transcript_summary(cfg: dict, meeting: Meeting, preview_turns: int = 4) -> dict:
    import json

    try:
        doc = json.loads((meeting.dir / "transcript.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {"started_at": meeting.read().get("started_at")}
    turns = doc.get("turns") or []
    speakers: dict[str, int] = {}
    for turn in turns:
        speakers[turn["speaker"]] = speakers.get(turn["speaker"], 0) + 1
    return {"id": meeting.id, "title": meeting.title, "started_at": doc.get("started_at"),
            "duration_seconds": doc.get("duration_seconds"), "engine": doc.get("engine"),
            "cost_usd": doc.get("cost_usd"), "speakers": speakers, "turns": len(turns),
            "preview": [{k: turn[k] for k in ("start", "speaker", "text")} for turn in turns[:preview_turns]],
            "files": transcript_locations(cfg, meeting), "deliveries": deliveries(meeting, "transcript")}


def transcript_locations(cfg: dict, meeting: Meeting) -> list[dict]:
    """Where transcript.md can be opened, the copies the user looks at first (a copy_to folder, a
    note in the vault), then the data folder."""
    from minuta.destinations import Receipt

    out = []
    for name, stages in (meeting.read().get("destinations") or {}).items():
        opts = (cfg.get("destinations") or {}).get(name) or {}
        st = (stages or {}).get("transcript") or (stages or {}).get("meeting") or {}
        receipt = Receipt.from_dict(st.get("receipt"))
        if st.get("status") != "ok" or not receipt or not receipt.remote_id or (receipt.extra or {}).get("in_place"):
            continue
        p = Path(receipt.remote_id)
        if opts.get("type") == "local" and (p / "transcript.md").exists():
            out.append({"destination": name, "path": str(p / "transcript.md"), "folder": str(p)})
        elif opts.get("type") == "obsidian" and p.is_file():
            out.append({"destination": name, "path": str(p), "folder": str(p.parent)})
    own = meeting.dir / "transcript.md"
    if own.exists():
        out.append({"destination": None, "path": str(own), "folder": str(meeting.dir)})
    return out


def deliveries(meeting: Meeting, stage: str) -> list[dict]:
    out = []
    for name, stages in (meeting.read().get("destinations") or {}).items():
        st = (stages or {}).get(stage) or {}
        if not st:
            continue
        receipt = st.get("receipt") or {}
        in_place = (receipt.get("extra") or {}).get("in_place")
        where = None if in_place else (receipt.get("remote_id") or receipt.get("url"))
        out.append({"destination": name, "status": st.get("status"), "error": st.get("error"), "where": where})
    return out


# --- retry ---------------------------------------------------------------------------------
def retry_targets(cfg: dict, refs: list[str] | None = None) -> list[Meeting]:
    store, router = store_for(cfg), router_for(cfg)
    if refs:
        return [store.resolve(r) for r in refs]
    return [m for m in store.all() if m.status == "ready" and router.needs_delivery(m)]


def retry(cfg: dict, targets: list[Meeting], on_progress: Progress | None = None) -> list[dict]:
    """Send again what failed. One row per meeting, destination and stage."""
    router = router_for(cfg)
    out = []
    for i, m in enumerate(targets):
        if on_progress is not None:
            on_progress("deliver", i / len(targets), {"meeting": m.id, "title": m.title})
        for key, st in router.retry(m).items():
            name, _, stage = key.rpartition(":")
            out.append({"meeting": m.id, "title": m.title, "destination": name, "stage": stage,
                        "status": st.get("status"), "error": st.get("error")})
    if on_progress is not None:
        on_progress("done", 1.0, {})
    return out


# --- recording mode ------------------------------------------------------------------------
def recording_mode(cfg: dict) -> str:
    mode = (cfg.get("recording") or {}).get("mode")
    return mode if mode in RECORDING_MODES else "auto"


def set_recording_mode(mode: str) -> dict:
    """Save the mode in config.toml; the server picks it up and the extension asks the server."""
    if mode not in RECORDING_MODES:
        raise ValueError(f"recording mode must be one of {RECORDING_MODES}")
    cfg = config_mod.load()
    cfg.setdefault("recording", {})["mode"] = mode
    config_mod.save(cfg)
    return cfg
