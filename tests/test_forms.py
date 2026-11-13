"""The questions of each destination, as data: the terminal and the extension's page answer the same
fields and end with the same configuration."""

from __future__ import annotations

import os
import sys

import pytest

from minuta import actions, credentials, forms
from minuta import config as config_mod
from minuta.destinations import notion
from minuta.destinations.rclone_setup import NEW_DRIVE
from minuta.setup import add_destination

from .notion_fake import TOKEN
from .test_cli import Scripted
from .test_notion_destination import fake as notion_fake  # noqa: F401 - fixture
from .test_rclone_destination import Fake as RcloneFake
from .test_rclone_destination import posix_only

KEY = "k" * 32


def through_the_terminal(kind: str, script: dict) -> tuple[dict, dict]:
    cfg = config_mod.load()
    p = Scripted(script)
    ok, message = add_destination(cfg, "dest", kind, {}, p)
    assert ok, message
    assert len(p.asked) == len(script), p.asked
    config_mod.save(cfg)
    return _saved()


def through_the_page(kind: str, answers: dict) -> tuple[dict, dict]:
    """As app.js does: ask for a step, answer the fields it shows, ask again until it is complete."""
    cfg = config_mod.load()
    given: dict = {}
    step = actions.destination_form(cfg, kind, "dest", given)
    while not step["complete"]:
        assert not step["errors"], step["errors"]
        for f in step["fields"]:
            given[f["key"]] = answers[f["key"]]
        step = actions.destination_form(cfg, kind, "dest", given)
    assert given == answers  # every answer was asked for
    out = actions.add_destination(kind, "dest", given)
    assert out["ok"], out
    return _saved()


def _saved() -> tuple[dict, dict]:
    cfg = config_mod.load()
    opts = dict(cfg["destinations"]["dest"])
    store = credentials.store(cfg)
    secrets = {k: store.get(v) for k, v in opts.items() if k.endswith("_secret")}
    actions.remove_destination("dest")
    return opts, secrets


def same_config(kind: str, script: dict, answers: dict) -> dict:
    tui = through_the_terminal(kind, script)
    web = through_the_page(kind, answers)
    assert tui == web
    return tui[0]


def test_local(cfg, tmp_path):
    opts = same_config("local", {"copy each meeting": True, "folder to copy": str(tmp_path / "copy")},
                       {"_copy": True, "copy_to": str(tmp_path / "copy")})
    assert opts["copy_to"] == str(tmp_path / "copy") and "_copy" not in opts


def test_webhook(cfg):
    header = "Authorization: Bearer abcdef123456"
    url = "https://example.com/in"
    opts = same_config("webhook", {"url": url, "signing key": KEY, "header": header, "audio": True},
                       {"url": url, "signing_key": KEY, "headers": header, "_audio": True})
    assert opts["send"] == ["audio", "transcript", "metadata"] and "_audio" not in opts
    assert "signing_key" not in opts and opts["signing_key_secret"] == "DEST_SIGNING_KEY"


def test_command(cfg):
    command = f'"{sys.executable}" -c pass'
    assert same_config("command", {"program": command}, {"command": command})["command"] == command


def test_obsidian(cfg, tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    opts = same_config("obsidian", {"vault folder": str(vault), "inside the vault": "Calls"},
                       {"vault": str(vault), "folder": "Calls"})
    assert opts["vault"] == str(vault) and opts["folder"] == "Calls"


def test_notion(cfg, notion_fake):  # noqa: F811
    db = notion_fake.add_database({"Name": {"type": "title"}})
    opts = same_config("notion", {"integration secret": TOKEN, "where should": "existing", "of the database": db},
                       {"token": TOKEN, "_how": "existing", "database": db})
    assert opts["database"] == notion.parse_id(db) and opts["token_secret"] == "DEST_TOKEN"


@posix_only
def test_rclone(cfg, tmp_path, monkeypatch):
    fake = RcloneFake(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"{fake.bin_dir}:{os.environ['PATH']}")
    opts = same_config("rclone", {"where should": "gdrive", "inside the remote": "Calls/2026"},
                       {"_remote": "gdrive", "folder": "Calls/2026"})
    assert opts["remote"] == "gdrive" and opts["folder"] == "Calls/2026" and "_remote" not in opts


# --- what the page receives ------------------------------------------------------------------------
def test_the_webhook_form_is_one_step_and_validated_on_the_server(cfg):
    step = actions.destination_form(cfg, "webhook", "hook", {})
    fields = {f["key"]: f for f in step["fields"]}
    assert list(fields) == ["url", "signing_key", "headers", "_audio"]
    assert fields["signing_key"]["kind"] == "secret" and fields["signing_key"]["required"]
    assert fields["_audio"]["kind"] == "bool" and fields["_audio"]["default"] is False
    assert all(f["label"] and not f["label"].startswith("setup.") for f in step["fields"])
    bad = actions.destination_form(cfg, "webhook", "hook", {"url": "http://example.com/x", "signing_key": "short",
                                                             "headers": "no colon", "_audio": False})
    assert not bad["complete"] and set(bad["errors"]) == {"url", "signing_key", "headers"}
    assert "https" in bad["errors"]["url"]
    good = actions.destination_form(cfg, "webhook", "hook", {"url": "https://example.com/x", "signing_key": KEY,
                                                              "headers": "", "_audio": False})
    assert good["complete"]


def test_notion_asks_in_two_steps(cfg):
    first = actions.destination_form(cfg, "notion", "notes", {})
    assert [f["key"] for f in first["fields"]] == ["token", "_how"]
    assert first["fields"][0]["help"] and [c["value"] for c in first["fields"][1]["choices"]] == ["existing", "create"]
    second = actions.destination_form(cfg, "notion", "notes", {"token": TOKEN, "_how": "create"})
    assert [f["key"] for f in second["fields"]] == ["parent_page"]
    third = actions.destination_form(cfg, "notion", "notes", {"token": TOKEN, "_how": "create", "parent_page": "nope"})
    assert "parent_page" in third["errors"]


@posix_only
def test_rclone_choices_that_need_a_terminal_are_marked_and_refused(cfg, tmp_path, monkeypatch):
    fake = RcloneFake(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"{fake.bin_dir}:{os.environ['PATH']}")
    step = actions.destination_form(cfg, "rclone", "drive", {})
    remote = step["fields"][0]
    assert {c["value"]: c["terminal"] for c in remote["choices"]}["gdrive"] is False
    assert {c["value"]: c["terminal"] for c in remote["choices"]}[NEW_DRIVE] is True
    assert "terminal" in remote["terminal_hint"]
    refused = actions.destination_form(cfg, "rclone", "drive", {"_remote": NEW_DRIVE})
    assert "_remote" in refused["errors"]


def test_secrets_never_come_back_in_a_step(cfg):
    step = actions.destination_form(cfg, "webhook", "hook", {"url": "https://example.com/x", "signing_key": KEY})
    assert KEY not in repr(step)


def test_names_and_types_are_checked(cfg):
    with pytest.raises(ValueError, match="invalid destination name"):
        actions.destination_form(cfg, "local", "Bad Name", {})
    with pytest.raises(ValueError, match="unknown destination type"):
        actions.destination_form(cfg, "nope", "x", {})


def test_a_terminal_asks_again_until_an_answer_is_valid(cfg, capsys):
    from minuta.destinations.webhook import WebhookDestination

    class Seq:
        def __init__(self, *answers):
            self.answers = list(answers)

        def text(self, message, default=""):
            return self.answers.pop(0)

        password = text

        def confirm(self, message, default):
            return self.answers.pop(0)

    opts = forms.ask(WebhookDestination, "hook", {"send": ["metadata"]},
                     Seq("ftp://x", "https://example.com/x", "short", KEY, "", False))
    assert opts["url"] == "https://example.com/x" and opts["signing_key"] == KEY
    assert capsys.readouterr().out.count("Not valid") == 2
