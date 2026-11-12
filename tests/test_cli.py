"""The CLI without a terminal: setup, status, list, destinations, transcribe, retry, doctor, uninstall."""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minuta import cli, credentials, pairing, paths
from minuta import config as config_mod
from minuta.meetings import MeetingStore

from .conftest import bursts, needs_ffmpeg, write_stereo

SETUP = ["setup", "--non-interactive", "--no-service", "--no-browser", "--skip-extension"]


def free_port() -> str:
    """A port nothing listens on, so a minuta installed on this machine never answers a test."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_setup_non_interactive_writes_config(capsys, tmp_path):
    code, out = run(capsys, *SETUP, "--language", "es", "--name", "Ana", "--audio-format", "mp3",
                    "--destination", "local", "--option", f"copy_to={tmp_path / 'Drive'}",
                    "--delete-audio-after-upload", "--port", "47555",
                    "--transcription-mode", "auto", "--transcription-language", "es-419")
    assert code == 0, out
    assert "Configuración guardada" in out and "Listo." in out
    cfg = config_mod.load()
    assert cfg["general"]["language"] == "es" and cfg["general"]["audio_format"] == "mp3"
    assert cfg["speakers"]["me"] == "Ana" and cfg["server"]["port"] == 47555
    assert cfg["destinations"]["local"]["copy_to"] == str(tmp_path / "Drive")
    assert cfg["storage"]["delete_local_audio_after_upload"] is True
    assert cfg["transcription"]["mode"] == "auto" and cfg["transcription"]["language"] == "es-419"
    assert pairing.get_token()  # the server token exists from the start
    assert config_mod.validate(cfg) == []


def test_setup_is_rerunnable_and_keeps_previous_answers(capsys):
    run(capsys, *SETUP, "--name", "Ana", "--audio-format", "mp3")
    run(capsys, *SETUP)
    cfg = config_mod.load()
    assert cfg["speakers"]["me"] == "Ana" and cfg["general"]["audio_format"] == "mp3"


def test_setup_with_vercel_stores_the_key_outside_the_config(capsys, monkeypatch):
    monkeypatch.setenv("MY_GATEWAY_KEY", "vk_test_abcdef123456")
    code, out = run(capsys, *SETUP, "--engine", "vercel", "--vercel-model", "spacexai/grok-stt",
                    "--api-key-env", "MY_GATEWAY_KEY", "--max-usd", "0.5")
    assert code == 0
    assert "vk_test_abcdef123456" not in out
    assert "vk_test_abcdef123456" not in paths.config_file().read_text()
    cfg = config_mod.load()
    assert cfg["transcription"]["vercel"]["model"] == "spacexai/grok-stt" and cfg["transcription"]["max_usd"] == 0.5
    assert credentials.store(cfg).get("AI_GATEWAY_API_KEY") == "vk_test_abcdef123456"


def test_setup_prints_the_extension_steps_and_a_code(capsys, monkeypatch):
    from minuta import browser

    monkeypatch.setattr(browser, "detect", lambda: [browser.Browser("Google Chrome", "/x/chrome", "chrome://extensions")])
    code, out = run(capsys, "setup", "--non-interactive", "--no-service", "--no-browser")
    assert "chrome://extensions" in out and "manifest.json" not in out
    assert str(browser.extension_dir(config_mod.load())) in out
    assert paths.pairing_file().exists()  # a code is waiting


def test_commands_need_setup_first(capsys):
    code, out = run(capsys, "status")
    assert code == 1 and "minuta setup" in out


def make_meeting(cfg, meeting_id="2026-05-05T10-00-00-000Z_cli-test-one", audio=True):
    m = MeetingStore(config_mod.meetings_dir(cfg)).get(meeting_id)
    m.update(status="ready", title="CLI meeting", started_at="2026-05-05T10:00:00Z", duration_seconds=900)
    if audio:
        write_stereo(m.dir / "audio.opus", bursts([(1, 3)], 6), bursts([(4, 5)], 6, freq=500))
    return m


def test_status_and_list_as_json(capsys):
    run(capsys, *SETUP, "--port", free_port())
    cfg = config_mod.load()
    m = make_meeting(cfg, audio=False)
    code, out = run(capsys, "status", "--json")
    st = json.loads(out)
    assert code == 0 and st["meetings"]["total"] == 1 and st["server"]["answering"] is False
    assert st["extension"]["paired"] is False
    code, out = run(capsys, "list", "--json")
    rows = json.loads(out)
    assert rows[0]["id"] == m.id and rows[0]["transcript"] is False
    code, out = run(capsys, "list")
    assert "CLI meeting" in out and "15 min" in out


def test_destinations_add_test_disable_remove(capsys, tmp_path):
    run(capsys, *SETUP)
    code, out = run(capsys, "destinations", "add", "backup", "--type", "local",
                    "--option", f"copy_to={tmp_path / 'backup'}", "--send", "audio,transcript")
    assert code == 0, out
    cfg = config_mod.load()
    assert cfg["destinations"]["backup"]["send"] == ["audio", "transcript"]
    assert run(capsys, "destinations", "test", "backup")[0] == 0
    run(capsys, "destinations", "disable", "backup")
    assert config_mod.load()["destinations"]["backup"]["enabled"] is False
    code, out = run(capsys, "destinations", "list")
    assert "backup" in out and "disabled" in out and "local (Keep meetings" in out
    run(capsys, "destinations", "remove", "backup")
    assert "backup" not in config_mod.load()["destinations"]
    assert run(capsys, "destinations", "add", "x", "--type", "nope")[0] == 2
    bad = tmp_path / "file"
    bad.write_text("x")
    assert run(capsys, "destinations", "add", "y", "--type", "local", "--option", f"copy_to={bad}")[0] == 1


@pytest.mark.parametrize("name", ["1hook", "my.hook", "Hook", "a" * 33, "with space"])
def test_destination_names_are_checked_with_a_clear_message(capsys, name):
    run(capsys, *SETUP)
    code, out = run(capsys, "destinations", "add", name, "--type", "local")
    assert code == 2 and "invalid destination name" in out and "Traceback" not in out
    assert name not in config_mod.load()["destinations"]


def test_list_shows_failed_deliveries(capsys):
    run(capsys, *SETUP)
    m = make_meeting(config_mod.load(), audio=False)
    m.update_path(["destinations", "hook", "meeting"], {"status": "failed", "error": "503"})
    code, out = run(capsys, "list")
    assert code == 0 and "[failed: hook:meeting]" in out


def test_webhook_add_keeps_secrets_out_of_config_and_output_and_remove_deletes_them(capsys):
    from .webhook_receiver import Receiver

    run(capsys, *SETUP)
    key, token = "cli-signing-key-0123456789abcdef", "cli_header_token_0123456789"
    with Receiver(key) as recv:
        code, out = run(capsys, "destinations", "add", "hook", "--type", "webhook", "--option", f"url={recv.url()}",
                        "--option", f"signing_key={key}", "--option", f"headers=Authorization: Bearer {token}")
        assert code == 0, out
        dest = config_mod.load()["destinations"]["hook"]
        assert dest["send"] == ["metadata", "transcript"]  # audio only when asked for
        assert dest["signing_key_secret"] == "HOOK_SIGNING_KEY" and dest["headers_secret"] == "HOOK_HEADERS"
        code, out = run(capsys, "destinations", "test", "hook", "--live")
        assert code == 0 and "live test ok" in out
        code, list_out = run(capsys, "destinations", "list")
        out += list_out
    assert recv.requests[0]["event"] == "ping" and recv.requests[0]["valid"]
    assert recv.requests[0]["headers"]["Authorization"] == f"Bearer {token}"
    for secret in (key, token):
        assert secret not in out and secret not in paths.config_file().read_text()
    store = credentials.store(config_mod.load())
    assert store.get("HOOK_SIGNING_KEY") == key
    run(capsys, "destinations", "remove", "hook")
    assert store.get("HOOK_SIGNING_KEY") is None and store.get("HOOK_HEADERS") is None


def test_webhook_add_is_refused_for_plain_http_and_leaves_no_secret(capsys):
    run(capsys, *SETUP)
    code, out = run(capsys, "destinations", "add", "hook", "--type", "webhook", "--option", "url=http://example.com/x",
                    "--option", "signing_key=cli-signing-key-0123456789abcdef")
    assert code == 1 and "https" in out
    assert "hook" not in config_mod.load()["destinations"]
    assert credentials.store(config_mod.load()).get("HOOK_SIGNING_KEY") is None


def test_command_and_obsidian_through_the_cli(capsys, tmp_path):
    import sys

    run(capsys, *SETUP)
    vault = tmp_path / "Vault"
    vault.mkdir()
    assert run(capsys, "destinations", "add", "notes", "--type", "obsidian", "--option", f"vault={vault}")[0] == 0
    code, out = run(capsys, "destinations", "add", "script", "--type", "command",
                    "--option", f'command="{sys.executable}" -c pass')
    assert code == 0, out
    code, out = run(capsys, "destinations", "test", "script", "--live")
    assert code == 0 and "no live test" in out
    assert run(capsys, "destinations", "add", "bad", "--type", "command", "--option", "command=nope-xyz-123")[0] == 1


class Scripted:
    """A Prompter that answers from a dict keyed by a word of each question; it fails on anything else."""

    interactive = True

    def __init__(self, answers: dict):
        self.answers, self.asked = answers, []

    def _answer(self, message, default=None):
        self.asked.append(message)
        key = next((k for k in self.answers if k in message.lower()), None)
        assert key is not None, f"unexpected question: {message}"
        return self.answers[key]

    def text(self, message, default=""):
        return self._answer(message, default)

    path = password = text

    def confirm(self, message, default):
        return self._answer(message, default)

    def select(self, message, choices, default):
        return self._answer(message, default)


def test_destinations_add_asks_through_configure_in_a_terminal(capsys, tmp_path, monkeypatch):
    import sys

    from minuta import setup

    run(capsys, *SETUP)
    vault = tmp_path / "Vault"
    vault.mkdir()
    answers = {
        "local": {"copy each meeting": True, "folder to copy": str(tmp_path / "copy")},
        "webhook": {"url": "https://example.com/in", "signing key": "k" * 32, "header": "", "audio": True},
        "command": {"program": f'"{sys.executable}" -c pass'},
        "obsidian": {"vault folder": str(vault), "inside the vault": "Calls"},
    }
    monkeypatch.setattr(cli, "interactive", lambda args: True)
    for kind, script in answers.items():
        p = Scripted(script)
        monkeypatch.setattr(setup, "Prompter", lambda interactive, p=p: p)
        code, out = run(capsys, "destinations", "add", f"my-{kind}", "--type", kind)
        assert code == 0, out
        assert len(p.asked) == len(script)
    saved = config_mod.load()["destinations"]
    assert saved["my-local"]["copy_to"] == str(tmp_path / "copy")
    assert saved["my-webhook"]["send"] == ["audio", "transcript", "metadata"]
    assert "headers_secret" not in saved["my-webhook"]
    assert saved["my-command"]["command"].endswith("-c pass")
    assert saved["my-obsidian"]["vault"] == str(vault) and saved["my-obsidian"]["folder"] == "Calls"
    # Given as --option, nothing is asked (Scripted fails on any question).
    monkeypatch.setattr(setup, "Prompter", lambda interactive: Scripted({}))
    code, out = run(capsys, "destinations", "add", "quiet", "--type", "obsidian", "--option", f"vault={vault}",
                    "--option", "folder=Meetings")
    assert code == 0, out


def test_destinations_add_stops_when_configure_cancels(capsys, monkeypatch):
    from minuta.destinations.local import LocalDestination

    run(capsys, *SETUP)
    monkeypatch.setattr(LocalDestination, "configure", classmethod(lambda cls, *a, **k: None))
    code, out = run(capsys, "destinations", "add", "nope", "--type", "local")
    assert code == 1 and "not added" in out
    assert "nope" not in config_mod.load()["destinations"]


def test_all_six_destinations_are_registered_and_declared_as_entry_points():
    from pathlib import Path

    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    from minuta import destinations

    kinds = {"local", "webhook", "command", "obsidian", "rclone", "notion"}
    assert set(destinations.registry()) >= kinds
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    eps = pyproject["project"]["entry-points"]["minuta.destinations"]
    assert set(eps) == kinds
    for kind, target in eps.items():
        module, cls = target.split(":")
        assert destinations.registry()[kind].__module__ == module and destinations.registry()[kind].__name__ == cls


def test_setup_adds_destinations_from_flags(capsys, tmp_path, monkeypatch):
    import sys

    vault = tmp_path / "Vault"
    vault.mkdir()
    code, out = run(capsys, *SETUP, "--destination", "webhook:hook", "--option", "url=https://example.com/minuta",
                    "--option", "signing_key=setup-signing-key-0123456789abcdef", "--option", "send=metadata",
                    "--destination", "obsidian", "--option", f"vault={vault}",
                    "--destination", "command", "--option", f'command="{sys.executable}" -c pass')
    assert code == 0, out
    cfg = config_mod.load()
    assert {"hook", "command", "obsidian"} <= set(cfg["destinations"])
    assert cfg["destinations"]["hook"]["signing_key_secret"] == "HOOK_SIGNING_KEY"
    assert cfg["destinations"]["hook"]["send"] == ["metadata"]
    assert cfg["destinations"]["obsidian"]["vault"] == str(vault)
    assert "setup-signing-key" not in out and "setup-signing-key" not in paths.config_file().read_text()
    assert config_mod.validate(cfg) == []
    # Without a key the webhook is not added, and setup still finishes; an unknown type is reported.
    run(capsys, "destinations", "remove", "hook")
    code, out = run(capsys, *SETUP, "--destination", "webhook", "--option", "url=https://example.com/minuta",
                    "--destination", "nope:x")
    assert code == 0 and "webhook" not in config_mod.load()["destinations"] and "signing_key" in out
    assert "unknown type 'nope'" in out
    # An --option needs the --destination it belongs to.
    with pytest.raises(SystemExit):
        cli.main([*SETUP, "--option", "url=https://example.com"])


def test_setup_assistant_asks_for_a_webhook(capsys, tmp_path):
    import argparse

    from minuta import setup
    from minuta.i18n import STRINGS

    class Answers(setup.Prompter):
        def __init__(self):
            super().__init__(True)
            self.asked = []

        def select(self, message, choices, default):
            self.asked.append(message)
            return "webhook"

        def text(self, message, default=""):
            self.asked.append(message)
            return "https://example.com/in"

        def password(self, message):
            self.asked.append(message)
            return "Authorization: Bearer abcdef123456" if "header" in message.lower() else "k" * 32

        def confirm(self, message, default):
            self.asked.append(message)
            return True

    run(capsys, *SETUP)
    cfg = config_mod.load()
    p = Answers()
    args = argparse.Namespace(destination=None)
    assert setup.destinations_step(cfg, args, p) == ["webhook"]
    assert cfg["destinations"]["webhook"]["send"] == ["audio", "transcript", "metadata"]
    assert all(q not in STRINGS for q in p.asked)  # every question has its text
    assert credentials.store(cfg).get("WEBHOOK_HEADERS") == "Authorization: Bearer abcdef123456"


@needs_ffmpeg
def test_retry_delivers_to_a_destination_added_later(capsys, tmp_path):
    run(capsys, *SETUP)
    cfg = config_mod.load()
    m = make_meeting(cfg)
    run(capsys, "destinations", "add", "later", "--type", "local", "--option", f"copy_to={tmp_path / 'later'}")
    code, out = run(capsys, "retry")
    assert code == 0 and "later:meeting" in out
    assert any((tmp_path / "later").iterdir())
    assert m.read()["destinations"]["later"]["meeting"]["status"] == "ok"
    assert "Nothing to retry" in run(capsys, "retry")[1]


@needs_ffmpeg
def test_transcribe_latest_with_local_engine(capsys, monkeypatch):
    from minuta import transcription

    class Engine:
        model = "fake"
        usd_per_minute = 0.0

        def transcribe_channel(self, x, segments, tcfg, budget=None, cost=None):
            return [{"start": a + 0.2, "end": b - 0.2, "text": "hello"} for a, b in segments]

    monkeypatch.setattr(transcription, "create_engine", lambda *a, **k: Engine())
    monkeypatch.setattr("minuta.transcription.pipeline.create_engine", lambda *a, **k: Engine())
    run(capsys, *SETUP)
    m = make_meeting(config_mod.load())
    code, out = run(capsys, "transcribe", "latest")
    assert code == 0, out
    assert m.has_transcript() and "transcript.md" in out
    code, out = run(capsys, "transcribe", m.id[:12])
    assert "already has a transcript" in out


@needs_ffmpeg
def test_paid_engine_asks_and_respects_the_cap(capsys, monkeypatch):
    calls = []
    monkeypatch.setattr("minuta.transcription.pipeline.transcribe", lambda *a, **k: calls.append(k))
    run(capsys, *SETUP, "--engine", "vercel")
    make_meeting(config_mod.load())
    code, out = run(capsys, "transcribe", "latest", "--estimate")
    assert code == 0 and "~US$" in out and not calls
    code, out = run(capsys, "transcribe", "latest", "--non-interactive")
    assert code == 3 and "--yes" in out and not calls
    code, out = run(capsys, "transcribe", "latest", "--yes", "--max-usd", "0.00001")
    assert code == 3 and "cap" in out and not calls
    code, out = run(capsys, "transcribe", "latest", "--yes")
    assert code == 0 and calls and calls[0]["budget"].max == 1.0


def test_doctor_reports_without_crashing(capsys):
    run(capsys, *SETUP, "--port", free_port())
    code, out = run(capsys, "doctor", "--json")
    checks = {c["check"]: c for c in json.loads(out)}
    assert checks["config"]["status"] == "ok"
    assert checks["server"]["status"] == "fail"  # nothing is running in tests
    assert "destination local" in checks


def test_pair_prints_a_code(capsys):
    run(capsys, *SETUP)
    code, out = run(capsys, "pair")
    assert code == 0 and "-" in out.split(":")[-1] and paths.pairing_file().exists()


@pytest.mark.parametrize("env, python", [(None, "3.12"), ("3.11", "3.11")])
def test_update_reinstalls_with_the_installers_python(capsys, monkeypatch, tmp_path, env, python):
    app = tmp_path / "app"
    app.mkdir()
    run(capsys, *SETUP, "--app-dir", str(app))
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli, "_download_tarball", lambda repo, ref, dest: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    if env:
        monkeypatch.setenv("MINUTA_PYTHON", env)
    else:
        monkeypatch.delenv("MINUTA_PYTHON", raising=False)
    code, out = run(capsys, "update", "--no-restart")
    assert code == 0
    install = next(c for c in calls if c[1:3] == ["tool", "install"])
    assert install[install.index("--python") + 1] == python


def fake_tools(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return calls


def test_update_follows_the_installed_tag_and_pins_the_lock(capsys, monkeypatch, tmp_path):
    app = tmp_path / "app"
    (app / ".git").mkdir(parents=True)
    (app / ".minuta-installer").touch()
    (app / "constraints.txt").write_text("requests==2.0\n")
    run(capsys, *SETUP, "--app-dir", str(app), "--install-ref", "v1.2.0")
    calls = fake_tools(monkeypatch)
    assert run(capsys, "update", "--no-restart")[0] == 0
    git = [c[3:] for c in calls if c[0] == "git"]
    assert git == [["fetch", "--depth", "1", "origin", "v1.2.0"], ["reset", "--hard", "FETCH_HEAD"]]
    install = next(c for c in calls if c[1:3] == ["tool", "install"])
    assert install[install.index("--constraints") + 1] == str(app / "constraints.txt")
    # A checkout of the user's own (no installer marker) is only fast-forwarded, never reset.
    (app / ".minuta-installer").unlink()
    calls.clear()
    assert run(capsys, "update", "--no-restart")[0] == 0
    assert [c[3:] for c in calls if c[0] == "git"] == [["pull", "--ff-only"]]


def test_update_without_git_replaces_the_folder_with_the_ref(capsys, monkeypatch, tmp_path):
    import io
    import tarfile
    import urllib.request

    app = tmp_path / "app"
    app.mkdir()
    (app / ".minuta-installer").touch()
    (app / "removed-upstream.py").write_text("old")
    run(capsys, *SETUP, "--app-dir", str(app), "--install-ref", "v2.0.0")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in (("minuta-2.0.0/pyproject.toml", b"[project]"), ("minuta-2.0.0/src/new.py", b"new"),
                           ("minuta-2.0.0/../escape.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text))
    urls = []

    def fake_urlopen(url, timeout):
        urls.append(url)
        return io.BytesIO(buf.getvalue())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("MINUTA_REPO_URL", "https://example.com/me/minuta")
    calls = fake_tools(monkeypatch)
    assert run(capsys, "update", "--no-restart")[0] == 0
    assert urls == ["https://example.com/me/minuta/archive/v2.0.0.tar.gz"]
    assert sorted(p.relative_to(app).as_posix() for p in app.rglob("*")) == [
        ".minuta-installer", "pyproject.toml", "src", "src/new.py"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".app")) == []
    assert not (tmp_path / "escape.txt").exists() and any(c[1:3] == ["tool", "install"] for c in calls)
    # A folder the installer did not make is never replaced.
    (app / ".minuta-installer").unlink()
    assert run(capsys, "update", "--no-restart")[0] == 1 and (app / "src" / "new.py").exists()


def test_uninstall_removes_config_and_optionally_data(capsys, monkeypatch):
    from minuta import service

    monkeypatch.setattr(service.ServiceManager, "uninstall", lambda self: None)
    run(capsys, *SETUP)
    cfg = config_mod.load()
    m = make_meeting(cfg, audio=False)
    code, out = run(capsys, "uninstall", "--yes", "--keep-tool")
    assert code == 0 and not paths.config_dir().exists() and m.dir.exists()
    run(capsys, *SETUP)
    code, out = run(capsys, "uninstall", "--yes", "--delete-data", "--keep-tool")
    assert code == 0 and not config_mod.meetings_dir(cfg).exists()


def test_uninstall_keeps_meetings_when_config_and_data_share_a_folder(capsys, monkeypatch, tmp_path):
    """macOS: platformdirs puts config and data in the same Application Support/minuta folder."""
    from minuta import service

    shared = tmp_path / "Application Support" / "minuta"
    monkeypatch.setattr(paths, "config_dir", lambda: shared)
    monkeypatch.setattr(paths, "default_data_dir", lambda: shared)
    monkeypatch.setattr(paths, "default_app_dir", lambda: shared / "app")
    monkeypatch.setattr(service.ServiceManager, "uninstall", lambda self: None)
    (shared / "app" / "extension").mkdir(parents=True)
    (shared / "app" / ".minuta-installer").touch()
    run(capsys, *SETUP, "--app-dir", str(shared / "app"))
    cfg = config_mod.load()
    m = make_meeting(cfg, audio=False)
    code, out = run(capsys, "uninstall", "--yes", "--keep-tool")
    assert code == 0 and m.dir.exists()
    assert sorted(p.name for p in shared.iterdir()) == ["meetings"]  # config, token and app are gone
    run(capsys, *SETUP)
    code, out = run(capsys, "uninstall", "--yes", "--delete-data", "--keep-tool")
    assert code == 0 and not shared.exists()


def test_uninstall_removes_a_read_only_installer_checkout(capsys, monkeypatch, tmp_path):
    """git checks out its objects read-only; on Windows a plain rmtree left the whole checkout behind."""
    import stat

    from minuta import service

    monkeypatch.setattr(service.ServiceManager, "uninstall", lambda self: None)
    app = tmp_path / "app"
    (app / ".git" / "objects" / "ab").mkdir(parents=True)
    (app / ".minuta-installer").touch()
    obj = app / ".git" / "objects" / "ab" / "cdef"
    obj.write_bytes(b"x")
    obj.chmod(stat.S_IREAD)
    run(capsys, *SETUP, "--app-dir", str(app))
    code, _ = run(capsys, "uninstall", "--yes", "--keep-tool")
    assert code == 0 and not app.exists()


def test_windows_uninstall_script_quotes_paths():
    script = cli.windows_uninstall_script(42, r"C:\it's\uv.exe", [Path(r"C:\tools\minuta")], [], Path("log"))
    assert "Wait-Process -Id 42" in script
    assert "& 'C:\\it''s\\uv.exe' tool uninstall minuta" in script
    assert "$targets = @('C:\\tools\\minuta')" in script


@pytest.mark.skipif(sys.platform != "win32", reason="the deferred uninstall runs PowerShell, only on Windows")
def test_windows_uninstall_finishes_after_the_process_exits(tmp_path):
    import stat

    env = tmp_path / "tools" / "minuta"
    env.mkdir(parents=True)
    fake_uv = tmp_path / "uv.cmd"
    fake_uv.write_text(f'@rmdir /s /q "{env}"\r\n', encoding="ascii")
    command = tmp_path / "bin" / "minuta.exe"  # a command uv did not remove: the script deletes it
    command.parent.mkdir()
    command.write_bytes(b"x")
    leftover = tmp_path / "app"
    (leftover / "sub").mkdir(parents=True)
    (leftover / "sub" / "obj").write_bytes(b"x")
    (leftover / "sub" / "obj").chmod(stat.S_IREAD)
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    log = tmp_path / "uninstall.log"
    script = cli.windows_uninstall_script(done.pid, str(fake_uv), [env, command], [leftover], log)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=120)
    assert not env.exists() and not command.exists() and not leftover.exists() and not log.exists()


def test_non_interactive_setup_never_opens_a_browser(capsys, monkeypatch):
    from minuta import browser

    opened = []
    monkeypatch.setattr(browser, "detect", lambda: [browser.Browser("Brave", "/x/brave", "brave://extensions")])
    monkeypatch.setattr(browser, "open_extensions_page", opened.append)
    code, out = run(capsys, "setup", "--non-interactive", "--no-service")
    assert code == 0 and "brave://extensions" in out and opened == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_serve_exits_cleanly_on_sigterm(home):
    """launchd restarts a job that dies from a signal; `minuta service stop` sends SIGTERM."""
    run_ = subprocess.run([sys.executable, "-m", "minuta", "setup", *SETUP[1:]], capture_output=True)
    assert run_.returncode == 0
    proc = subprocess.Popen([sys.executable, "-m", "minuta", "serve", "--port", "0"], stderr=subprocess.PIPE)
    try:
        end = time.time() + 60  # a cold start on a CI runner can take long
        while not (paths.config_dir() / "server.pid").exists() and time.time() < end and proc.poll() is None:
            time.sleep(0.1)
        assert (paths.config_dir() / "server.pid").exists(), proc.stderr.read() if proc.poll() is not None else ""
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=10)
        assert code == 0, proc.stderr.read()
    finally:
        proc.kill()


@pytest.mark.parametrize("argv", [["--version"], ["service", "--help"]])
def test_basic_flags(capsys, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 0


def test_option_reads_secret_from_environment(monkeypatch):
    from minuta.setup import parse_options

    monkeypatch.setenv("MY_TOKEN", "s3cret-value")
    assert parse_options(["token@env=MY_TOKEN", "url=https://x"]) == {"token": "s3cret-value", "url": "https://x"}
    monkeypatch.delenv("MY_TOKEN")
    import pytest
    with pytest.raises(ValueError):
        parse_options(["token@env=MY_TOKEN"])
