"""The `minuta` command. Without arguments it opens a menu; every action also works with flags.

What each action does lives in `actions` (data); this module asks, prints and translates.
"""

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

from minuta import __version__, paths
from minuta import config as config_mod
from minuta.i18n import set_language, t

DEFAULT_REPO = "https://github.com/damianpeaf/minuta"
# The Python the installers (install.sh, install.ps1) give the tool environment, overridable with
# MINUTA_PYTHON as there. `minuta update` pins it too, so uv does not switch to another interpreter.
DEFAULT_PYTHON = "3.12"
# Left in the source folder by the installers: only such a folder may be replaced or reset.
INSTALLER_MARKER = ".minuta-installer"
_TAR_FILTER = {"filter": "data"} if hasattr(__import__("tarfile"), "data_filter") else {}
DISCARDED = "discarded"


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


def interactive(args) -> bool:
    return sys.stdin.isatty() and not getattr(args, "non_interactive", False)


def ask():
    from minuta.setup import Prompter

    return Prompter(True)


def cli_logging(verbose: bool = False) -> None:
    """Logs go to a file; the terminal gets messages written for people (or the log too, with --verbose)."""
    from minuta.redact import setup_logging

    setup_logging(logfile=paths.log_dir() / "cli.log", stream=verbose)


def resolve_meeting(store, ref: str):
    from minuta.meetings import MeetingLookupError

    try:
        return store.resolve(ref)
    except MeetingLookupError as e:
        from minuta import ui

        ui.error(t(e.key, **e.kw))
        return None


def status_word(status: str | None) -> str:
    return t(f"meeting.status.{status}") if status else "-"


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
    from minuta.redact import setup_logging
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
    from minuta import actions, ui

    if args.json:
        print(json.dumps(actions.status(cfg), indent=2))
        return 0
    with ui.step(t("status.checking")):
        out = actions.status(cfg)
    print_status(out)
    return 0


def print_status(out: dict) -> None:
    from minuta import ui

    svc, srv, ext, m = out["service"], out["server"], out["extension"], out["meetings"]
    yes = lambda good, text: ui.styled(text, "ok" if good else "warn")  # noqa: E731
    service = t("status.service_running") if svc["running"] else (
        t("status.service_stopped") if svc["installed"] else t("status.service_missing"))
    server = t("status.server_up", port=srv["port"]) if srv["answering"] else t("status.server_down", port=srv["port"])
    if ext["paired"]:
        seen = ext["last_seen_seconds_ago"]
        extension = t("status.ext_seen", ago=t("ago", what=ui.duration(max(1, seen)))) if seen is not None \
            else t("status.ext_paired")
    else:
        extension = t("status.ext_unpaired")
    rows = [
        (t("status.version"), f"minuta {out['version']}"),
        (t("status.service"), yes(svc["running"], service)),
        (t("status.server"), yes(srv["answering"], server)),
        (t("status.extension"), yes(ext["paired"], extension)),
        (t("status.mode"), t(f"mode.{out['recording_mode']}")),
    ]
    if out["recording_now"]:
        rows.append((t("status.recording"), t("status.recording_now", n=len(out["recording_now"]))))
    rows += [
        (t("status.meetings"), t("status.meetings_value", total=m["total"], pending=m["without_transcript"])),
    ]
    if m["with_failed_delivery"]:
        rows.append((t("status.failed"), ui.styled(t("status.failed_value", n=m["with_failed_delivery"]), "fail")))
    rows += [(t("status.data"), out["data"]), (t("status.config"), out["config"])]
    ui.kv(rows)


def cmd_list(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import actions, ui

    rows = actions.meetings(cfg, include_discarded=args.all)
    if args.json:
        for r in rows:
            r.pop("code", None)  # same shape as before
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        ui.say(t("list.empty"))
        return 0
    rows = list(reversed(rows))  # newest first
    short = actions.short_ids([r["id"] for r in rows], minimum=64 if args.verbose else 16)
    table = []
    for r in rows:
        failed = [f"{name}" for name, stages in r["destinations"].items()
                  for status in stages.values() if status == "failed"]
        tr = ui.styled(t("list.transcribed"), "ok") if r["transcript"] else ui.styled(t("list.not_transcribed"), "dim")
        state = status_word(r["status"])
        if failed:
            state += " · " + t("list.failed_to", names=", ".join(dict.fromkeys(failed)))
        table.append([ui.when(r["started_at"]), r["title"], ui.duration(r["duration_seconds"]), state, tr,
                      ui.styled(short[r["id"]], "dim")])
    ui.table([t("list.col.when"), t("list.col.title"), t("list.col.duration"), t("list.col.status"),
              t("list.col.transcript"), t("list.col.id")], table)
    ui.say(t("list.hint"), style="dim")
    return 0


def _meeting_label(m) -> str:
    from minuta import ui

    d = m.read()
    tr = t("list.transcribed") if m.has_transcript() else t("list.not_transcribed")
    return f"{ui.when(d.get('started_at'))}   {m.title}   ({ui.duration(d.get('duration_seconds'))}, {tr})"


def _pick_meeting(cfg: dict):
    from minuta import actions, ui

    meetings = actions.transcribable(cfg)
    if not meetings:
        ui.say(t("list.empty"))
        return None
    choices = [(m.id, _meeting_label(m)) for m in meetings]
    first = next((m.id for m in meetings if not m.has_transcript()), meetings[0].id)
    return ask().select(t("transcribe.pick"), choices, first)


def _engine_label(o: dict) -> str:
    from minuta import ui

    name = "local" if o["engine"] == "local" else o["model"]
    model = f" ({o['model']})" if o["engine"] == "local" and o.get("model") else ""
    when = t("transcribe.takes", time=ui.duration(o["seconds"])) if o.get("seconds") else ""
    price = t("transcribe.up_to", usd=o["usd"]) if o["paid"] else t("transcribe.free")
    extra = "" if o.get("available", True) else " - " + t("transcribe.not_installed")
    return f"{name}{model}: {price}{', ' + when if when else ''}{extra}"


def choose_engine(cfg: dict, meeting, args) -> tuple[str, str | None] | None:
    """The engine and model to use: from --engine, or asked in a terminal with the time and price of each."""
    from minuta import actions, ui

    if args.engine:
        return args.engine, None
    if not interactive(args):
        return cfg["transcription"]["engine"], None
    with ui.step(t("transcribe.options")):
        options = actions.engine_options(cfg, meeting)
    usable = [o for o in options if o.get("available", True)] or options
    if len(usable) == 1:
        o = usable[0]
        ui.say(t("transcribe.engine", engine=_engine_label(o)))
        return o["engine"], o.get("model") if o["engine"] == "vercel" else None
    choices = [(str(i), _engine_label(o)) for i, o in enumerate(usable)]
    picked = usable[int(ask().select(t("transcribe.which_engine"), choices, "0"))]
    return picked["engine"], picked.get("model") if picked["engine"] == "vercel" else None


class TranscriptionView:
    """Turns pipeline progress into lines: a finished step gets an 'ok' line, the current one a bar.
    The same view shows a job of the background service (`follow_job`)."""

    def __init__(self):
        self.step = None
        self.bar = None
        self.info: dict = {}

    def __call__(self, step: str, fraction, info: dict) -> None:
        from minuta import actions, ui

        if step != self.step:
            self.close()
            if step == "done":
                return
            self.step, self.info = step, dict(info)
            label = actions.step_label("transcribe", step, info) or step
            self.bar = ui.Bar(label, unit="bytes" if step == "download" else "")
            self.bar.__enter__()
        self.info.update(info)
        if self.bar is not None and fraction is not None:
            if step == "download" and info.get("total"):
                self.bar.update(info.get("done", 0), info["total"])
            else:
                self.bar.update(round(fraction * 1000), 1000)

    def close(self, failed: bool = False) -> None:
        from minuta import actions, ui

        if self.bar is not None:
            self.bar.__exit__(None, None, None)
            self.bar = None
            if not failed and self.step in actions.STEP_DONE:
                ui.ok(actions.step_label("transcribe", self.step, self.info, done=True))
        if failed:
            self.step = None


class JobFailed(Exception):
    def __init__(self, job: dict):
        super().__init__(job.get("error") or job.get("state"))
        self.job = job


def follow_job(port: int, job: dict, on_progress) -> dict:
    """Show a job of the background service through `on_progress` until it ends; its result. Ctrl+C
    cancels it there. Steps that passed between two looks are shown too, so no step is skipped."""
    from minuta import client

    seen: list[str] = []
    try:
        while True:
            for step in job.get("steps") or []:
                if step not in seen and step != job.get("step"):
                    seen.append(step)
                    on_progress(step, 1.0, {})
            if job.get("step"):
                if job["step"] not in seen:
                    seen.append(job["step"])
                on_progress(job["step"], job.get("fraction"), job.get("info") or {})
            if job["state"] == "done":
                return job.get("result") or {}
            if job["state"] in ("error", "cancelled"):
                raise JobFailed(job)
            time.sleep(0.4)
            code, body = client.call(port, "GET", f"/jobs/{job['id']}")
            if code != 200:
                raise JobFailed({"state": "error", "error": body.get("error") or f"HTTP {code}"})
            job = body["job"]
    except KeyboardInterrupt:
        client.call(port, "POST", f"/jobs/{job['id']}/cancel")
        raise


def cmd_transcribe(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import actions, ui
    from minuta.transcription import price_per_minute
    from minuta.transcription.budget import BudgetExceeded

    cli_logging(getattr(args, "verbose", False))
    store = actions.store_for(cfg)
    ref = args.meeting
    if ref is None:
        ref = _pick_meeting(cfg) if interactive(args) else "latest"
        if ref is None:
            return 1
    meeting = resolve_meeting(store, ref)
    if meeting is None:
        return 1
    if meeting.has_transcript() and not args.force and not args.estimate:
        ui.say(t("transcribe.exists", title=meeting.title))
        if interactive(args) and getattr(args, "from_menu", False):
            offer_transcript(cfg, meeting)
        return 0
    picked = choose_engine(cfg, meeting, args)
    if picked is None:
        return 1
    engine, model = picked
    window = None
    if args.from_min is not None or args.to_min is not None:
        window = ((args.from_min or 0) * 60, (args.to_min if args.to_min is not None else 10**6) * 60)
    try:
        if price_per_minute(engine, actions.with_engine(cfg, engine, model)) > 0:
            with ui.step(t("transcribe.measuring")):
                plan = actions.plan_transcription(cfg, meeting, engine, model, args.max_usd, window)
        else:
            plan = actions.plan_transcription(cfg, meeting, engine, model, args.max_usd, window)
    except ValueError as e:
        ui.error(str(e))
        return 1
    est = plan["estimate"]
    if plan["paid"]:
        ui.say(t("transcribe.estimate", engine=model or engine, minutes=est["seconds"] / 60, usd=est["usd"]))
        if args.estimate:
            return 0
        if plan["over_cap"]:
            ui.error(t("transcribe.over_cap", cap=plan["cap"]))
            return 3
        if not args.yes:
            if not interactive(args):
                ui.error(t("transcribe.need_yes"))
                return 3
            if not ask().confirm(t("transcribe.confirm", usd=est["usd"]), False):
                ui.say(t("transcribe.cancelled"))
                return 1
    elif args.estimate:
        ui.say(t("transcribe.estimate", engine=engine, minutes=0.0, usd=0.0))
        return 0
    view = TranscriptionView()
    try:
        summary = _transcribe_now(cfg, meeting, plan, args, view)
    except BudgetExceeded as e:
        view.close(failed=True)
        ui.error(str(e))
        return 3
    except JobFailed as e:
        view.close(failed=True)
        if e.job.get("state") == "cancelled":
            ui.say(t("transcribe.job_cancelled"))
            return 1
        if e.job.get("error_kind") == "BudgetExceeded":
            ui.error(e.job["error"])
            return 3
        ui.fail(t("transcribe.failed", error=str(e)[:300]))
        ui.say(t("see_log", path=paths.log_dir() / "server.log"), style="dim")
        return 1
    except KeyboardInterrupt:
        view.close(failed=True)
        raise
    except Exception as e:
        view.close(failed=True)
        ui.fail(t("transcribe.failed", error=str(e)[:300]))
        ui.say(t("see_log", path=paths.log_dir() / "cli.log"), style="dim")
        return 1
    view.close()
    print_summary(summary)
    if interactive(args) and getattr(args, "from_menu", False):
        offer_transcript(cfg, meeting)
    return 0


def _transcribe_now(cfg: dict, meeting, plan: dict, args, view) -> dict:
    """In the background service when it runs (the page shows the same job), else in this process."""
    from minuta import actions, client, ui

    port = client.service(cfg)
    if port is None:
        return actions.run_transcription(cfg, meeting, plan, on_progress=view)
    body = {"engine": plan["engine"], "model": plan["model"], "max_usd": plan["cap"], "confirm": True,
            "window": plan["window"]}
    code, out = client.call(port, "POST", f"/meetings/{meeting.id}/transcribe", body)
    if code != 202:
        raise RuntimeError(out.get("error") or f"HTTP {code}")
    if out["job"].get("position"):
        ui.say(t("transcribe.queued", n=out["job"]["position"]), style="dim")
    return follow_job(port, out["job"], view)


def print_summary(s: dict) -> None:
    from minuta import ui

    ui.blank()
    ui.markup(f"[head]{ui.esc(t('summary.title', title=s['title']))}[/]  [dim]{ui.esc(ui.when(s['started_at']))}[/]")
    speakers = ", ".join(t("summary.turns" if v != 1 else "summary.turn", name=k, n=v)
                         for k, v in s["speakers"].items()) or t("summary.nobody")
    rows: list = [(t("summary.duration"), ui.duration(s["duration_seconds"])), (t("summary.speakers"), speakers)]
    engine = s.get("engine") or {}
    model = str(engine.get("model") or "").rsplit("/", 1)[-1]
    rows.append((t("summary.engine"), f"{engine.get('name')} ({model})" if model else str(engine.get("name"))))
    if s.get("cost_usd"):
        rows.append((t("summary.cost"), f"US${s['cost_usd']:.4f}"))
    for i, f in enumerate(s["files"]):
        label = t("summary.file") if i == 0 else t("summary.also")
        where = t("summary.in_destination", name=f["destination"]) if f["destination"] else t("summary.in_data")
        rows.append((label, _link(f["path"], where)))
    for d in s["deliveries"]:
        good = d["status"] == "ok"
        text = f"{t('mark.ok') if good else t('mark.fail')}  {d['destination']}"
        if not good and d.get("error"):
            text += f": {d['error']}"
        rows.append((t("summary.delivery"), ui.styled(text, "ok" if good else "fail")))
    ui.kv(rows)
    if s["preview"]:
        ui.blank()
        ui.say(t("summary.preview"), style="dim")
        for turn in s["preview"]:
            ui.markup(f"  [dim]{ui.clock(turn['start'])}[/]  [accent]{ui.esc(turn['speaker'])}[/]: "
                      f"{ui.esc(_cut(turn['text'], 160))}")


def _link(path: str, note: str):
    from rich.text import Text

    from minuta import ui

    text = Text.from_markup(ui.path_markup(path))
    text.append(f"  ({note})", style="dim")
    return text


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def offer_transcript(cfg: dict, meeting) -> None:
    """After transcribing from the menu: open the transcript, show it here, or go back."""
    from minuta import actions, ui

    files = actions.transcript_locations(cfg, meeting)
    if not files:
        return
    choice = ask().select(t("summary.next"), [("open", t("summary.open")), ("show", t("summary.show")),
                                              ("back", t("summary.back"))], "open")
    if choice == "open":
        if ui.open_with_system(Path(files[0]["path"])):
            ui.ok(t("summary.opened", path=files[0]["path"]))
        else:
            ui.warn(t("summary.cannot_open", path=files[0]["path"]))
    elif choice == "show":
        doc = json.loads((meeting.dir / "transcript.json").read_text(encoding="utf-8"))
        ui.blank()
        for turn in doc.get("turns") or []:
            ui.markup(f"[dim]{ui.clock(turn['start'])}[/]  [accent]{ui.esc(turn['speaker'])}[/]: "
                      f"{ui.esc(turn['text'])}")


PARTS_KEYS = {"audio": "part.audio", "transcript": "part.transcript", "metadata": "part.metadata"}


def cmd_destinations(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import actions, ui
    from minuta import destinations as reg
    from minuta.redact import redact

    dests = cfg.setdefault("destinations", {})
    action = args.action or "list"
    if action == "list":
        if args.json:
            kinds = reg.registry()
            print(json.dumps({"configured": dests, "types": sorted(kinds)}, indent=2))
            return 0
        data = actions.destinations(cfg)
        rows = []
        for d in data["configured"]:
            state = ui.styled(t("dest.enabled"), "ok") if d["enabled"] else ui.styled(t("dest.disabled"), "dim")
            sends = ", ".join(t(PARTS_KEYS.get(p, p)) for p in d["send"])
            where = d["target"] or (t("dest.data_folder") if d["type"] == "local" else "-")
            rows.append([d["name"], d["type"], state, sends, where])
        if rows:
            ui.table([t("dest.col.name"), t("dest.col.type"), t("dest.col.state"), t("dest.col.sends"),
                      t("dest.col.where")], rows)
        else:
            ui.say(t("dest.none"))
        ui.blank()
        ui.say(t("dest.types"))
        width = max(len(x["type"]) for x in data["types"])
        for x in data["types"]:
            ui.say(f"  {x['type']:<{width}}  {_type_text(x['type'], x['description'])}")
        ui.blank()
        ui.say(t("dest.add_hint"), style="dim")
        return 0
    if not args.name:
        ui.error(t("dest.name_required"))
        return 2
    if action == "add":
        kind = args.type or args.name
        if kind not in reg.registry():
            ui.error(t("dest.unknown_type", kind=kind, known=", ".join(sorted(reg.registry()))))
            return 2
        from minuta.setup import Cancelled, Prompter, add_destination, parse_options

        try:
            opts = parse_options(args.option)
            if args.send:
                opts["send"] = args.send
            with ui.step(t("dest.checking", name=args.name)) if not interactive(args) else _nothing():
                ok, message = add_destination(cfg, args.name, kind, opts,
                                              Prompter(True) if interactive(args) else None, force=args.force)
        except KeyboardInterrupt:
            print()
            return 130
        except Cancelled:
            ui.error(t("dest.not_added", name=args.name))
            return 1
        except ValueError as e:
            ui.error(str(e))
            return 2
        if not ok:
            ui.error(redact(t("dest.check_failed", name=args.name, message=message)))
            return 1
        config_mod.save(cfg)
        ui.ok(redact(t("dest.added", name=args.name, message=message)))
        return 0
    if args.name not in dests:
        ui.error(t("dest.no_such", name=args.name))
        return 1
    if action == "remove":
        actions.remove_destination(args.name)
        ui.ok(t("dest.removed", name=args.name))
        return 0
    if action in ("enable", "disable"):
        actions.set_destination_enabled(args.name, action == "enable")
        ui.ok(t(f"dest.{action}_done", name=args.name))
        return 0
    if action == "test":
        with ui.step(t("dest.live", name=args.name) if args.live else t("dest.testing", name=args.name)):
            r = actions.test_destination(cfg, args.name, live=args.live)
        live = r["live"]  # None when not asked for or when the check failed
        (ui.ok if r["ok"] or live else ui.fail)(f"{args.name}: {r['message']}")
        if live is None:
            return 0 if r["ok"] else 1
        if not live["supported"]:
            ui.say(t("dest.no_live", name=args.name, kind=dests[args.name].get("type")))
            return 0
        if not live["ok"]:
            ui.fail(t("dest.live_failed", name=args.name, error=live["error"]))
            return 1
        ui.ok(t("dest.live_ok", name=args.name, url=live.get("url") or "").rstrip())
        return 0
    return 2


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _type_text(kind: str, description: str) -> str:
    key = f"dest.type.{kind}"
    from minuta.i18n import STRINGS

    return t(key) if key in STRINGS else description


def cmd_retry(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import actions, ui
    from minuta.meetings import MeetingLookupError

    cli_logging(getattr(args, "verbose", False))
    try:
        targets = actions.retry_targets(cfg, args.meetings)
    except MeetingLookupError as e:
        ui.error(t(e.key, **e.kw))
        return 1
    if not targets:
        ui.say(t("retry.none"))
        return 0
    from minuta import client

    port = client.service(cfg)
    with ui.Bar(t("retry.sending", n=len(targets))) as bar:
        progress = lambda step, f, info: bar.update(f or 0, 1)  # noqa: E731
        if port is None:
            rows = actions.retry(cfg, targets, on_progress=progress)
        else:
            code, out = client.call(port, "POST", "/retry", {"meetings": [m.id for m in targets]})
            if code != 202:
                ui.error(out.get("error") or f"HTTP {code}")
                return 1
            try:
                rows = follow_job(port, out["job"], progress)["rows"]
            except JobFailed as e:
                ui.error(str(e))
                return 1
    worst = 0
    table = []
    for r in rows:
        good = r["status"] in ("ok", "skipped")
        worst = worst or (0 if good else 1)
        mark = ui.styled(t("mark.ok") if good else t("mark.fail"), "ok" if good else "fail")
        table.append([mark, r["title"], r["destination"], t(f"stage.{r['stage']}"), r.get("error") or ""])
    ui.table(["", t("retry.col.meeting"), t("retry.col.destination"), t("retry.col.stage"), t("retry.col.error")],
             table)
    return worst


def cmd_pair(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import browser, pairing, ui

    if args.rotate:
        pairing.rotate_token()
        ui.ok(t("pair.rotated"))
    code = pairing.new_code()
    ui.markup(f"{ui.esc(t('pair.code_label'))}  [bold accent]{code}[/]  [dim]{ui.esc(t('pair.valid'))}[/]")
    ui.say(t("pair.where"))
    ui.markup("  " + ui.path_markup(browser.extension_dir(cfg)))
    if args.wait:
        from minuta.setup import wait_for_extension

        with ui.step(t("setup.waiting_ext", seconds=args.wait)):
            ok = wait_for_extension(int(cfg["server"]["port"]), args.wait, time.time() - 1)
        (ui.ok if ok else ui.warn)(t("setup.ext_ok") if ok else t("setup.ext_timeout"))
        return 0 if ok else 1
    return 0


def cmd_doctor(args) -> int:
    from minuta import doctor

    cfg = load_cfg(required=False)
    return doctor.run(cfg, as_json=args.json)


def cmd_mode(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import actions, ui

    mode = args.mode
    if mode is None:
        current = actions.recording_mode(cfg)
        if not interactive(args):
            ui.say(t("mode.current", mode=t(f"mode.{current}")))
            return 0
        mode = ask().select(t("mode.question"), [(m, t(f"mode.{m}.long")) for m in actions.RECORDING_MODES], current)
    actions.set_recording_mode(mode)
    ui.ok(t("mode.saved", mode=t(f"mode.{mode}")))
    ui.say(t(f"mode.{mode}.long"), style="dim")
    return 0


def _quiet(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a step without filling the terminal; its output is shown only when it fails."""
    return subprocess.run(argv, text=True, capture_output=True, cwd=cwd)


def _show_failure(r: subprocess.CompletedProcess) -> None:
    from minuta import ui

    tail = "\n".join(((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-15:])
    if tail:
        ui.say(tail, style="dim")


def cmd_update(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import browser, ui

    g = cfg["general"]
    app_dir = g.get("app_dir")
    if not app_dir or not Path(app_dir).exists():
        ui.error(t("update.no_source"))
        return 1
    app = Path(app_dir)
    ref = g.get("install_ref") or ""
    with ui.step(t("update.fetching")):
        if (app / ".git").exists():
            r = _git_update(app, ref)
            if r.returncode != 0:
                ui.fail(t("update.fetch_failed"))
                _show_failure(r)
                return r.returncode
        else:
            try:
                _download_tarball(os.environ.get("MINUTA_REPO_URL", DEFAULT_REPO), ref, app)
            except (OSError, ValueError) as e:
                ui.fail(t("update.fetch_failed"))
                ui.error(str(e))
                return 1
    ui.ok(t("update.fetched"))
    uv = shutil.which("uv")
    if not uv:
        ui.error(t("update.no_uv"))
        return 1
    extras = g.get("install_extras", "local")
    spec = f".[{extras}]" if extras else "."
    python = os.environ.get("MINUTA_PYTHON") or DEFAULT_PYTHON
    argv = [uv, "tool", "install", "--force", "--reinstall", "--python", python]
    if (app / "constraints.txt").exists():  # the versions pinned in uv.lock
        argv += ["--constraints", "constraints.txt"]
    # Relative paths from inside the checkout: uv splits a --constraints path at spaces.
    with ui.step(t("update.installing")):
        r = _quiet([*argv, spec], cwd=app)
    if r.returncode != 0:
        ui.fail(t("update.install_failed"))
        _show_failure(r)
        return r.returncode
    ui.ok(t("update.installed"))
    ext = browser.sync_extension(cfg)
    if ext.copied:
        ui.ok(t("update.extension_synced", path=ext.folder))
    if ext.old_folder_in_use:
        ui.warn(t("update.extension_moved", new=ext.folder, old=ext.old_folder))
    if not args.no_restart:
        from minuta.service import ServiceManager

        mgr = ServiceManager()
        if mgr.status().installed:
            with ui.step(t("update.restarting")):
                mgr.restart()
            ui.ok(t("update.restarted"))
    ui.blank()
    ui.say(t("update.done"))
    return 0


def _git_update(app: Path, ref: str) -> subprocess.CompletedProcess:
    """The installers' checkout follows the branch or tag it was installed from (a tag leaves HEAD
    detached). A checkout without the installer's marker is the user's own: only fast-forwarded."""
    git = ["git", "-C", str(app)]
    if not (app / INSTALLER_MARKER).exists():
        return _quiet([*git, "pull", "--ff-only"])
    r = None
    for step in (["fetch", "--depth", "1", "origin", ref or "HEAD"], ["reset", "--hard", "FETCH_HEAD"]):
        r = _quiet([*git, *step])
        if r.returncode != 0:
            return r
    return r


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
    from minuta import browser, credentials, ui

    asking = interactive(args) and not args.yes
    if asking and not ask().confirm(t("uninstall.confirm"), False):
        return 1
    from minuta.service import ServiceManager

    with ui.step(t("uninstall.service")):
        ServiceManager().uninstall()
    ui.ok(t("uninstall.service_done"))
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
    ui.ok(t("uninstall.secrets_done"))
    _remove_config(keep=[config_mod.meetings_dir(cfg), Path(cfg['general'].get('app_dir') or paths.default_app_dir())])
    logs = paths.log_dir()
    paths.remove_tree(logs)
    if logs.parent.name == "minuta" and logs.parent.exists() and not any(logs.parent.iterdir()):
        logs.parent.rmdir()
    ui.ok(t("uninstall.config_done"))
    if browser.remove_extension_copy(cfg):
        ui.ok(t("uninstall.extension_done", path=browser.extension_copy_dir(cfg)))
    leftovers: list[Path] = []
    if delete_data:
        paths.remove_tree(data)
        leftovers.append(data)
        default = paths.default_data_dir()
        if default.exists() and not any(p for p in default.iterdir() if p.name != "app"):
            paths.remove_tree(default)
            leftovers.append(default)
        ui.ok(t("uninstall.data_done", path=data))
    else:
        ui.ok(t("uninstall.kept", path=data))
    app_dir = cfg["general"].get("app_dir")
    if app_dir and (Path(app_dir) / INSTALLER_MARKER).exists():
        paths.remove_tree(Path(app_dir))
        leftovers.append(Path(app_dir))
    for d in (paths.config_dir(), paths.default_data_dir()):
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    if not args.keep_tool:
        with ui.step(t("uninstall.tool")):
            _uninstall_tool([d for d in leftovers if d.exists()])
    ui.blank()
    ui.say(t("uninstall.done"))
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
    from minuta import ui
    from minuta.service import ServiceManager

    mgr = ServiceManager()
    action = args.action
    with ui.step(t(f"service.doing.{action}")):
        if action == "install":
            where = mgr.install()
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
    if action == "install":
        ui.ok(t("setup.service_installed", where=where))
    state = t("status.service_running") if st.running else (
        t("status.service_stopped") if st.installed else t("status.service_missing"))
    (ui.ok if st.running or action in ("stop", "uninstall") else ui.warn)(t("service.state", state=state))
    return 0


def cmd_engine(args) -> int:
    cfg = load_cfg()
    if cfg is None:
        return 1
    from minuta import setup, ui

    try:
        api_key = setup.engine_step(cfg, args, setup.Prompter(interactive(args)))
    except KeyboardInterrupt:
        print()
        return 130
    config_mod.save(cfg)
    setup.store_api_key(cfg, api_key)
    tr = cfg["transcription"]
    name = tr["engine"] if tr["engine"] == "local" else tr["vercel"]["model"]
    ui.ok(t("engine.saved", engine=name))
    return 0


MENU = [("status", ["status"]), ("list", ["list"]), ("transcribe", ["transcribe"]),
        ("destinations", ["destinations", "list"]), ("mode", ["mode"]), ("engine", ["engine"]),
        ("retry", ["retry"]), ("doctor", ["doctor"]), ("pair", ["pair"]), ("setup", ["setup"]),
        ("update", ["update"])]


def menu_header(cfg: dict) -> str:
    """One line: version, whether the server answers, the extension, meetings left to transcribe.
    Only cheap checks, so the menu opens at once."""
    from minuta import actions, client, pairing, ui

    port = int(cfg["server"]["port"])
    up = bool(client.health(port, timeout=0.5))
    state = pairing.read_state()
    pending = sum(1 for m in actions.transcribable(cfg) if not m.has_transcript())
    paired = bool(state.get("allowed_origins"))
    parts = [f"[head]minuta[/] [dim]{__version__}[/]",
             f"[ok]{ui.esc(t('header.on'))}[/]" if up else f"[fail]{ui.esc(t('header.off'))}[/]",
             ui.esc(t("header.paired")) if paired else f"[warn]{ui.esc(t('header.unpaired'))}[/]",
             ui.esc(t(f"mode.{actions.recording_mode(cfg)}")),
             ui.esc(t("header.pending", n=pending)) if pending else ui.esc(t("header.all_done"))]
    return "  ·  ".join(parts)


def cmd_menu(_args) -> int:
    cfg = load_cfg(required=False)
    if not config_mod.exists():
        ns = build_parser().parse_args(["setup"])
        return cmd_setup(ns)
    set_language((cfg or {}).get("general", {}).get("language"))
    from minuta import ui

    ui.markup(menu_header(cfg))
    choices = [(k, t(f"menu.{k}")) for k, _ in MENU] + [("quit", t("menu.quit"))]
    last = "status"
    while True:
        ui.blank()
        try:
            choice = ask().select(t("menu.title"), choices, last)
        except KeyboardInterrupt:
            return 0
        if choice == "quit":
            return 0
        last = choice
        ui.rule(t(f"menu.{choice}"))
        ns = build_parser().parse_args(dict(MENU)[choice])
        ns.from_menu = True
        try:
            ns.func(ns)
        except KeyboardInterrupt:
            ui.blank()
            ui.say(t("menu.interrupted"), style="dim")
        if choice == "setup":
            cfg = load_cfg(required=False) or cfg
            choices = [(k, t(f"menu.{k}")) for k, _ in MENU] + [("quit", t("menu.quit"))]


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
    s.add_argument("--recording-mode", choices=["auto", "ask", "manual"],
                   help="auto: record every call; ask: ask in the browser; manual: only from the popup")
    s.add_argument("--extension-dir", help="where to keep the browser extension folder (default: ~/minuta-extension)")
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
    s.add_argument("--verbose", action="store_true", help="show the full meeting ids")
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
    s.add_argument("--verbose", action="store_true", help="also print the log")
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
    s.add_argument("--verbose", action="store_true", help="also print the log")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("mode", help="when the extension records: auto, ask or manual")
    s.add_argument("mode", nargs="?", choices=["auto", "ask", "manual"])
    s.add_argument("--non-interactive", action="store_true")
    s.set_defaults(func=cmd_mode)

    s = sub.add_parser("engine", help="choose the transcription engine (local or Vercel AI Gateway)")
    s.add_argument("--engine", choices=["local", "vercel"])
    s.add_argument("--vercel-model")
    s.add_argument("--local-model")
    s.add_argument("--api-key-env", help="read the Vercel AI Gateway key from this environment variable")
    s.add_argument("--max-usd", type=float)
    s.add_argument("--non-interactive", action="store_true")
    s.set_defaults(func=cmd_engine)

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


def use_select_on_macos() -> None:
    """Prompts read the terminal through asyncio, whose macOS default (kqueue) cannot watch
    /dev/tty: `curl ... | sh` hands setup the terminal that way and the first question crashed."""
    if sys.platform != "darwin":
        return
    import asyncio
    import selectors

    class SelectPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return asyncio.SelectorEventLoop(selectors.SelectSelector())

    asyncio.set_event_loop_policy(SelectPolicy())


def main(argv: list[str] | None = None) -> int:
    use_select_on_macos()
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
