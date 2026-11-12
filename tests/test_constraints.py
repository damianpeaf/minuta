"""constraints.txt is uv.lock exported for `uv tool install --constraints` (installers and `minuta update`)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ["uv", "export", "--frozen", "--no-hashes", "--no-dev", "--all-extras", "--no-emit-project", "--no-annotate"]


def pins(text: str) -> list[str]:
    """The `name==version` pins, without the environment markers: how uv writes the markers changes
    between uv releases (it simplifies them differently) while the pinned versions do not."""
    lines = (line.split(";", 1)[0].strip() for line in text.splitlines() if not line.startswith("#"))
    return sorted(line.lower() for line in lines if line)


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
def test_constraints_match_the_lock():
    exported = subprocess.run(EXPORT, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert pins((ROOT / "constraints.txt").read_text(encoding="utf-8")) == pins(exported), (
        "uv.lock changed: regenerate with `" + " ".join(EXPORT) + " -o constraints.txt`")


def test_the_installers_and_update_install_with_the_constraints():
    for name in ("install.sh", "install.ps1"):
        assert "--constraints" in (ROOT / name).read_text(encoding="utf-8"), name
    assert '"--constraints"' in (ROOT / "src" / "minuta" / "cli.py").read_text(encoding="utf-8")
