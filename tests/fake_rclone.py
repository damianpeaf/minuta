"""A stand-in for the rclone program, for tests. Remotes are local folders.

Behaviour comes from environment variables:
  FAKE_RCLONE_LOG      file where each call's argv is appended as a JSON line
  FAKE_RCLONE_REMOTES  JSON {"name": "/folder"}
  FAKE_RCLONE_FAIL     JSON {"command": [exit_code, "stderr", times]}: fail that command (times<0: always)
  FAKE_RCLONE_SLEEP    seconds to sleep before answering
  FAKE_RCLONE_NOHASH   "1": `check` reports that no common hash exists
  FAKE_RCLONE_CORRUPT  "1": uploads drop the last byte, so `check` finds a difference
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

VALUE_FLAGS = {"--config", "--contimeout", "--timeout", "--max-depth"}


def fail(code: int, msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def resolve(spec: str, remotes: dict) -> Path:
    if ":" not in spec:
        return Path(spec)
    name, _, path = spec.partition(":")
    if name not in remotes:
        fail(1, f'Failed to create file system for "{spec}": didn\'t find section in config file ("{name}")')
    return Path(remotes[name]) / path.lstrip("/")


def main() -> None:
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_RCLONE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")
    if os.environ.get("FAKE_RCLONE_SLEEP"):
        time.sleep(float(os.environ["FAKE_RCLONE_SLEEP"]))
    remotes = json.loads(os.environ.get("FAKE_RCLONE_REMOTES") or "{}")
    flags, words, paths = [], [], []
    it = iter(argv)
    for a in it:
        if a == "--":
            paths = list(it)
            break
        if a.startswith("-"):
            flags.append(a)
            if a in VALUE_FLAGS:
                flags.append(next(it))
        else:
            words.append(a)
    if not words:
        fail(1, "Usage: rclone [flags]")
    cmd = words[0]
    paths = words[1:] + paths
    failures = json.loads(os.environ.get("FAKE_RCLONE_FAIL") or "{}")
    if cmd in failures:
        code, msg, times = failures[cmd]
        state = Path(os.environ.get("FAKE_RCLONE_LOG", "x") + f".{cmd}.fails")
        done = int(state.read_text()) if state.exists() else 0
        if times < 0 or done < times:
            state.write_text(str(done + 1))
            fail(code, msg)

    if cmd == "listremotes":
        print("\n".join(f"{n}:" for n in remotes))
    elif cmd == "lsjson":
        d = resolve(paths[0], remotes)
        if not d.is_dir():
            fail(3, "ERROR : : error listing: directory not found")
        files_only = "--files-only" in flags
        out = [{"Path": p.name, "Name": p.name, "Size": p.stat().st_size if p.is_file() else -1,
                "IsDir": p.is_dir()} for p in sorted(d.iterdir()) if not (files_only and p.is_dir())]
        print(json.dumps(out))
    elif cmd == "copyto":
        src, dst = resolve(paths[0], remotes), resolve(paths[1], remotes)
        if not src.is_file():
            fail(4, f"ERROR : {paths[0]}: object not found")
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        if os.environ.get("FAKE_RCLONE_CORRUPT") == "1" and ":" in paths[1]:
            data = data[:-1]
        dst.write_bytes(data)
    elif cmd == "cat":
        p = resolve(paths[0], remotes)
        if not p.is_file():
            fail(4, "object not found")
        sys.stdout.write(p.read_text(encoding="utf-8"))
    elif cmd == "check":
        src, dst = resolve(paths[0], remotes), resolve(paths[1], remotes)
        bad = [p.name for p in src.iterdir()
               if not (dst / p.name).is_file() or (dst / p.name).read_bytes() != p.read_bytes()]
        if os.environ.get("FAKE_RCLONE_NOHASH") == "1":
            sys.stderr.write("NOTICE: Local file system: No common hash found - not using a hash for checks\n")
        if bad:
            fail(1, "\n".join(f"ERROR : {b}: sizes differ" for b in bad) + f"\n{len(bad)} differences found")
    elif cmd == "config":
        pass
    else:
        fail(1, f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
