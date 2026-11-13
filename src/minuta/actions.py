"""What each menu action does, as data. The CLI formats it; the server offers it to the extension's page.

Nothing here prints, and only texts meant for a person (doctor details, form labels, step labels) are
translated. Long operations report through `on_progress(step, fraction, info)`: `step` names the
stage, `fraction` is 0..1 or None when unknown, `info` holds its numbers.
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
def status(cfg: dict, live: dict | None = None) -> dict:
    """Service, server, extension and meetings. Same shape as `minuta status --json`. `live`: the
    server's own state, when the server itself asks (it does not call itself over HTTP)."""
    from minuta.service import ServiceManager

    port = int(cfg["server"]["port"])
    svc = ServiceManager().status()
    health = True if live is not None else client.health(port)
    st = live if live is not None else (client.status(port) if health else None)
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


# --- doctor ----------------------------------------------------------------------------------
# What the page can offer for a check that is not ok: the section where it is fixed.
FIXES = {"engine vercel": "settings", "engine local": "settings", "extension": "pair"}


def doctor(cfg: dict) -> list[dict]:
    """`minuta doctor`'s checks, with the name in the current language and where to fix each problem."""
    from minuta import doctor as doc

    out = []
    for c in doc.checks(cfg):
        fix = None
        if c["status"] != "ok":
            fix = "destinations" if c["check"].startswith("destination ") else FIXES.get(c["check"])
        out.append({**c, "label": doc.label(c["check"]), "fix": fix})
    return out


# --- one meeting -----------------------------------------------------------------------------
class NotFound(LookupError):
    """No meeting, destination or job by that name."""


def meeting(cfg: dict, meeting_id: str) -> Meeting:
    """The meeting with exactly this id; NotFound when there is none."""
    from minuta.meetings import valid_id

    if not valid_id(meeting_id):
        raise NotFound(meeting_id)
    m = store_for(cfg).get(meeting_id)
    if not m.exists() or m.status is None:
        raise NotFound(meeting_id)
    return m


def meeting_detail(cfg: dict, m: Meeting) -> dict:
    """Everything the page shows about one meeting: its row, details, deliveries and the transcript."""
    import json

    d = m.read()
    out = meeting_row(m)
    out.update({k: d.get(k) for k in ("url", "ended_at", "end_reason", "error", "transcript_status",
                                      "transcript_error")})
    out["warning"] = (d.get("audio_check") or {}).get("warning")
    out["audio_file"] = m.audio_path.name if m.audio_path else None
    out["folder"] = str(m.dir)
    out["deliveries"] = {"meeting": deliveries(m, "meeting"), "transcript": deliveries(m, "transcript")}
    out["files"] = transcript_locations(cfg, m)
    out["transcribable"] = m.status not in (DISCARDED, RECORDING) and m.status is not None
    out["transcript_text"] = None
    if m.has_transcript():
        try:
            doc = json.loads((m.dir / "transcript.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        out["transcript_text"] = {"engine": doc.get("engine"), "generated_at": doc.get("generated_at"),
                                  "cost_usd": doc.get("cost_usd"),
                                  "turns": [{k: turn.get(k) for k in ("start", "end", "speaker", "text")}
                                            for turn in doc.get("turns") or []]}
    return out


def open_meeting(cfg: dict, m: Meeting, what: str) -> dict:
    """Open the meeting's folder or its transcript with the system's app. Only paths of this meeting."""
    from minuta import ui

    if what == "transcript":
        files = transcript_locations(cfg, m)
        if not files:
            raise ValueError("this meeting has no transcript")
        path = Path(files[0]["path"])
    elif what == "folder":
        path = m.dir
    else:
        raise ValueError("what must be 'folder' or 'transcript'")
    return {"opened": ui.open_with_system(path), "path": str(path)}


# Progress steps in words, the same in the terminal and on the page.
STEP_LABELS = {"audio": "transcribe.step.audio", "model": "transcribe.step.model",
               "download": "transcribe.step.download", "transcribe": "transcribe.step.transcribe",
               "deliver": "transcribe.step.deliver"}
STEP_DONE = {"audio": "transcribe.done.audio", "model": "transcribe.done.model",
             "download": "transcribe.done.download", "transcribe": "transcribe.done.transcribe",
             "deliver": "transcribe.done.deliver"}


def step_words(step: str | None, info: dict) -> dict:
    from minuta import ui

    model = str(info.get("model") or info.get("engine") or "").rsplit("/", 1)[-1]
    total = info.get("total")
    size = f" ({total / 1e6:.0f} MB)" if step == "download" and total else ""
    return {"model": model, "size": size, "speech": ui.duration(total) if total else "-"}


def step_label(kind: str, step: str | None, info: dict, done: bool = False) -> str:
    """What a job is doing (or, with `done`, did) in the current language."""
    from minuta.i18n import t

    if kind == "retry":
        return t("retry.step") if step != "done" else t("retry.step_done")
    table = STEP_DONE if done else STEP_LABELS
    if step not in table:
        return ""
    return t(table[step], **step_words(step, info))


# --- transcribing, step by step ------------------------------------------------------------------
class OverCap(ValueError):
    """The estimate is above the spending cap."""


def plan_transcription(cfg: dict, m: Meeting, engine: str | None = None, model: str | None = None,
                       max_usd: float | None = None, window=None) -> dict:
    """What a run would use and cost: engine, model, whether it is paid, the estimate (paid engines only:
    it measures the speech to send) and the cap. The terminal and the page ask before spending with it."""
    from minuta.transcription import ENGINES, price_per_minute
    from minuta.transcription.pipeline import estimate_run

    if m.status in (DISCARDED, RECORDING, None):
        raise ValueError("this meeting cannot be transcribed now")
    engine = engine or cfg["transcription"]["engine"]
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}")
    run_cfg = with_engine(cfg, engine, model)
    cap = float(max_usd if max_usd is not None else cfg["transcription"].get("max_usd") or 0)
    paid = price_per_minute(engine, run_cfg) > 0
    est = {"engine": engine, "seconds": 0.0, "usd": 0.0}
    if paid:
        est = estimate_run(m, run_cfg, engine, router=router_for(cfg), window=window)
    return {"meeting": m.id, "engine": engine, "model": run_cfg["transcription"]["vercel"]["model"]
            if engine == "vercel" else None, "paid": paid, "estimate": est, "cap": cap,
            "over_cap": paid and est["usd"] > cap, "window": list(window) if window else None}


def run_transcription(cfg: dict, m: Meeting, plan: dict, on_progress: Progress | None = None) -> dict:
    """Transcribe as `plan` says, with the cap as budget for paid engines. Returns `transcript_summary()`."""
    from minuta.jobs import Cancelled
    from minuta.transcription.budget import Budget

    if plan["over_cap"]:
        raise OverCap(f"the estimate is above the cap of US${plan['cap']:.2f}")
    run_cfg = with_engine(cfg, plan["engine"], plan["model"])
    window = tuple(plan["window"]) if plan.get("window") else None
    try:
        return transcribe(run_cfg, m, plan["engine"], budget=Budget(plan["cap"]) if plan["paid"] else None,
                          window=window, on_progress=on_progress)
    except Cancelled:
        m.update(transcript_status="done" if m.has_transcript() else None, transcript_error=None)
        raise


# --- destinations: add, test, change, remove ----------------------------------------------------
def destination_secrets(opts: dict) -> dict[str, bool]:
    """For each secret option of a destination: configured or not. Never the value."""
    from minuta import destinations as reg

    cls = reg.registry().get(opts.get("type"))
    names = list(getattr(cls, "secret_options", ()) or ())
    names += [k[: -len("_secret")] for k in opts if k.endswith("_secret") and k[: -len("_secret")] not in names]
    return {n: bool(opts.get(f"{n}_secret")) for n in names}


def destination_form(cfg: dict, kind: str, name: str, answers: dict) -> dict:
    """The next questions to add a destination of type `kind` (see `forms.next_step`)."""
    from minuta import destinations as reg
    from minuta import forms

    cls = _destination_class(kind)
    _check_name(name)
    base = {"type": kind, "enabled": True, "send": list(getattr(cls, "default_send", reg.PARTS))}
    return forms.next_step(cls, name, base, answers, credentials.store(cfg))


def add_destination(kind: str, name: str, answers: dict, force: bool = False) -> dict:
    """Add (or replace) a destination with the answers to its form, as the terminal does: same
    `setup.add_destination`, same checks, secrets to the secret store. Saved when it is added."""
    from minuta import forms
    from minuta.redact import redact
    from minuta.setup import Cancelled
    from minuta.setup import add_destination as add

    _destination_class(kind)
    cfg = config_mod.load()
    try:
        ok, message = add(cfg, name, kind, {}, forms.Answers(answers), force=force)
    except forms.Incomplete as e:
        return {"ok": False, "message": "", "fields": [f.to_dict() for f in e.fields], "errors": e.errors}
    except Cancelled:
        return {"ok": False, "message": "cancelled"}
    if ok:
        config_mod.save(cfg)
    return {"ok": ok, "message": redact(message)}


def remove_destination(name: str) -> dict:
    from minuta import destinations as reg

    cfg = config_mod.load()
    opts = _configured(cfg, name)
    reg.forget_secrets(opts, credentials.store(cfg))
    del cfg["destinations"][name]
    config_mod.save(cfg)
    return {"ok": True}


def set_destination_enabled(name: str, enabled: bool) -> dict:
    cfg = config_mod.load()
    _configured(cfg, name)["enabled"] = bool(enabled)
    config_mod.save(cfg)
    return {"ok": True, "enabled": bool(enabled)}


def test_destination(cfg: dict, name: str, live: bool = False) -> dict:
    """check(), and with `live` a real test event where the type has one. Messages are redacted."""
    from minuta import destinations as reg
    from minuta.redact import redact

    opts = _configured(cfg, name)
    out: dict = {"name": name, "ok": False, "message": "", "live": None}
    try:
        dest = reg.create(name, opts, credentials.store(cfg))
        check = dest.check()
    except Exception as e:  # noqa: BLE001
        out["message"] = redact(str(e))
        return out
    out.update(ok=check.ok, message=redact(check.message))
    if not check.ok or not live:
        return out
    ping = getattr(dest, "ping", None)
    if ping is None:
        out["live"] = {"ok": None, "supported": False}
        return out
    try:
        receipt = ping()
        out["live"] = {"ok": True, "supported": True, "url": receipt.url}
    except Exception as e:  # noqa: BLE001
        out["live"] = {"ok": False, "supported": True, "error": redact(str(e))}
        out["ok"] = False
    return out


def _destination_class(kind: str):
    from minuta import destinations as reg

    cls = reg.registry().get(kind)
    if cls is None:
        raise ValueError(f"unknown destination type {kind!r}; available: {', '.join(sorted(reg.registry()))}")
    return cls


def _check_name(name: str) -> None:
    from minuta import destinations as reg

    if not isinstance(name, str) or not reg.NAME_RE.match(name):
        raise ValueError(f"invalid destination name {name!r}: up to 32 lowercase letters, digits, '-' or '_', "
                         "starting with a letter")


def _configured(cfg: dict, name: str) -> dict:
    opts = (cfg.get("destinations") or {}).get(name)
    if opts is None:
        raise NotFound(name)
    return opts


# --- settings ---------------------------------------------------------------------------------------
LOCAL_MODELS = ("auto", "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
LANGUAGES = ("en", "es")


def settings(cfg: dict) -> dict:
    """What the page's settings show. The API key is only said to be configured or not."""
    from minuta.transcription.vercel import KEY_NAME, PRESETS, usd_per_minute

    tr = cfg["transcription"]
    return {"recording_mode": recording_mode(cfg), "recording_modes": list(RECORDING_MODES),
            "language": cfg["general"].get("language", "en"), "languages": list(LANGUAGES),
            "name": cfg["speakers"].get("me", ""), "others_name": cfg["speakers"].get("others", ""),
            "engine": tr.get("engine", "local"), "engines": ["local", "vercel"],
            "transcription_mode": tr.get("mode", "on-demand"),
            "transcription_modes": list(config_mod.TRANSCRIPTION_MODES),
            "local_model": (tr.get("local") or {}).get("model", "auto"), "local_models": list(LOCAL_MODELS),
            "vercel_model": (tr.get("vercel") or {}).get("model"),
            "vercel_models": [{"name": k, "model": v["model"], "usd_per_hour": round(60 * usd_per_minute(v), 3)}
                              for k, v in PRESETS.items()],
            "max_usd": float(tr.get("max_usd") or 0),
            "api_key_configured": bool(credentials.store(cfg).get(KEY_NAME))}


def save_settings(changes: dict) -> dict:
    """Change some settings and save config.toml; `api_key` goes to the secret store, after asking the
    gateway whether it works (it costs nothing). ValueError names what is not valid; nothing is saved then."""
    cfg = config_mod.load()
    g, tr, sp = cfg["general"], cfg["transcription"], cfg["speakers"]
    allowed = {"recording_mode", "language", "name", "others_name", "engine", "transcription_mode",
               "local_model", "vercel_model", "max_usd", "api_key"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")

    def pick(key: str, options) -> str:
        value = changes[key]
        if value not in options:
            raise ValueError(f"{key} must be one of {', '.join(options)}")
        return value

    def label(key: str) -> str:
        value = str(changes[key] or "").strip()
        if not value or len(value) > 80:
            raise ValueError(f"{key} must have between 1 and 80 characters")
        return value

    if "recording_mode" in changes:
        cfg.setdefault("recording", {})["mode"] = pick("recording_mode", RECORDING_MODES)
    if "language" in changes:
        g["language"] = pick("language", LANGUAGES)
    if "name" in changes:
        sp["me"] = label("name")
    if "others_name" in changes:
        sp["others"] = label("others_name")
    if "engine" in changes:
        tr["engine"] = pick("engine", ("local", "vercel"))
    if "transcription_mode" in changes:
        tr["mode"] = pick("transcription_mode", config_mod.TRANSCRIPTION_MODES)
    if "local_model" in changes:
        tr.setdefault("local", {})["model"] = pick("local_model", LOCAL_MODELS)
    if "vercel_model" in changes:
        from minuta.transcription.vercel import PRESETS

        tr.setdefault("vercel", {})["model"] = pick("vercel_model", [p["model"] for p in PRESETS.values()])
    if "max_usd" in changes:
        try:
            value = float(changes["max_usd"])
        except (TypeError, ValueError):
            raise ValueError("max_usd must be a number") from None
        if not 0 <= value <= 1000:
            raise ValueError("max_usd must be between 0 and 1000")
        tr["max_usd"] = value
    key_check = None
    if changes.get("api_key"):
        from minuta.transcription.vercel import KEY_NAME, check_key

        key = str(changes["api_key"]).strip()
        verdict, detail = check_key(key)
        if verdict == "invalid":
            raise ValueError("the gateway refused this key")
        key_check = {"verdict": verdict, "detail": detail}
        credentials.store(cfg).set(KEY_NAME, key)
    errors = config_mod.validate(cfg)
    if errors:
        raise ValueError("; ".join(errors))
    config_mod.save(cfg)
    return {"ok": True, "settings": settings(cfg), "key_check": key_check}
