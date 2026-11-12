"""`minuta setup`: the assistant, and its non-interactive twin driven by flags."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import time
from pathlib import Path

from minuta import browser, client, credentials, pairing
from minuta import config as config_mod
from minuta.i18n import STRINGS, set_language, t
from minuta.service import ServiceManager


def full_name() -> str:
    """The user's full name from the system (macOS `id -F`, the GECOS field on Linux, the display name
    on Windows), or else the login name."""
    name = ""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=5)
            name = r.stdout.strip() if r.returncode == 0 else ""
        elif os.name == "nt":
            name = _windows_display_name()
        else:
            import pwd

            name = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except (OSError, KeyError, subprocess.SubprocessError, ImportError):
        name = ""
    return name or getpass.getuser() or "Me"


def _windows_display_name() -> str:
    import ctypes

    size = ctypes.c_ulong(0)
    secur32 = ctypes.windll.secur32  # type: ignore[attr-defined]
    secur32.GetUserNameExW(3, None, ctypes.byref(size))  # 3 = NameDisplay
    if not size.value:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    return buf.value if secur32.GetUserNameExW(3, buf, ctypes.byref(size)) else ""


class Prompter:
    """Questions in a terminal. Not interactive, each one answers its default (and a password, nothing)."""

    def __init__(self, interactive: bool):
        self.interactive = interactive

    def _ask(self, kind: str, message: str, fallback, **kw):
        if not self.interactive:
            return fallback
        import questionary

        answer = getattr(questionary, kind)(message, **kw).ask()
        if answer is None:  # Ctrl+C
            raise KeyboardInterrupt
        return answer.strip() if isinstance(answer, str) else answer

    def select(self, message: str, choices: list[tuple[str, str]], default: str) -> str:
        from questionary import Choice

        qc = [Choice(title=label, value=value) for value, label in choices]
        chosen = next((c for c in qc if c.value == default), None)
        return self._ask("select", message, default, choices=qc, default=chosen)

    def text(self, message: str, default: str = "") -> str:
        return self._ask("text", message, default, default=default)

    def path(self, message: str, default: str = "") -> str:
        return self._ask("path", message, default, default=default, only_directories=True)

    def confirm(self, message: str, default: bool) -> bool:
        """Yes or no, in the CLI's language (questionary's own confirm always says Yes/No)."""
        if not self.interactive:
            return default
        choices = [("yes", t("answer.yes")), ("no", t("answer.no"))]
        return self.select(message, choices, "yes" if default else "no") == "yes"

    def password(self, message: str) -> str:
        return self._ask("password", message, "")


def parse_options(items: list[str] | None) -> dict:
    """`key=value` items from `--option`; `key@env=VAR` reads the value from VAR (keeps secrets off argv)."""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--option expects key=value, got {item!r}")
        k, v = (x.strip() for x in item.split("=", 1))
        if k.endswith("@env"):
            k = k[:-4]
            if not os.environ.get(v):
                raise ValueError(f"--option {k}@env={v}: environment variable {v} is not set")
            v = os.environ[v]
        out[k] = v
    return out


def build_config(args, p: Prompter) -> tuple[dict, str | None]:
    """Ask (or read flags) and return (config, api key to store or None)."""
    cfg = config_mod.load() if config_mod.exists() else config_mod.deep_merge(config_mod.DEFAULTS, {})
    g = cfg["general"]

    lang = args.language or p.select(t("setup.language"), [("en", "English"), ("es", "Español")], g["language"])
    g["language"] = lang
    set_language(lang)
    from minuta import ui

    ui.say(t("setup.welcome"))

    default_name = cfg["speakers"].get("me") if cfg["speakers"].get("me") != "Me" else full_name()
    cfg["speakers"]["me"] = args.name or p.text(t("setup.name"), default_name) or "Me"
    if args.others_name:
        cfg["speakers"]["others"] = args.others_name
    elif cfg["speakers"].get("others") in ("Others", "Otros"):
        cfg["speakers"]["others"] = t("setup.others")

    data_default = str(config_mod.data_dir(cfg))
    data = args.data_dir or p.path(t("setup.data_dir"), data_default)
    g["data_dir"] = "" if data == str(config_mod.paths.default_data_dir()) else str(Path(data).expanduser())

    fmt = args.audio_format or p.select(t("setup.audio_format"),
                                        [("opus", t("setup.format.opus")), ("mp3", t("setup.format.mp3"))],
                                        g["audio_format"])
    g["audio_format"] = fmt

    if p.interactive and (cfg["destinations"].get("local") or {}).get("type") == "local":
        ok, message = add_destination(cfg, "local", "local", {}, p, keep_existing=True)  # asks for copy_to
        if not ok:
            ui.warn(t("setup.dest_failed", name="local", error=message))
    if args.delete_audio_after_upload is not None:
        cfg["storage"]["delete_local_audio_after_upload"] = bool(args.delete_audio_after_upload)
    elif (cfg["destinations"].get("local") or {}).get("copy_to"):
        cfg["storage"]["delete_local_audio_after_upload"] = p.confirm(
            t("setup.delete_after"), cfg["storage"]["delete_local_audio_after_upload"])

    tr = cfg["transcription"]
    api_key = engine_step(cfg, args, p)
    tr["mode"] = args.transcription_mode or p.select(t("setup.mode"), [("on-demand", t("setup.mode.on-demand")),
                                                                         ("auto", t("setup.mode.auto"))], tr["mode"])
    if args.transcription_language is not None:
        tr["language"] = args.transcription_language
    elif p.interactive:
        tr["language"] = p.text(t("setup.tlang"), tr.get("language") or "")
    rec = cfg.setdefault("recording", {})
    rec["mode"] = getattr(args, "recording_mode", None) or p.select(
        t("setup.recording"), [(m, t(f"mode.{m}.long")) for m in config_mod.RECORDING_MODES], rec.get("mode", "auto"))
    if getattr(args, "extension_dir", None):
        g["extension_dir"] = str(Path(args.extension_dir).expanduser())
    if args.port:
        cfg["server"]["port"] = int(args.port)
    if args.app_dir:
        g["app_dir"] = str(Path(args.app_dir).expanduser().resolve())
    if args.install_extras is not None:
        g["install_extras"] = args.install_extras
    if args.install_ref is not None:
        g["install_ref"] = args.install_ref
    if args.secrets_backend:
        cfg["secrets"]["backend"] = args.secrets_backend
    return cfg, api_key


def engine_step(cfg: dict, args, p: Prompter) -> str | None:
    """Choose local or Vercel AI Gateway, explaining both first. Returns an API key to store, or None."""
    from minuta import ui
    from minuta.actions import SPEED
    from minuta.transcription.vercel import KEY_NAME, KEYS_PAGE, PRESETS, check_key, usd_per_minute

    tr = cfg["transcription"]
    if getattr(args, "local_model", None):
        tr["local"]["model"] = args.local_model
    engine = getattr(args, "engine", None)
    if engine is None and p.interactive:
        hour = 3600 * _local_speed(tr, SPEED)
        ui.blank()
        ui.say(t("setup.engine.explain_local", time=ui.duration(hour)))
        ui.say(t("setup.engine.explain_vercel", usd=60 * usd_per_minute({"model": PRESETS["whisper"]["model"]})))
        engine = p.select(t("setup.engine"), [("local", t("setup.engine.local")),
                                               ("vercel", t("setup.engine.vercel"))], tr["engine"])
    tr["engine"] = engine = engine or tr["engine"]
    if engine != "vercel":
        return None
    current = tr["vercel"].get("model", PRESETS["whisper"]["model"])
    model = getattr(args, "vercel_model", None) or p.select(
        t("setup.vercel_model"), [(v["model"], _preset_label(k, v)) for k, v in PRESETS.items()], current)
    tr["vercel"]["model"] = model
    if getattr(args, "max_usd", None) is not None:
        tr["max_usd"] = float(args.max_usd)
    if getattr(args, "api_key_env", None):
        return os.environ.get(args.api_key_env) or None
    if not p.interactive:
        return None
    has = bool(credentials.store(cfg).get(KEY_NAME))
    ui.say(t("setup.api_key_where", url=KEYS_PAGE))
    while True:
        key = p.password(t("setup.api_key_keep") if has else t("setup.api_key")) or None
        if key is None:
            break
        with ui.step(t("setup.api_key_checking")):
            verdict, detail = check_key(key)
        if verdict == "ok":
            ui.ok(t("setup.api_key_ok", balance=detail or "?"))
            break
        if verdict == "unknown":
            ui.warn(t("setup.api_key_unknown", reason=detail or "?"))
            break
        ui.fail(t("setup.api_key_invalid"))
    tr["max_usd"] = float(p.text(t("setup.max_usd"), str(tr["max_usd"])) or tr["max_usd"])
    ui.say(t("setup.api_key_confirm_note"), style="dim")
    return key


def _local_speed(tr: dict, speed: dict) -> float:
    try:
        from minuta.transcription.local import is_apple_silicon

        if is_apple_silicon():
            return speed["mlx"]
        device = (tr.get("local") or {}).get("device") or "auto"
        if device == "auto":
            import shutil

            device = "cuda" if shutil.which("nvidia-smi") else "cpu"
        return speed.get(device, speed["cpu"])
    except Exception:  # noqa: BLE001 - only for an estimate
        return speed["cpu"]


def _preset_label(name: str, preset: dict) -> str:
    from minuta.transcription.vercel import usd_per_minute

    return t("setup.vercel_preset", name=name, model=preset["model"], usd=60 * usd_per_minute(preset))


def store_api_key(cfg: dict, api_key: str | None) -> None:
    if cfg["transcription"]["engine"] != "vercel":
        return
    from minuta import ui
    from minuta.transcription.vercel import KEY_NAME

    store = credentials.store(cfg)
    if api_key:
        ui.ok(t("setup.api_key_saved", where=store.set(KEY_NAME, api_key)))
    elif not store.get(KEY_NAME):
        ui.warn(t("setup.api_key_missing"))


class Cancelled(Exception):
    """The destination's configure() returned None: the user stopped, or something it needs is missing."""


def add_destination(cfg: dict, name: str, kind: str, opts: dict, prompter: Prompter | None = None,
                    force: bool = False, keep_existing: bool = False) -> tuple[bool, str]:
    """Configure, store secrets, check and add a destination to `cfg` (not saved). Returns (added, message).

    The one place that calls `Destination.configure()`: `minuta destinations add` and the setup assistant
    both come through here. `prompter` is None when nobody can answer questions. With `keep_existing`,
    options already saved under `name` (same type) are the starting point. Raises Cancelled, or
    ValueError for a name that cannot be used."""
    from minuta import destinations as reg
    from minuta.destinations import DestinationError

    if isinstance(opts.get("send"), str):
        opts = {**opts, "send": [x.strip() for x in opts["send"].split(",") if x.strip()]}
    if not reg.NAME_RE.match(name):
        raise ValueError(f"invalid destination name {name!r}: up to 32 lowercase letters, digits, '-' or '_', "
                         "starting with a letter")
    cls = reg.registry()[kind]
    store = credentials.store(cfg)
    existing = (cfg.get("destinations") or {}).get(name) or {}
    if not (keep_existing and existing.get("type") == kind):
        existing = {}
    opts = {"type": kind, "enabled": True, "send": list(getattr(cls, "default_send", reg.PARTS)), **existing,
            **{k: v for k, v in opts.items() if v not in ("", None)}}
    try:
        configured = cls.configure(name, opts, prompter, store)
    except (DestinationError, ValueError) as e:
        return False, str(e)
    if configured is None:
        raise Cancelled(name)
    opts = reg.store_secrets(name, cls, dict(configured), store)
    check = reg.create(name, opts, store).check()
    if not check.ok and not force:
        if not existing and name not in (cfg.get("destinations") or {}):
            reg.forget_secrets(opts, store)
        return False, check.message
    cfg.setdefault("destinations", {})[name] = opts
    return True, check.message


def destinations_step(cfg: dict, args, p: Prompter) -> list[str]:
    """Add more destinations: the ones given with `--destination TYPE[:NAME]` (each followed by its
    `--option key=value`), or else, in a terminal, one picked from a list. Returns the names added.
    Each goes through add_destination(), so its configure() asks what is missing."""
    from minuta import destinations as reg
    from minuta import ui
    from minuta.redact import redact

    wanted = []
    for d in getattr(args, "destination", None) or []:
        kind, _, name = d["spec"].partition(":")
        wanted.append((name or kind, kind, d["options"]))
    if not wanted and p.interactive:
        kinds = [k for k in reg.registry() if k != "local"]
        labels = {k: t(f"setup.more.{k}") if f"setup.more.{k}" in STRINGS else f"{k}: {reg.registry()[k].describe()}"
                  for k in kinds}
        kind = p.select(t("setup.more"), [("none", t("setup.more.none")), *labels.items()], "none")
        if kind != "none":
            wanted.append((kind, kind, []))
    added = []
    for name, kind, options in wanted:
        try:
            if kind not in reg.registry():
                raise ValueError(f"unknown type {kind!r}; available: {', '.join(sorted(reg.registry()))}")
            ok, message = add_destination(cfg, name, kind, parse_options(options), p if p.interactive else None,
                                          keep_existing=True)
        except Cancelled:
            continue
        except ValueError as e:
            ok, message = False, str(e)
        if not ok:
            ui.warn(redact(t("setup.dest_failed", name=name, error=message)))
            continue
        ui.ok(redact(t("setup.dest_added", name=name, message=message)))
        added.append(name)
        _offer_deleting_local_audio(cfg, name, p)
    return added


def _offer_deleting_local_audio(cfg: dict, name: str, p: Prompter) -> None:
    """Once a destination that can give the audio back holds it, the local copy may go."""
    from minuta import destinations as reg

    opts = cfg["destinations"][name]
    cls = reg.registry()[opts["type"]]
    if not (p.interactive and cls.capabilities.fetch_audio and "audio" in (opts.get("send") or ())):
        return
    if cfg["storage"]["delete_local_audio_after_upload"] or not p.confirm(t("setup.delete_after"), False):
        return
    cfg["storage"]["delete_local_audio_after_upload"] = True
    # A local destination without copy_to keeps the audio in place, so it cannot hold it once
    # deleted and would block the deletion: it keeps the transcript and details only.
    local = (cfg.get("destinations") or {}).get("local") or {}
    if local.get("type") == "local" and not local.get("copy_to"):
        local["send"] = [x for x in (local.get("send") or ["audio", "transcript", "metadata"]) if x != "audio"]


def wait_for_server(port: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if client.health(port):
            return True
        time.sleep(0.5)
    return False


def wait_for_extension(port: int, seconds: float, since: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        st = client.status(port)
        if st and st.get("paired_origins") and (st.get("last_seen") or 0) >= since:
            return True
        time.sleep(1)
    return False


def file_manager_name() -> str:
    return t("fm.mac") if sys.platform == "darwin" else t("fm.windows") if os.name == "nt" else t("fm.linux")


def extension_step(cfg: dict, args, p: Prompter) -> None:
    from minuta import ui

    port = int(cfg["server"]["port"])
    with ui.step(t("setup.ext_copying")):
        ext = browser.install_extension(cfg)
    code = pairing.new_code()
    found = browser.detect()
    ui.blank()
    ui.markup(f"[head]{ui.esc(t('setup.ext_title'))}[/]")
    ui.say(t("setup.ext_folder"))
    ui.markup("  " + ui.path_markup(ext))
    opening = p.interactive and not args.no_browser
    if found:
        chosen = found[0]
        if p.interactive and len(found) > 1:
            name = p.select(t("setup.which_browser"), [(b.name, b.name) for b in found], found[0].name)
            chosen = next(b for b in found if b.name == name)
        ui.say(t("setup.ext_steps", url=chosen.extensions_url, browser=chosen.name, fm=file_manager_name()))
        others = [b for b in found if b is not chosen]
        if others:
            ui.say(t("setup.ext_other_browsers", list=", ".join(f"{b.name}: {b.extensions_url}" for b in others)),
                   style="dim")
    else:
        ui.say(t("setup.no_browser"))
    ui.markup(f"{ui.esc(t('pair.code_label'))}  [bold accent]{code}[/]  [dim]{ui.esc(t('pair.valid'))}[/]")
    if opening:
        if ui.copy_to_clipboard(str(ext)):
            ui.ok(t("setup.ext_copied"))
        if ui.open_with_system(ext):
            ui.ok(t("setup.ext_folder_opened", fm=file_manager_name()))
        if found:
            browser.open_extensions_page(chosen)
    if args.skip_extension or (not p.interactive and not args.wait_extension):
        ui.say(t("setup.ext_skipped"))
        return
    seconds = int(args.wait_extension or 600)
    since = time.time() - 1
    try:
        with ui.step(t("setup.waiting_ext", seconds=seconds)):
            ok = wait_for_extension(port, seconds, since)
    except KeyboardInterrupt:
        ui.say(t("setup.ext_skipped"))
        return
    (ui.ok if ok else ui.warn)(t("setup.ext_ok") if ok else t("setup.ext_timeout"))


def run(args) -> int:
    from minuta import ui

    interactive = not args.non_interactive
    p = Prompter(interactive)
    try:
        cfg, api_key = build_config(args, p)
        destinations_step(cfg, args, p)
    except KeyboardInterrupt:
        print()
        return 130
    errors = config_mod.validate(cfg)
    if errors:
        for e in errors:
            ui.error(f"config: {e}")
        return 2
    ui.blank()
    path = config_mod.save(cfg)
    ui.ok(t("setup.saved", path=path))
    config_mod.make_data_dirs(cfg)
    pairing.get_token(create=True)
    store_api_key(cfg, api_key)

    port = int(cfg["server"]["port"])
    service_ok = False
    if not args.no_service:
        try:
            with ui.step(t("setup.service_installing")):
                where = ServiceManager().install()
            ui.ok(t("setup.service_installed", where=where))
            with ui.step(t("setup.server_waiting")):
                service_ok = wait_for_server(port, 20)
            (ui.ok if service_ok else ui.warn)(t("setup.server_up", port=port) if service_ok
                                               else t("setup.server_down", port=port))
        except Exception as e:  # noqa: BLE001
            ui.fail(t("setup.service_failed", error=e))
    else:
        service_ok = bool(client.health(port))

    try:
        if service_ok or args.skip_extension or not interactive:
            extension_step(cfg, args, p)
    except KeyboardInterrupt:
        ui.say(t("setup.ext_skipped"))
    ui.blank()
    ui.say(t("setup.done"))
    return 0
