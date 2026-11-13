"""The extension's real service worker with a fake Chrome API: icon, warnings, queue, pairing, popup."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from .conftest import ROOT

SCENARIOS = re.findall(r"^  async (\w+)\(\)", (ROOT / "tests/sw_harness.js").read_text(), re.M)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_service_worker(scenario):
    r = subprocess.run(["node", str(ROOT / "tests/sw_harness.js"), str(ROOT / "extension/background.js"), scenario],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_there_are_scenarios():
    assert len(SCENARIOS) >= 12


def test_manifest_asks_only_for_what_it_uses():
    m = json.loads((ROOT / "extension/manifest.json").read_text())
    assert m["manifest_version"] == 3
    assert sorted(m["permissions"]) == ["alarms", "notifications", "storage"]
    assert m["host_permissions"] == ["http://127.0.0.1/*", "https://meet.google.com/*"]
    assert all(cs["matches"] == ["https://meet.google.com/*"] for cs in m["content_scripts"])
    for size in (16, 32, 48, 128):
        for state in ("idle", "rec", "paused", "warn"):
            assert (ROOT / f"extension/icons/{state}-{size}.png").exists()
    assert m["version"] == __import__("minuta").__version__
    # Extension pages (the popup, app.html) load nothing remote and talk only to the local server.
    csp = m["content_security_policy"]["extension_pages"]
    assert "script-src 'self';" in csp and "connect-src http://127.0.0.1:*;" in csp and "object-src 'none'" in csp
    assert "http" not in csp.replace("http://127.0.0.1:*", "")
