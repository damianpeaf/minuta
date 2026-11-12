"""`minuta doctor`: what works and what does not, with a hint for each problem."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

from minuta import __version__, browser, client, credentials, pairing, paths
from minuta import config as config_mod
from minuta.i18n import forced_language, t

OK, WARN, FAIL = "ok", "warn", "fail"


def checks(cfg: dict | None) -> list[dict]:
    out: list[dict] = []

    def add(name: str, status: str, detail: str = "", hint: str = "") -> None:
        out.append({"check": name, "status": status, "detail": detail, "hint": hint})

    add("python", OK if sys.version_info >= (3, 10) else FAIL, sys.version.split()[0])
    add("minuta", OK, __version__)
    for exe in ("ffmpeg", "ffprobe"):
        found = shutil.which(exe)
        add(exe, OK if found else (FAIL if exe == "ffmpeg" else WARN), found or t("doctor.not_found"),
            "" if found else t("doctor.hint.ffmpeg"))
    if cfg is None or not config_mod.exists():
        add("config", FAIL, str(paths.config_file()), t("doctor.hint.setup"))
        return out
    errors = config_mod.validate(cfg)
    add("config", FAIL if errors else OK, "; ".join(errors) or str(paths.config_file()))

    data = config_mod.data_dir(cfg)
    try:
        data.mkdir(parents=True, exist_ok=True)
        writable = os.access(data, os.W_OK)
    except OSError:
        writable = False
    add("data folder", OK if writable else FAIL, str(data), "" if writable else t("doctor.hint.data"))

    from minuta.service import ServiceManager

    st = ServiceManager().status()
    add("service", OK if st.running else (WARN if st.installed else FAIL),
        t("doctor.service_running") if st.running else t("doctor.service_stopped") if st.installed
        else t("doctor.service_missing"),
        "" if st.running else t("doctor.hint.service"))

    port = int(cfg["server"]["port"])
    h = client.health(port)
    add("server", OK if h else FAIL, t("status.server_up", port=port) if h else t("status.server_down", port=port),
        "" if h else t("doctor.hint.server"))

    state = pairing.read_state()
    paired = bool(state.get("allowed_origins"))
    seen = state.get("last_seen")
    add("extension", OK if paired else WARN,
        (t("doctor.ext_seen", seconds=round(time.time() - seen)) if seen and paired else
         t("status.ext_paired") if paired else t("status.ext_unpaired")),
        "" if paired else t("doctor.hint.extension", path=browser.extension_dir(cfg)))
    found = browser.detect()
    add("browser", OK if found else WARN, ", ".join(b.name for b in found) or t("doctor.no_browser"))

    store = credentials.store(cfg)
    add("secrets", OK, store.where)

    engine = cfg["transcription"]["engine"]
    if engine == "local":
        from minuta.transcription.local import available

        ok, detail = available()
        add("engine local", OK if ok else WARN, detail if ok else t("doctor.local_missing", module=detail.split()[0]))
    else:
        from minuta.transcription.vercel import KEY_NAME

        has = bool(store.get(KEY_NAME))
        add("engine vercel", OK if has else FAIL,
            t("doctor.key_stored") if has else t("doctor.key_missing", name=KEY_NAME),
            "" if has else t("doctor.hint.key"))

    from minuta import destinations as reg

    for name, opts in (cfg.get("destinations") or {}).items():
        if not opts.get("enabled", True):
            add(f"destination {name}", OK, t("dest.disabled"))
            continue
        try:
            r = reg.create(name, opts, store).check()
            add(f"destination {name}", OK if r.ok else FAIL, r.message)
        except Exception as e:  # noqa: BLE001
            add(f"destination {name}", FAIL, str(e))
    return out


def label(check: str) -> str:
    """A check's name in the CLI's language ("destination local" -> "destino local")."""
    kind, _, rest = check.partition(" ")
    if kind in ("destination", "engine") and rest:
        return t(f"doctor.name.{kind}", name=rest)
    return t(f"doctor.name.{check.replace(' ', '_')}")


def run(cfg: dict | None, as_json: bool = False) -> int:
    if as_json:
        with forced_language("en"):  # scripts read English
            results = checks(cfg)
        print(json.dumps(results, indent=2))
        return 1 if any(r["status"] == FAIL for r in results) else 0
    from minuta import ui

    with ui.step(t("doctor.checking")):
        results = checks(cfg)
    rows = []
    for r in results:
        mark = ui.styled(t(f"mark.{r['status']}"), {"ok": "ok", "warn": "warn", "fail": "fail"}[r["status"]])
        detail = r["detail"] + (f"\n-> {r['hint']}" if r["hint"] else "")
        rows.append([mark, label(r["check"]), detail])
    ui.table(["", t("doctor.col.check"), t("doctor.col.detail")], rows)
    problems = sum(1 for r in results if r["status"] == FAIL)
    if problems:
        ui.fail(t("doctor.problems", n=problems))
    else:
        ui.ok(t("doctor.all_good"))
    return 1 if problems else 0
