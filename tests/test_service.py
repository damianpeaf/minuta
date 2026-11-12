"""Background service: unit files per OS, and what install/uninstall run (with a fake runner)."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from minuta import service
from minuta.service import ServiceManager, render_launchd, render_schtasks_create, render_systemd


class FakeRunner:
    def __init__(self, fail: set[str] | None = None, stdout: str = ""):
        self.calls: list[list[str]] = []
        self.fail = fail or set()
        self.stdout = stdout

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        rc = 1 if any(f in " ".join(args) for f in self.fail) else 0
        return subprocess.CompletedProcess(args, rc, stdout=self.stdout, stderr="")


CMD = ["/home/u/.local/share/uv/tools/minuta/bin/python", "-m", "minuta", "serve"]


def test_server_command_uses_this_python_and_home(monkeypatch):
    monkeypatch.setenv("MINUTA_HOME", "/tmp/h")
    assert service.server_command("/py") == ["/py", "-m", "minuta", "serve", "--home", "/tmp/h"]
    monkeypatch.delenv("MINUTA_HOME")
    assert service.server_command("/py") == ["/py", "-m", "minuta", "serve"]


def test_systemd_unit():
    unit = render_systemd([*CMD, "--home", "/tmp/with space"], "/usr/bin:/bin")
    assert "ExecStart=/home/u/.local/share/uv/tools/minuta/bin/python -m minuta serve --home '/tmp/with space'" in unit
    assert 'Environment="PATH=/usr/bin:/bin"' in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit


def test_launchd_plist(tmp_path):
    data = plistlib.loads(render_launchd(CMD, "/opt/homebrew/bin:/usr/bin", tmp_path))
    assert data["Label"] == service.LAUNCHD_LABEL and data["ProgramArguments"] == CMD
    assert data["RunAtLoad"] is True and data["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin")
    assert data["StandardErrorPath"].startswith(str(tmp_path))


def test_schtasks_command_quotes_paths_with_spaces():
    args = render_schtasks_create([r"C:\Users\A B\pythonw.exe", "-m", "minuta", "serve"])
    assert args[:4] == ["schtasks", "/Create", "/TN", "minuta"]
    assert args[args.index("/TR") + 1] == r'"C:\Users\A B\pythonw.exe" -m minuta serve'
    assert "ONLOGON" in args and "/F" in args


def test_service_path_includes_common_ffmpeg_locations(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    p = service.service_path_env().split(os.pathsep)
    assert p[0] == "/usr/bin" and "/opt/homebrew/bin" in p


def test_linux_install_and_uninstall_are_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    run = FakeRunner()
    mgr = ServiceManager("linux", home=tmp_path, runner=run, cmd=CMD)
    mgr.install()
    mgr.install()
    unit = tmp_path / "xdg/systemd/user/minuta.service"
    assert unit.exists() and "minuta serve" in unit.read_text()
    assert ["systemctl", "--user", "enable", "minuta.service"] in run.calls
    mgr.uninstall()
    mgr.uninstall()
    assert not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "minuta.service"] in run.calls


def test_linux_install_reports_systemctl_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    mgr = ServiceManager("linux", home=tmp_path, runner=FakeRunner(fail={"enable"}), cmd=CMD)
    with pytest.raises(RuntimeError, match="enable"):
        mgr.install()
    assert not mgr.unit_path.exists()  # a failed install must not read as installed
    assert not mgr.status().installed


def test_macos_install_boots_out_then_bootstraps(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    run = FakeRunner(fail={"bootout"})  # bootout fails when nothing is loaded: must be ignored
    mgr = ServiceManager("macos", home=tmp_path, runner=run, cmd=CMD)
    mgr.install()
    plist = tmp_path / "Library/LaunchAgents/io.minuta.server.plist"
    assert plist.exists()
    assert run.calls[-1] == ["launchctl", "bootstrap", "gui/501", str(plist)]
    mgr.uninstall()
    assert not plist.exists() and run.calls[-1][:2] == ["launchctl", "bootout"]


def test_windows_install_uses_task_scheduler(tmp_path):
    run = FakeRunner()
    mgr = ServiceManager("windows", home=tmp_path, runner=run, cmd=CMD)
    where = mgr.install()
    assert where.startswith("Task Scheduler")
    assert run.calls[0][:2] == ["schtasks", "/Create"] and ["schtasks", "/Run", "/TN", "minuta"] in run.calls
    assert mgr.status().installed
    mgr.uninstall()
    assert ["schtasks", "/Delete", "/TN", "minuta", "/F"] in run.calls and not mgr.status().installed


@pytest.mark.service
@pytest.mark.skipif(not (sys.platform.startswith("linux") and shutil.which("systemctl")), reason="systemd only")
def test_real_systemd_user_service(cfg, tmp_path):
    """Installs the real unit for this user, waits for the server, then removes everything."""
    import time

    from minuta import client

    cfg["server"]["port"] = 47399
    from minuta import config as config_mod

    config_mod.save(cfg)
    mgr = ServiceManager("linux")
    try:
        mgr.install()
        for _ in range(40):
            if client.health(47399):
                break
            time.sleep(0.5)
        assert client.health(47399)
        assert mgr.status().running
    finally:
        mgr.uninstall()
    assert not Path(mgr.unit_path).exists()
    assert not mgr.status().running
