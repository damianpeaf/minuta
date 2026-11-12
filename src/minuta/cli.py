"""The `minuta` command. Without arguments it opens a menu; every action also works with flags."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from minuta import __version__, client, credentials, pairing, paths
from minuta import config as config_mod
from minuta.i18n import set_language, t
from minuta.meetings import DISCARDED, MeetingStore
from minuta.redact import redact, setup_logging

DEFAULT_REPO = "https://github.com/damianpeaf/minuta"
# The Python the installers (install.sh, install.ps1) give the tool environment, overridable with
# MINUTA_PYTHON as there. `minuta update` pins it too, so uv does not switch to another interpreter.
DEFAULT_PYTHON = "3.12"
# Left in the source folder by the installers: only such a folder may be replaced or reset.
INSTALLER_MARKER = ".minuta-installer"
_TAR_FILTER = {"filter": "data"} if hasattr(__import__("tarfile"), "data_filter") else {}


# --- helpers -------------------------------------------------------------------------
def load_cfg(required: bool = True) -> dict | None:
    if not config_mod.exists():
        if required:
            print(t("no_config"), file=sys.stderr)
            return None
        return config_mod.load()
    cfg = config_mod.load()
    set_language(cfg["general"].get("language"))
    return cfg


def store_for(cfg: dict) -> MeetingStore:
    return MeetingStore(config_mod.meetings_dir(cfg))


def router_for(cfg: dict):
    from minuta.router import Router

    return Router(cfg, credentials.store(cfg))


def interactive(args) -> bool:
    return sys.stdin.isatty() and not getattr(args, "non_interactive", False)


def fmt_minutes(sec) -> str:
    return f"{round((sec or 0) / 60)} min" if sec else "-"


# --- commands ------------------------------------------------------------------------
def cmd_setup(args) -> int:
    from minuta import setup

    return setup.run(args)


def cmd_serve(args) -> int:
    # Stopping is a clean exit: launchd (KeepAlive on failure) would restart a server killed by SIGTERM.
    # Set first, so a stop while the server is still starting is clean too.
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    if args.home:
        os.environ["MINUTA_HOME"] = str(Path(args.home).expanduser())
    from minuta.service import pid_file

    try:
        from minuta.server import Server

        cfg = config_mod.load()
        setup_logging(logfile=paths.log_dir() / "server.log")
        srv = Server(cfg, port=args.port)
        paths.write_private(pid_file(), str(os.getpid()))
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pid_file().unlink(missing_ok=True)
    return 0


def _stop_on_sigterm(_signum, _frame) -> None:
    raise KeyboardInterrupt


def cmd_status(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta.service import ServiceManager

    port = int(cfg["server"]["port"])
    svc = ServiceManager().status()
    health = client.health(port)
    st = client.status(port) if health else None
    state = pairing.read_state()
    store = store_for(cfg)
    meetings = [m for m in store.all() if m.status != DISCARDED]
    router = router_for(cfg)
    failed = sum(1 for m in meetings if router.failed(m))
    last_seen = (st or {}).get("last_seen") or state.get("last_seen")
    out = {
        "version": __version__,
        "config": str(paths.config_file()),
        "data": str(config_mod.data_dir(cfg)),
        "service": {"installed": svc.installed, "running": svc.running, "detail": svc.detail},
        "server": {"port": port, "answering": bool(health)},
        "extension": {"paired": bool(state.get("allowed_origins")),
                      "last_seen_seconds_ago": round(time.time() - last_seen) if last_seen else None},
        "recording_now": (st or {}).get("recording", []),
        "meetings": {"total": len(meetings), "without_transcript": sum(1 for m in meetings if not m.has_transcript()),
                     "with_failed_delivery": failed},
    }
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"minuta {__version__}")
    print(f"  config      {out['config']}")
    print(f"  data        {out['data']}")
    print(f"  service     {'installed' if svc.installed else 'not installed'}, {svc.detail or '-'}")
    print(f"  server      127.0.0.1:{port} {'answering' if health else 'not answering'}")
    seen = out["extension"]["last_seen_seconds_ago"]
    print(f"  extension   {'paired' if out['extension']['paired'] else 'not paired'}"
          f"{f', last seen {seen} s ago' if seen is not None else ''}")
    if out["recording_now"]:
        print(f"  recording   {', '.join(out['recording_now'])}")
    m = out["meetings"]
    print(f"  meetings    {m['total']} ({m['without_transcript']} without transcript, "
          f"{m['with_failed_delivery']} with failed deliveries)")
    return 0


def cmd_list(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    rows = []
    for m in store_for(cfg).all():
        d = m.read()
        if d.get("status") == DISCARDED and not args.all:
            continue
        rows.append({"id": m.id, "title": m.title, "started_at": d.get("started_at"), "status": d.get("status"),
                     "duration_seconds": d.get("duration_seconds"), "transcript": m.has_transcript(),
                     "audio": m.audio_path is not None,
                     "destinations": {k: {s: (v or {}).get("status") for s, v in (st or {}).items()}
                                      for k, st in (d.get("destinations") or {}).items()}})
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(t("list.empty"))
        return 0
    for r in rows:
        tr = "transcript" if r["transcript"] else "no transcript"
        failed = [f"{name}:{stage}" for name, stages in r["destinations"].items()
                  for stage, status in stages.items() if status == "failed"]
        print(f"{r['id']}  {(r['started_at'] or '')[:16]:16}  {fmt_minutes(r['duration_seconds']):>7}  "
              f"{r['status'] or '-':10}  {tr:13}  {r['title']}"
              + (f"  [failed: {', '.join(failed)}]" if failed else ""))
    return 0


def ask():
    from minuta.setup import Prompter

    return Prompter(True)


def _pick_meeting(store: MeetingStore) -> str | None:
    meetings = [m for m in store.all() if m.status not in (DISCARDED, "recording")]
    if not meetings:
        print(t("list.empty"))
        return None
    choices = [(m.id, f"{m.id}  {m.title}{'' if m.has_transcript() else '  *'}") for m in reversed(meetings)]
    return ask().select(t("transcribe.pick"), choices, choices[0][0])


def cmd_transcribe(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta.transcription import price_per_minute
    from minuta.transcription.budget import Budget, BudgetExceeded
    from minuta.transcription.pipeline import estimate_run, transcribe

    store = store_for(cfg)
    ref = args.meeting
    if ref is None:
        ref = _pick_meeting(store) if interactive(args) else "latest"
        if ref is None:
            return 1
    try:
        meeting = store.resolve(ref)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    if meeting.has_transcript() and not args.force and not args.estimate:
        print(t("transcribe.exists", id=meeting.id))
        return 0
    engine = args.engine or cfg["transcription"]["engine"]
    window = None
    if args.from_min is not None or args.to_min is not None:
        window = ((args.from_min or 0) * 60, (args.to_min if args.to_min is not None else 10**6) * 60)
    router = router_for(cfg)
    cap = float(args.max_usd if args.max_usd is not None else cfg["transcription"].get("max_usd") or 0)
    budget = None
    if price_per_minute(engine, cfg) > 0:
        est = estimate_run(meeting, cfg, engine, router=router, window=window)
        print(t("transcribe.estimate", engine=engine, minutes=est["seconds"] / 60, usd=est["usd"]))
        if args.estimate:
            return 0
        if est["usd"] > cap:
            print(t("transcribe.over_cap", cap=cap))
            return 3
        if not args.yes:
            if not interactive(args):
                print(t("transcribe.need_yes"))
                return 3
            if not ask().confirm(t("transcribe.confirm", usd=est["usd"]), False):
                print(t("transcribe.cancelled"))
                return 1
        budget = Budget(cap)
    elif args.estimate:
        print(t("transcribe.estimate", engine=engine, minutes=0.0, usd=0.0))
        return 0
    setup_logging()
    try:
        transcribe(meeting, cfg, engine, router=router, secrets=credentials.store(cfg), budget=budget, window=window)
    except BudgetExceeded as e:
        print(e, file=sys.stderr)
        return 3
    print(t("transcribe.done", path=meeting.dir / "transcript.md"))
    return 0


def cmd_destinations(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import destinations as reg

    dests = cfg.setdefault("destinations", {})
    action = args.action or "list"
    if action == "list":
        kinds = reg.registry()
        if args.json:
            print(json.dumps({"configured": dests, "types": sorted(kinds)}, indent=2))
            return 0
        for name, opts in dests.items():
            state = "enabled" if opts.get("enabled", True) else "disabled"
            extra = {k: v for k, v in opts.items() if k not in ("type", "enabled", "send")}
            print(f"{name:12} {opts.get('type'):10} {state:9} sends {','.join(opts.get('send') or [])}  {extra or ''}")
        print("available types: " + ", ".join(f"{k} ({c.describe()})" for k, c in sorted(kinds.items())))
        return 0
    if not args.name:
        print("a destination name is required", file=sys.stderr)
        return 2
    if action == "add":
        kind = args.type or args.name
        if kind not in reg.registry():
            print(f"unknown type {kind!r}; available: {', '.join(sorted(reg.registry()))}", file=sys.stderr)
            return 2
        from minuta.setup import Cancelled, Prompter, add_destination, parse_options

        try:
            opts = parse_options(args.option)
            if args.send:
                opts["send"] = args.send
            ok, message = add_destination(cfg, args.name, kind, opts, Prompter(True) if interactive(args) else None,
                                          force=args.force)
        except KeyboardInterrupt:
            print()
            return 130
        except Cancelled:
            print(f"{args.name}: not added", file=sys.stderr)
            return 1
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        if not ok:
            print(redact(f"{args.name}: {message} (use --force to save anyway)"), file=sys.stderr)
            return 1
        config_mod.save(cfg)
        print(redact(f"{args.name}: added. {message}"))
        return 0
    if args.name not in dests:
        print(f"no destination named {args.name!r}", file=sys.stderr)
        return 1
    if action == "remove":
        reg.forget_secrets(dests.pop(args.name), credentials.store(cfg))
        config_mod.save(cfg)
        print(f"{args.name}: removed")
        return 0
    if action in ("enable", "disable"):
        dests[args.name]["enabled"] = action == "enable"
        config_mod.save(cfg)
        print(f"{args.name}: {action}d")
        return 0
    if action == "test":
        try:
            dest = reg.create(args.name, dests[args.name], credentials.store(cfg))
            check = dest.check()
        except Exception as e:  # noqa: BLE001
            print(f"{args.name}: {redact(str(e))}")
            return 1
        print(f"{args.name}: {'ok' if check.ok else 'problem'} {check.message}")
        if not check.ok or not args.live:
            return 0 if check.ok else 1
        ping = getattr(dest, "ping", None)
        if ping is None:
            print(f"{args.name}: {dests[args.name].get('type')} has no live test")
            return 0
        try:
            receipt = ping()
        except Exception as e:  # noqa: BLE001
            print(f"{args.name}: live test failed: {redact(str(e))}")
            return 1
        print(f"{args.name}: live test ok {receipt.url or ''}".rstrip())
        return 0
    return 2


def cmd_retry(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    setup_logging()
    store = store_for(cfg)
    router = router_for(cfg)
    if args.meetings:
        try:
            targets = [store.resolve(r) for r in args.meetings]
        except LookupError as e:
            print(e, file=sys.stderr)
            return 1
    else:
        targets = [m for m in store.all() if m.status == "ready" and router.needs_delivery(m)]
    if not targets:
        print(t("retry.none"))
        return 0
    worst = 0
    for m in targets:
        results = router.retry(m)
        for key, st in results.items():
            print(f"{m.id}  {key:24} {st.get('status')}  {st.get('error') or ''}")
            if st.get("status") == "failed":
                worst = 1
    return worst


def cmd_pair(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    if args.rotate:
        pairing.rotate_token()
    code = pairing.new_code()
    print(t("pair.code", code=code))
    if args.wait:
        from minuta.setup import wait_for_extension

        ok = wait_for_extension(int(cfg["server"]["port"]), args.wait, time.time() - 1)
        print(t("setup.ext_ok") if ok else t("setup.ext_timeout"))
        return 0 if ok else 1
    return 0


def cmd_doctor(args) -> int:
    from minuta import doctor

    cfg = load_cfg(required=False)
    return doctor.run(cfg, as_json=args.json)


def cmd_update(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    g = cfg["general"]
    app_dir = g.get("app_dir")
    if not app_dir or not Path(app_dir).exists():
        print(t("update.no_source"))
        return 1
    app = Path(app_dir)
    ref = g.get("install_ref") or ""
    if (app / ".git").exists():
        code = _git_update(app, ref)
        if code != 0:
            return code
    else:
        try:
            _download_tarball(os.environ.get("MINUTA_REPO_URL", DEFAULT_REPO), ref, app)
        except (OSError, ValueError) as e:
            print(f"update: {e}", file=sys.stderr)
            return 1
    uv = shutil.which("uv")
    if not uv:
        print("uv is not installed", file=sys.stderr)
        return 1
    extras = g.get("install_extras", "local")
    spec = f".[{extras}]" if extras else "."
    python = os.environ.get("MINUTA_PYTHON") or DEFAULT_PYTHON
    argv = [uv, "tool", "install", "--force", "--reinstall", "--python", python]
    if (app / "constraints.txt").exists():  # the versions pinned in uv.lock
        argv += ["--constraints", "constraints.txt"]
    # Relative paths from inside the checkout: uv splits a --constraints path at spaces.
    r = subprocess.run([*argv, spec], text=True, cwd=app)
    if r.returncode != 0:
        return r.returncode
    if not args.no_restart:
        from minuta.service import ServiceManager

        mgr = ServiceManager()
        if mgr.status().installed:
            mgr.restart()
    print(t("update.done"))
    return 0


def _git_update(app: Path, ref: str) -> int:
    """The installers' checkout follows the branch or tag it was installed from (a tag leaves HEAD
    detached). A checkout without the installer's marker is the user's own: only fast-forwarded."""
    git = ["git", "-C", str(app)]
    if not (app / INSTALLER_MARKER).exists():
        return subprocess.run([*git, "pull", "--ff-only"], text=True).returncode
    for step in (["fetch", "--depth", "1", "origin", ref or "HEAD"], ["reset", "--hard", "FETCH_HEAD"]):
        r = subprocess.run([*git, *step], text=True)
        if r.returncode != 0:
            return r.returncode
    return 0


def _download_tarball(repo: str, ref: str, dest: Path) -> None:
    """Replace `dest` with the source of `ref` (a branch or a tag; default main) in one rename, so
    no file of the old version is left behind and a failed download changes nothing."""
    import io
    import tarfile
    import tempfile
    import urllib.request

    if dest.exists() and any(dest.iterdir()) and not (dest / INSTALLER_MARKER).exists():
        raise ValueError(f"{dest} was not created by the installer; update it yourself")
    url = f"{repo.rstrip('/')}/archive/{ref or 'main'}.tar.gz"
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - https URL of the project
        data = r.read()
    new = Path(tempfile.mkdtemp(prefix=f".{dest.name}-new-", dir=dest.parent))
    old = dest.with_name(f".{dest.name}-old")
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                name = member.name.split("/", 1)[1] if "/" in member.name else ""
                if not name or not (member.isfile() or member.isdir()) or ".." in Path(name).parts \
                        or Path(name).is_absolute():
                    continue
                member.name = name
                tar.extract(member, new, **_TAR_FILTER)  # noqa: S202 - names checked above
        (new / INSTALLER_MARKER).touch()
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            os.replace(dest, old)
        os.replace(new, dest)
    finally:
        shutil.rmtree(new, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)


def cmd_uninstall(args) -> int:
    cfg = config_mod.load()
    set_language(cfg["general"].get("language"))
    asking = interactive(args) and not args.yes
    if asking and not ask().confirm(t("uninstall.confirm"), False):
        return 1
    from minuta.service import ServiceManager

    ServiceManager().uninstall()
    data = config_mod.data_dir(cfg)
    delete_data = args.delete_data
    if delete_data is None:
        delete_data = asking and ask().confirm(t("uninstall.data", path=data), False)
    from minuta.destinations import forget_secrets
    from minuta.transcription.vercel import KEY_NAME

    store = credentials.store(cfg)
    store.delete(KEY_NAME)
    for opts in (cfg.get("destinations") or {}).values():
        forget_secrets(opts, store)
    _remove_config(keep=[config_mod.meetings_dir(cfg), Path(cfg['general'].get('app_dir') or paths.default_app_dir())])
    logs = paths.log_dir()
    paths.remove_tree(logs)
    if logs.parent.name == "minuta" and logs.parent.exists() and not any(logs.parent.iterdir()):
        logs.parent.rmdir()
    leftovers: list[Path] = []
    if delete_data:
        paths.remove_tree(data)
        leftovers.append(data)
        default = paths.default_data_dir()
        if default.exists() and not any(p for p in default.iterdir() if p.name != "app"):
            paths.remove_tree(default)
            leftovers.append(default)
    else:
        print(t("uninstall.kept", path=data))
    app_dir = cfg["general"].get("app_dir")
    if app_dir and (Path(app_dir) / INSTALLER_MARKER).exists():
        paths.remove_tree(Path(app_dir))
        leftovers.append(Path(app_dir))
    for d in (paths.config_dir(), paths.default_data_dir()):
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    if not args.keep_tool:
        _uninstall_tool([d for d in leftovers if d.exists()])
    print(t("uninstall.done"))
    return 0


def _remove_config(keep: list[Path]) -> None:
    """Delete the config folder. On macOS it is also the data folder (Application Support/minuta), so
    the meetings and the source checkout inside it are left for the data and app steps to decide."""
    cfg_dir = paths.config_dir()
    if not cfg_dir.exists():
        return
    root = cfg_dir.resolve()
    keep = [k.resolve() for k in keep]
    if not any(k == root or root in k.parents for k in keep):
        paths.remove_tree(cfg_dir)
        return
    for child in cfg_dir.iterdir():
        c = child.resolve()
        if any(c == k or c in k.parents for k in keep):
            continue
        if child.is_dir() and not child.is_symlink():
            paths.remove_tree(child)
        else:
            child.unlink(missing_ok=True)
    if not any(cfg_dir.iterdir()):
        cfg_dir.rmdir()


def _uninstall_tool(leftovers: list[Path]) -> None:
    """Remove the `uv tool` installation when we are running from one (not from a dev checkout)."""
    uv = shutil.which("uv")
    prefix = Path(sys.prefix).as_posix()
    if not uv or "/uv/tools/minuta" not in prefix.replace("\\", "/"):
        return
    if os.name == "nt":
        _uninstall_tool_after_exit(uv, leftovers)
        print(t("uninstall.finishing"))
        return
    subprocess.run([uv, "tool", "uninstall", "minuta"], capture_output=True)


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ps_list(values: list[Path]) -> str:
    return "@(" + ", ".join(_ps_quote(v) for v in values) + ")"


def windows_uninstall_script(pid: int, uv: str, targets: list[Path], leftovers: list[Path], log: Path) -> str:
    """PowerShell that waits for `pid` and the minuta.exe launcher in front of it to exit, then runs
    `uv tool uninstall minuta` until the tool environment and its commands (`targets`) are gone, and
    removes the folders the uninstall could not delete while running. The log stays only on failure."""
    return f"""$ErrorActionPreference = 'SilentlyContinue'
$targets = {_ps_list(targets)}
$log = {_ps_quote(log)}
"waiting for {pid}" | Out-File -LiteralPath $log -Encoding utf8
Wait-Process -Id {pid} -Timeout 120
Get-Process | Where-Object {{ $targets -contains $_.Path }} | Wait-Process -Timeout 30
for ($i = 0; $i -lt 30 -and ($targets | Where-Object {{ Test-Path -LiteralPath $_ }}); $i++) {{
  & {_ps_quote(uv)} tool uninstall minuta *>> $log
  foreach ($f in $targets) {{
    if (Test-Path -LiteralPath $f) {{ Remove-Item -LiteralPath $f -Recurse -Force *>> $log }}
  }}
  if ($targets | Where-Object {{ Test-Path -LiteralPath $_ }}) {{ Start-Sleep -Seconds 1 }}
}}
foreach ($d in {_ps_list(leftovers)}) {{
  if (Test-Path -LiteralPath $d) {{ Remove-Item -LiteralPath $d -Recurse -Force *>> $log }}
}}
if (-not ($targets + {_ps_list(leftovers)} | Where-Object {{ Test-Path -LiteralPath $_ }})) {{
  Remove-Item -LiteralPath $log -Force
}}
if ($PSCommandPath) {{ Remove-Item -LiteralPath $PSCommandPath -Force }}
"""


def _uninstall_tool_after_exit(uv: str, leftovers: list[Path]) -> None:
    """Windows does not let the running minuta.exe delete itself, so a hidden PowerShell finishes the job
    once this process has exited."""
    targets = [Path(sys.prefix)]
    r = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        targets += [Path(r.stdout.strip()) / name for name in ("minuta.exe", "minuta-server.exe")]
    temp = Path(os.environ.get("TEMP") or Path.home())
    script = temp / f"minuta-uninstall-{os.getpid()}.ps1"
    script.write_text(windows_uninstall_script(os.getpid(), uv, targets, leftovers, temp / "minuta-uninstall.log"),
                      encoding="utf-8-sig")
    if not start_detached_powershell(script):
        script.unlink(missing_ok=True)
        print(t("uninstall.finish_by_hand"))


def start_detached_powershell(script: Path) -> bool:
    """Run a PowerShell script hidden, in a process that outlives this one. It is started through WMI:
    a child of ours would sit in the job of the minuta.exe launcher, which ends its processes with it."""
    command_line = ("powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden "
                    f'-File "{script}"')
    create = ("$ErrorActionPreference = 'Stop'; $c = Get-CimClass Win32_ProcessStartup; "
              "$si = New-CimInstance -CimClass $c -ClientOnly -Property @{ShowWindow = [uint16]0}; "
              "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              f"-Arguments @{{CommandLine = {_ps_quote(command_line)}; ProcessStartupInformation = $si}}; "
              "if ($r.ReturnValue -eq 0) { exit 0 } else { exit 1 }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", create],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def cmd_service(args) -> int:
    from minuta.service import ServiceManager

    mgr = ServiceManager()
    action = args.action
    if action == "install":
        print(mgr.install())
    elif action == "uninstall":
        mgr.uninstall()
    elif action == "start":
        mgr.start()
    elif action == "stop":
        mgr.stop()
    elif action == "restart":
        mgr.restart()
    st = mgr.status()
    if action in ("start", "stop", "restart"):
        # launchctl returns before the job changes state: give it a moment before reporting.
        end = time.time() + 5
        while st.running != (action != "stop") and time.time() < end:
            time.sleep(0.2)
            st = mgr.status()
    print(f"service: {'installed' if st.installed else 'not installed'}, {'running' if st.running else 'stopped'}"
          f" ({st.detail or '-'})")
    return 0


def cmd_menu(_args) -> int:
    cfg = load_cfg(required=False)
    if not config_mod.exists():
        ns = build_parser().parse_args(["setup"])
        return cmd_setup(ns)
    set_language((cfg or {}).get("general", {}).get("language"))
    actions = [("status", ["status"]), ("list", ["list"]), ("transcribe", ["transcribe"]),
               ("destinations", ["destinations", "list"]), ("retry", ["retry"]), ("doctor", ["doctor"]),
               ("pair", ["pair"]), ("setup", ["setup"]), ("update", ["update"])]
    choices = [(k, t(f"menu.{k}")) for k, _ in actions] + [("quit", t("menu.quit"))]
    while True:
        try:
            choice = ask().select(t("menu.title"), choices, "status")
        except KeyboardInterrupt:
            return 0
        if choice == "quit":
            return 0
        argv = dict(actions)[choice]
        ns = build_parser().parse_args(argv)
        ns.func(ns)
        print()


# --- parser --------------------------------------------------------------------------
class _DestinationOption(argparse.Action):
    """`minuta setup --destination webhook --option url=...`: an option belongs to the destination before it."""

    def __call__(self, parser, namespace, value, option_string=None):
        if not getattr(namespace, "destination", None):
            parser.error("--option goes after the --destination it belongs to")
        namespace.destination[-1]["options"].append(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minuta", description="Record Google Meet calls locally and transcribe them.")
    p.add_argument("--version", action="version", version=f"minuta {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("setup", help="configure minuta, install the service and pair the extension")
    s.add_argument("--non-interactive", action="store_true", help="no questions; flags and defaults only")
    s.add_argument("--language", choices=["en", "es"])
    s.add_argument("--name", help="your name in transcripts")
    s.add_argument("--others-name", help="label for everyone else (default: Others)")
    s.add_argument("--data-dir")
    s.add_argument("--audio-format", choices=["opus", "mp3"])
    s.add_argument("--delete-audio-after-upload", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--engine", choices=["local", "vercel"])
    s.add_argument("--vercel-model")
    s.add_argument("--local-model", help="local model: auto, tiny, base, small, medium, large-v3, large-v3-turbo")
    s.add_argument("--api-key-env", help="read the Vercel AI Gateway key from this environment variable")
    s.add_argument("--max-usd", type=float)
    s.add_argument("--transcription-mode", choices=["on-demand", "auto"])
    s.add_argument("--transcription-language")
    s.add_argument("--port", type=int)
    s.add_argument("--app-dir", help="source checkout used by `minuta update` (set by the installers)")
    s.add_argument("--install-extras", help=argparse.SUPPRESS)
    s.add_argument("--install-ref", help=argparse.SUPPRESS)
    s.add_argument("--secrets-backend", choices=["auto", "file"])
    s.add_argument("--destination", action="append", metavar="TYPE[:NAME]", type=lambda v: {"spec": v, "options": []},
                   help="add or change a destination (name: the type); repeat for more")
    s.add_argument("--option", action=_DestinationOption, metavar="KEY=VALUE",
                   help="an option of the --destination before it, as in `destinations add`; repeat for more")
    s.add_argument("--no-service", action="store_true", help="do not install the background service")
    s.add_argument("--no-browser", action="store_true", help="do not open the browser's extensions page")
    s.add_argument("--skip-extension", action="store_true", help="do not wait for the extension")
    s.add_argument("--wait-extension", type=int, metavar="SECONDS", help="wait this long for the extension")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("serve", help="run the local server in the foreground (the service runs this)")
    s.add_argument("--home", help=argparse.SUPPRESS)
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("status", help="service, extension and meetings at a glance")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="list meetings")
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="include discarded meetings")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("transcribe", help="transcribe a meeting")
    s.add_argument("meeting", nargs="?", help="meeting id, id prefix or 'latest'")
    s.add_argument("--engine", choices=["local", "vercel"])
    s.add_argument("--max-usd", type=float, help="spending cap for this run")
    s.add_argument("--yes", action="store_true", help="do not ask before spending")
    s.add_argument("--estimate", action="store_true", help="only show the estimated cost")
    s.add_argument("--from", dest="from_min", type=float, metavar="MIN", help="start at this minute")
    s.add_argument("--to", dest="to_min", type=float, metavar="MIN", help="stop at this minute")
    s.add_argument("--force", action="store_true", help="transcribe again")
    s.add_argument("--non-interactive", action="store_true")
    s.set_defaults(func=cmd_transcribe)

    s = sub.add_parser("destinations", help="list, add, test or remove destinations")
    s.add_argument("action", nargs="?", choices=["list", "add", "test", "remove", "enable", "disable"])
    s.add_argument("name", nargs="?")
    s.add_argument("--type", help="destination type (default: same as the name)")
    s.add_argument("--send", help="comma-separated: audio,transcript,metadata")
    s.add_argument("--option", action="append", metavar="KEY=VALUE")
    s.add_argument("--force", action="store_true", help="save even if the check fails")
    s.add_argument("--live", action="store_true", help="test: also send a real test event (webhook)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--non-interactive", action="store_true", help="no questions; --option values only")
    s.set_defaults(func=cmd_destinations)

    s = sub.add_parser("retry", help="retry failed deliveries")
    s.add_argument("meetings", nargs="*")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("pair", help="new pairing code for the browser extension")
    s.add_argument("--wait", type=int, metavar="SECONDS")
    s.add_argument("--rotate", action="store_true", help="new server token; every extension must pair again")
    s.set_defaults(func=cmd_pair)

    s = sub.add_parser("doctor", help="check the installation")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("update", help="get the source again (same branch or tag), reinstall and restart the service")
    s.add_argument("--no-restart", action="store_true")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("uninstall", help="remove the service and configuration (and optionally the data)")
    s.add_argument("--yes", action="store_true", help="do not ask")
    s.add_argument("--delete-data", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--keep-tool", action="store_true", help="do not run `uv tool uninstall minuta`")
    s.add_argument("--non-interactive", action="store_true")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("service", help="manage the background service")
    s.add_argument("action", choices=["start", "stop", "status", "restart", "install", "uninstall"])
    s.set_defaults(func=cmd_service)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_menu(args)
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


def serve_main() -> int:
    """Entry point without a console window (Windows); same as `minuta serve`."""
    return main(["serve", *sys.argv[1:]])
