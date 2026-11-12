"""`minuta doctor`: what works and what does not, with a hint for each problem."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

from minuta import __version__, browser, client, credentials, pairing, paths
from minuta import config as config_mod

OK, WARN, FAIL = "ok", "warn", "fail"


def checks(cfg: dict | None) -> list[dict]:
    out: list[dict] = []

    def add(name: str, status: str, detail: str = "", hint: str = "") -> None:
        out.append({"check": name, "status": status, "detail": detail, "hint": hint})

    add("python", OK if sys.version_info >= (3, 10) else FAIL, sys.version.split()[0])
    add("minuta", OK, __version__)
    for exe in ("ffmpeg", "ffprobe"):
        found = shutil.which(exe)
        add(exe, OK if found else (FAIL if exe == "ffmpeg" else WARN), found or "not found",
            "" if found else "install ffmpeg (brew install ffmpeg, apt install ffmpeg, winget install ffmpeg)")
    if cfg is None or not config_mod.exists():
        add("config", FAIL, str(paths.config_file()), "run `minuta setup`")
        return out
    errors = config_mod.validate(cfg)
    add("config", FAIL if errors else OK, "; ".join(errors) or str(paths.config_file()))

    data = config_mod.data_dir(cfg)
    try:
        data.mkdir(parents=True, exist_ok=True)
        writable = os.access(data, os.W_OK)
    except OSError:
        writable = False
    add("data folder", OK if writable else FAIL, str(data), "" if writable else "choose another folder in setup")

    from minuta.service import ServiceManager

    st = ServiceManager().status()
    add("service", OK if st.running else (WARN if st.installed else FAIL),
        f"{'installed' if st.installed else 'not installed'}, {st.detail or '-'}",
        "" if st.running else "run `minuta service install` or `minuta setup`")

    port = int(cfg["server"]["port"])
    h = client.health(port)
    add("server", OK if h else FAIL, f"127.0.0.1:{port} {'answering' if h else 'not answering'}",
        "" if h else "is another program using the port? change [server] port and run setup again")

    state = pairing.read_state()
    paired = bool(state.get("allowed_origins"))
    seen = state.get("last_seen")
    add("extension", OK if paired else WARN,
        ("paired" if paired else "not paired") + (f", last seen {round(time.time() - seen)} s ago" if seen else ""),
        "" if paired else f"load {browser.extension_dir(cfg)} and run `minuta pair`")
    found = browser.detect()
    add("browser", OK if found else WARN, ", ".join(b.name for b in found) or "no Chrome, Edge or Brave found")

    store = credentials.store(cfg)
    add("secrets", OK, store.where)

    engine = cfg["transcription"]["engine"]
    if engine == "local":
        from minuta.transcription.local import available

        ok, detail = available()
        add("engine local", OK if ok else WARN, detail)
    else:
        from minuta.transcription.vercel import KEY_NAME

        has = bool(store.get(KEY_NAME))
        add("engine vercel", OK if has else FAIL, "key stored" if has else f"{KEY_NAME} missing",
            "" if has else "run `minuta setup` and choose vercel to store the key")

    from minuta import destinations as reg

    for name, opts in (cfg.get("destinations") or {}).items():
        if not opts.get("enabled", True):
            add(f"destination {name}", OK, "disabled")
            continue
        try:
            r = reg.create(name, opts, store).check()
            add(f"destination {name}", OK if r.ok else FAIL, r.message)
        except Exception as e:  # noqa: BLE001
            add(f"destination {name}", FAIL, str(e))
    return out


def run(cfg: dict | None, as_json: bool = False) -> int:
    results = checks(cfg)
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[r["status"]]
            line = f"[{mark}] {r['check']:20} {r['detail']}"
            if r["hint"]:
                line += f"\n       -> {r['hint']}"
            print(line)
    return 1 if any(r["status"] == FAIL for r in results) else 0
