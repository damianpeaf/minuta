"""The command destination with real programs (Python scripts run by the current interpreter)."""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import pytest

from minuta.destinations import DestinationError, MeetingParts, PermanentError
from minuta.destinations.command import CommandDestination, parse_command
from minuta.meetings import MeetingStore
from minuta.redact import register_secret

# Writes what it received (arguments, environment, working folder) next to itself.
RECORDER = r'''
import json, os, sys
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl")
with open(out, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}) + "\n")
print("recorded", sys.argv[-2:])
'''

WEIRD = "it's \"quoted\" $(touch pwned) `id` ; rm -rf x | y & {meeting_id}* ñ"


@pytest.fixture
def recorder(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "recorder.py"
    script.write_text(RECORDER, encoding="utf-8")
    return script


def calls(script):
    path = script.parent / "calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def make_meeting(root):
    m = MeetingStore(root).get("2026-02-02T09-00-00-000Z_xyz-abcd-efg")
    m.update(status="ready", title="t")
    return m


def cmd(command, **opts) -> CommandDestination:
    return CommandDestination("script", {"type": "command", "command": command, **opts})


def script_file(tmp_path, name, code):
    p = tmp_path / name
    p.write_text(code, encoding="utf-8")
    return p


# --- arguments ---------------------------------------------------------------------------
def test_event_and_folder_are_appended_and_in_the_environment(tmp_path, recorder):
    m = make_meeting(tmp_path / "meetings")
    d = cmd([sys.executable, str(recorder), "--flag"])
    assert d.check().ok
    r = d.send_meeting(m, MeetingParts(metadata={"id": m.id}))
    d.send_transcript(m, {})
    first, second = calls(recorder)
    assert first["argv"] == ["--flag", "meeting.ready", str(m.dir)]
    assert second["argv"] == ["--flag", "transcript.ready", str(m.dir)]
    assert first["env"]["MINUTA_EVENT"] == "meeting.ready" and first["env"]["MINUTA_MEETING_ID"] == m.id
    assert first["env"]["MINUTA_MEETING_DIR"] == str(m.dir) and first["env"]["MINUTA_DESTINATION"] == "script"
    assert os.path.samefile(first["cwd"], m.dir)
    assert r.extra["exit_code"] == 0 and r.audio is False


def test_odd_characters_reach_the_program_verbatim_without_a_shell(tmp_path, recorder):
    root = tmp_path / "dir with spaces ; $(touch pwned) & {event} ñ" / "meetings"
    m = make_meeting(root)
    weird_arg = WEIRD
    d = cmd([sys.executable, str(recorder), weird_arg, "--dir={meeting_dir}", "{event}:{meeting_id}"])
    d.send_meeting(m, MeetingParts(metadata={"id": m.id}))
    (call,) = calls(recorder)
    # Placeholders are replaced once; everything else is passed as is.
    assert call["argv"] == [WEIRD.replace("{meeting_id}", m.id), f"--dir={m.dir}", f"meeting.ready:{m.id}"]
    assert "{event}" in str(m.dir)  # a placeholder inside the folder name itself is not expanded again
    assert not list(tmp_path.rglob("pwned")) and not (m.dir / "pwned").exists()


def test_a_command_line_string_is_split_but_never_run_by_a_shell(tmp_path, recorder):
    m = make_meeting(tmp_path / "meetings")
    line = f'"{sys.executable}" "{recorder}" "two words" \'$HOME\' ";" "$(touch pwned)"'
    assert parse_command(line)[2:] == ["two words", "$HOME", ";", "$(touch pwned)"]
    cmd(line).send_meeting(m, MeetingParts(metadata={"id": m.id}))
    (call,) = calls(recorder)
    assert call["argv"][:4] == ["two words", "$HOME", ";", "$(touch pwned)"]
    assert not list(tmp_path.rglob("pwned"))
    assert parse_command(["a b", "c"]) == ["a b", "c"]


# --- environment -------------------------------------------------------------------------
def test_minimal_environment_without_minuta_secrets(tmp_path, recorder, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vk_should_not_leak_123456")
    monkeypatch.setenv("HOOK_SIGNING_KEY", "signing-key-should-not-leak")
    monkeypatch.setenv("MINUTA_HOME_EXTRA", "nope")
    monkeypatch.setenv("UNRELATED_VARIABLE", "nope")
    monkeypatch.setenv("NOTES_DIR", "/notes")
    monkeypatch.setenv("SNEAKY", "signing-key-should-not-leak")
    register_secret("signing-key-should-not-leak")  # as SecretStore.get does when a secret is read
    m = make_meeting(tmp_path / "meetings")
    d = cmd([sys.executable, str(recorder)], pass_env=["NOTES_DIR", "SNEAKY", "AI_GATEWAY_API_KEY", "MINUTA_HOME"])
    d.send_meeting(m, MeetingParts(metadata={"id": m.id}))
    env = calls(recorder)[0]["env"]
    assert env.get("NOTES_DIR") == "/notes"
    for name in ("AI_GATEWAY_API_KEY", "HOOK_SIGNING_KEY", "SNEAKY", "MINUTA_HOME", "MINUTA_HOME_EXTRA",
                 "MINUTA_NO_KEYRING", "UNRELATED_VARIABLE", "VIRTUAL_ENV", "PYTHONPATH"):
        assert name not in env, name
    assert "signing-key-should-not-leak" not in json.dumps(env) and "vk_should_not_leak" not in json.dumps(env)
    assert set(env) <= {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "NOTES_DIR",
                        "MINUTA_EVENT", "MINUTA_MEETING_ID", "MINUTA_MEETING_DIR", "MINUTA_DESTINATION",
                        "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
                        "USERNAME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                        "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}


# --- failures ----------------------------------------------------------------------------
def test_non_zero_exit_is_retryable(tmp_path):
    m = make_meeting(tmp_path / "meetings")
    failing = script_file(tmp_path, "fail.py", "import sys\nprint('partial work')\n"
                                               "sys.stderr.write('disk full\\n')\nsys.exit(3)\n")
    with pytest.raises(DestinationError, match="exited with 3: disk full") as e:
        cmd([sys.executable, str(failing)]).send_meeting(m, MeetingParts(metadata={"id": m.id}))
    assert not isinstance(e.value, PermanentError)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the process group with os.kill")
def test_timeout_stops_the_program_and_its_children(tmp_path):
    m = make_meeting(tmp_path / "meetings")
    pidfile = tmp_path / "child.pid"
    slow = script_file(tmp_path, "slow.py",
                       "import subprocess, sys, time\n"
                       f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                       f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
                       "time.sleep(60)\n")
    start = time.monotonic()
    with pytest.raises(DestinationError, match="did not finish within 1 s") as e:
        cmd([sys.executable, str(slow)], timeout_seconds=1).send_meeting(m, MeetingParts(metadata={}))
    assert time.monotonic() - start < 10 and not isinstance(e.value, PermanentError)
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the child process outlived the timeout")


def test_output_is_truncated_and_masked_in_the_log(tmp_path, caplog):
    register_secret("tok_secret_value_123456")
    m = make_meeting(tmp_path / "meetings")
    noisy = script_file(tmp_path, "noisy.py", "print('A' * 100000)\nprint('token tok_secret_value_123456 end')\n")
    with caplog.at_level(logging.INFO, logger="minuta.destinations.command"):
        cmd([sys.executable, str(noisy)]).send_meeting(m, MeetingParts(metadata={}))
    text = caplog.text
    assert "tok_secret_value_123456" not in text and "***" in text
    assert "A" * 3900 in text and "A" * 4100 not in text


def test_check_reports_missing_programs_and_bad_options(tmp_path):
    assert not cmd(["definitely-not-a-program-xyz"]).check().ok
    assert not cmd([]).check().ok
    assert not cmd("").check().ok
    assert not cmd("[not json").check().ok
    assert not cmd([sys.executable], timeout_seconds="soon").check().ok
    assert cmd([sys.executable, "-c", "pass"]).check().ok
    m = make_meeting(tmp_path / "meetings")
    with pytest.raises(PermanentError, match="not found"):
        cmd(["definitely-not-a-program-xyz"]).send_meeting(m, MeetingParts(metadata={}))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_batch_files_are_refused_on_windows(tmp_path):
    bat = script_file(tmp_path, "x.bat", "@echo off\n")
    assert ".bat and .cmd" in cmd([str(bat)]).check().message
