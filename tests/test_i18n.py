"""Every CLI text exists in English and Spanish, and a Spanish session shows no stray English."""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from minuta import cli
from minuta import config as config_mod
from minuta.i18n import STRINGS, forced_language, language, set_language, t
from minuta.meetings import MeetingStore

from .test_cli import SETUP, free_port, run

SRC = Path(__file__).resolve().parents[1] / "src" / "minuta"
# Words that leaked in English before: none may show up in a Spanish session.
ENGLISH = re.compile(r"\b(ready|enabled|disabled|sends|available types|Yes|no transcript|not installed|answering|"
                     r"paired|last seen|without transcript|failed deliveries|recording|Others|What do you want)\b")


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_every_key_has_english_and_spanish_with_the_same_placeholders():
    for key, entry in STRINGS.items():
        assert set(entry) == {"en", "es"}, key
        assert entry["en"].strip() and entry["es"].strip(), key
        assert fields(entry["en"]) == fields(entry["es"]), key


def test_every_key_the_code_uses_exists():
    used = set()
    for f in SRC.rglob("*.py"):
        used |= set(re.findall(r'\bt\("([A-Za-z0-9_.\-]+)"', f.read_text(encoding="utf-8")))
    assert used - set(STRINGS) == set()


def test_forced_language_restores_the_previous_one():
    set_language("es")
    with forced_language("en"):
        assert t("answer.yes") == "Yes"
    assert language() == "es" and t("answer.yes") == "Sí"
    set_language("en")


@pytest.fixture
def spanish(capsys, tmp_path):
    run(capsys, *SETUP, "--language", "es", "--port", free_port(), "--name", "Ana",
        "--destination", "local", "--option", f"copy_to={tmp_path / 'Docs'}")
    cfg = config_mod.load()
    m = MeetingStore(config_mod.meetings_dir(cfg)).get("2026-11-12T22-50-21-178Z_abc-defg-hij")
    m.update(status="ready", code="abc-defg-hij", started_at="2026-11-12T22:50:21Z", duration_seconds=48)
    yield cfg
    set_language("en")


@pytest.mark.parametrize("argv", [["list"], ["status"], ["destinations"], ["doctor"], ["pair"], ["mode"],
                                  ["retry"], ["transcribe", "nope"]])
def test_spanish_commands_have_no_english_words(capsys, spanish, argv):
    code, out = run(capsys, *argv)
    assert out.strip()
    assert not ENGLISH.findall(out), out


def test_the_spanish_menu_separates_each_action_and_speaks_spanish(capsys, spanish, monkeypatch):
    from minuta import setup

    answers = iter(["status", "status", "list", "destinations", "quit"])
    asked = []

    def select(self, message, choices, default):
        asked.append((message, [label for _, label in choices]))
        return next(answers)

    monkeypatch.setattr(setup.Prompter, "select", select)
    assert cli.cmd_menu(None) == 0
    out = capsys.readouterr().out
    assert not ENGLISH.findall(out), out
    assert out.count("-- Estado --") == 2 and "-- Ver reuniones --" in out and "-- Destinos --" in out
    assert asked[0][0] == "¿Qué quieres hacer?" and "Salir" in asked[0][1]
    assert "1 sin transcribir" in out.splitlines()[0]  # the header line


def test_confirm_answers_yes_and_no_in_spanish(monkeypatch):
    from minuta import setup

    set_language("es")
    seen = []
    monkeypatch.setattr(setup.Prompter, "select", lambda self, m, choices, d: seen.append((choices, d)) or "no")
    assert setup.Prompter(True).confirm("¿Seguro?", True) is False
    assert seen == [([("yes", "Sí"), ("no", "No")], "yes")]
    set_language("en")
