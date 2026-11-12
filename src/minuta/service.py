"""Run the server in the background at login.

- Linux: a systemd user unit (`~/.config/systemd/user/minuta.service`).
- macOS: a launchd agent (`~/Library/LaunchAgents/io.minuta.server.plist`).
- Windows: a Task Scheduler task that runs at logon; if creating it is not allowed for this
  account, a `Run` registry entry for the current user does the same job.

Install and uninstall are idempotent: installing twice rewrites the same file, uninstalling
something absent is not an error. Rendering is separate from installing so it can be tested on
any OS.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from minuta import paths

SYSTEMD_UNIT = "minuta.service"
LAUNCHD_LABEL = "io.minuta.server"
WINDOWS_TASK = "minuta"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

Runner = Callable[..., subprocess.CompletedProcess]


def server_command(python: str | None = None, home: str | None = None, windows: bool | None = None) -> list[str]:
    python = python or sys.executable
    windows = (os.name == "nt") if windows is None else windows
    if windows:
        w = Path(python).with_name("pythonw.exe")
        if w.exists():
            python = str(w)
    cmd = [python, "-m", "minuta", "serve"]
    home = home if home is not None else os.environ.get("MINUTA_HOME")
    if home:
        cmd += ["--home", home]
    return cmd


def service_path_env() -> str:
    """launchd and systemd start with a bare PATH; ffmpeg lives in Homebrew or ~/.local/bin."""
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin")]
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for p in extra:
        if p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def render_systemd(cmd: list[str], path_env: str) -> str:
    exec_start = " ".join(shlex.quote(c) for c in cmd)
    return "\n".join([
        "[Unit]",
        "Description=minuta: local server for meeting recordings",
        "After=default.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        f'Environment="PATH={path_env}"',
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def render_launchd(cmd: list[str], path_env: str, log_dir: Path) -> bytes:
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PATH": path_env},
        "StandardOutPath": str(log_dir / "server.out.log"),
        "StandardErrorPath": str(log_dir / "server.err.log"),
        "ProcessType": "Interactive",
    })


def _win_quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def render_schtasks_create(cmd: list[str]) -> list[str]:
    command_line = " ".join(_win_quote(c) for c in cmd)
    return ["schtasks", "/Create", "/TN", WINDOWS_TASK, "/TR", command_line, "/SC", "ONLOGON", "/RL", "LIMITED", "/F"]


def pid_file() -> Path:
    return paths.config_dir() / "server.pid"


def read_pid() -> int | None:
    try:
        return int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


@dataclass
class Status:
    installed: bool
    running: bool
    detail: str = ""


class ServiceManager:
    def __init__(self, platform: str | None = None, home: Path | None = None, runner: Runner | None = None,
                 cmd: list[str] | None = None):
        self.platform = platform or ("windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux")
        self.home = home or Path.home()
        self.run = runner or (lambda args, **kw: subprocess.run(args, capture_output=True, text=True, **kw))
        self.cmd = cmd or server_command(windows=self.platform == "windows")

    # --- file locations ---------------------------------------------------------
    @property
    def unit_path(self) -> Path:
        if self.platform == "linux":
            base = Path(os.environ.get("XDG_CONFIG_HOME") or self.home / ".config")
            return base / "systemd" / "user" / SYSTEMD_UNIT
        if self.platform == "macos":
            return self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        return paths.config_dir() / "windows-task.txt"  # marker: the task lives in Task Scheduler

    def render(self) -> str | bytes:
        if self.platform == "linux":
            return render_systemd(self.cmd, service_path_env())
        if self.platform == "macos":
            return render_launchd(self.cmd, service_path_env(), paths.log_dir())
        return " ".join(render_schtasks_create(self.cmd))

    def _uid_domain(self) -> str:
        return f"gui/{os.getuid()}"  # type: ignore[attr-defined]

    # --- actions ----------------------------------------------------------------------
    def install(self) -> str:
        content = self.render()
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        if self.platform == "linux":
            self.unit_path.write_text(content, encoding="utf-8")  # type: ignore[arg-type]
            self._ok(["systemctl", "--user", "daemon-reload"])
            self._ok(["systemctl", "--user", "enable", SYSTEMD_UNIT])
            self._ok(["systemctl", "--user", "restart", SYSTEMD_UNIT])
            return str(self.unit_path)
        if self.platform == "macos":
            paths.log_dir().mkdir(parents=True, exist_ok=True)
            self.unit_path.write_bytes(content)  # type: ignore[arg-type]
            self.run(["launchctl", "bootout", f"{self._uid_domain()}/{LAUNCHD_LABEL}"])
            self._ok(["launchctl", "bootstrap", self._uid_domain(), str(self.unit_path)])
            return str(self.unit_path)
        return self._install_windows()

    def _install_windows(self) -> str:
        r = self.run(render_schtasks_create(self.cmd))
        if r.returncode == 0:
            self.unit_path.write_text("task\n", encoding="utf-8")
            self.run(["schtasks", "/Run", "/TN", WINDOWS_TASK])
            return f"Task Scheduler: {WINDOWS_TASK}"
        # Some accounts may not create logon tasks without elevation: use the per-user Run key.
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, WINDOWS_TASK, 0, winreg.REG_SZ, " ".join(_win_quote(c) for c in self.cmd))
        self.unit_path.write_text("runkey\n", encoding="utf-8")
        self.start()
        return f"HKCU\\{RUN_KEY}\\{WINDOWS_TASK}"

    def uninstall(self) -> None:
        if self.platform == "linux":
            self.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
            self.unit_path.unlink(missing_ok=True)
            self.run(["systemctl", "--user", "daemon-reload"])
            self.run(["systemctl", "--user", "reset-failed", SYSTEMD_UNIT])
        elif self.platform == "macos":
            self.run(["launchctl", "bootout", f"{self._uid_domain()}/{LAUNCHD_LABEL}"])
            self.unit_path.unlink(missing_ok=True)
        else:
            self.stop()
            self.run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"])
            try:
                import winreg  # type: ignore[import-not-found]

                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, WINDOWS_TASK)
            except (OSError, ImportError):
                pass
            self.unit_path.unlink(missing_ok=True)

    def start(self) -> None:
        if self.platform == "linux":
            self._ok(["systemctl", "--user", "start", SYSTEMD_UNIT])
        elif self.platform == "macos":
            self._ok(["launchctl", "kickstart", f"{self._uid_domain()}/{LAUNCHD_LABEL}"])
        else:
            if self.run(["schtasks", "/Run", "/TN", WINDOWS_TASK]).returncode != 0:
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                subprocess.Popen(self.cmd, creationflags=flags, close_fds=True)  # noqa: S603

    def stop(self) -> None:
        if self.platform == "linux":
            self.run(["systemctl", "--user", "stop", SYSTEMD_UNIT])
        elif self.platform == "macos":
            self.run(["launchctl", "kill", "TERM", f"{self._uid_domain()}/{LAUNCHD_LABEL}"])
        else:
            self.run(["schtasks", "/End", "/TN", WINDOWS_TASK])
            # The Run-key variant (or a manual start) is a plain process: the server writes its pid.
            pid = read_pid()
            if pid:
                self.run(["taskkill", "/F", "/PID", str(pid)])

    def restart(self) -> None:
        if self.platform == "linux":
            self._ok(["systemctl", "--user", "restart", SYSTEMD_UNIT])
        elif self.platform == "macos":
            self._ok(["launchctl", "kickstart", "-k", f"{self._uid_domain()}/{LAUNCHD_LABEL}"])
        else:
            self.stop()
            self.start()

    def status(self) -> Status:
        installed = self.unit_path.exists()
        if self.platform == "linux":
            r = self.run(["systemctl", "--user", "is-active", SYSTEMD_UNIT])
            state = (r.stdout or "").strip()
            return Status(installed, state == "active", state)
        if self.platform == "macos":
            r = self.run(["launchctl", "print", f"{self._uid_domain()}/{LAUNCHD_LABEL}"])
            running = r.returncode == 0 and "state = running" in (r.stdout or "")
            return Status(installed, running, "running" if running else "not running")
        r = self.run(["schtasks", "/Query", "/TN", WINDOWS_TASK])
        running = "Running" in (r.stdout or "")
        return Status(installed, running, (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")

    def _ok(self, args: list[str]) -> None:
        r = self.run(args)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(args)} failed: {(r.stderr or r.stdout or '').strip()[:300]}")
