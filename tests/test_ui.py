"""Dates, durations and the extension folder the CLI shows."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from minuta import browser, ui
from minuta.i18n import set_language


@pytest.fixture(autouse=True)
def english():
    yield
    set_language("en")


def test_dates_are_local_and_in_the_language():
    local = datetime(2026, 11, 12, 22, 50, tzinfo=timezone.utc).astimezone()
    set_language("es")
    months = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    assert ui.when("2026-11-12T22:50:21Z") == f"{local.day} {months[local.month - 1]} {local.year}, {local:%H:%M}"
    set_language("en")
    assert ui.when("2026-11-12T22:50:21Z").startswith(f"{local.day} {local:%b}")
    assert ui.when(None) == "-"


@pytest.mark.parametrize("seconds, text", [(48, "48 s"), (59.6, "1 min"), (720, "12 min"), (3900, "1 h 05 min"),
                                           (None, "-")])
def test_durations(seconds, text):
    assert ui.duration(seconds) == text


def test_full_name_comes_from_the_system(monkeypatch):
    from minuta import setup

    if setup.sys.platform == "linux":
        class Pw:
            pw_gecos = "Ana Pérez,,,"

        monkeypatch.setattr("pwd.getpwuid", lambda uid: Pw())
        assert setup.full_name() == "Ana Pérez"
        Pw.pw_gecos = ""
        monkeypatch.setattr(setup.getpass, "getuser", lambda: "ana")
        assert setup.full_name() == "ana"


def test_the_extension_is_copied_to_a_visible_folder_and_kept_in_sync(tmp_path, home):
    src = tmp_path / "app" / "extension"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text('{"name": "minuta"}')
    (src / "old.js").write_text("1")
    cfg = {"general": {"app_dir": str(tmp_path / "app")}}
    dest = browser.install_extension(cfg)
    assert dest == home / "minuta-extension" and " " not in dest.name
    assert (dest / "old.js").exists() and browser.extension_dir(cfg) == dest
    (src / "old.js").unlink()
    (src / "new.js").write_text("2")
    sync = browser.sync_extension(cfg)
    assert sync.copied and not (dest / "old.js").exists() and (dest / "new.js").read_text() == "2"
    assert browser.remove_extension_copy(cfg) and not dest.exists()


def test_a_folder_that_is_not_ours_is_never_overwritten(tmp_path, home):
    src = tmp_path / "app" / "extension"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text("{}")
    mine = home / "minuta-extension"
    mine.mkdir(parents=True)
    (mine / "notes.txt").write_text("mine")
    cfg = {"general": {"app_dir": str(tmp_path / "app")}}
    assert browser.install_extension(cfg) == src
    assert (mine / "notes.txt").read_text() == "mine" and not browser.remove_extension_copy(cfg)


def test_update_says_when_the_browser_still_loads_the_old_folder(tmp_path, home, monkeypatch):
    src = tmp_path / "Application Support" / "minuta" / "app" / "extension"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text('{"name": "minuta"}')
    profile = tmp_path / "Brave" / "Default"
    profile.mkdir(parents=True)
    prefs = {"extensions": {"settings": {"abc": {"path": str(src), "location": 4},
                                         "store": {"path": "store-ext/1.0"}}}}
    (profile / "Secure Preferences").write_text(json.dumps(prefs))
    monkeypatch.setattr(browser, "profile_roots", lambda: [tmp_path / "Brave"])
    cfg = {"general": {"app_dir": str(src.parent)}}
    assert browser.loaded_extension_paths() == [src]
    sync = browser.sync_extension(cfg)
    assert sync.old_folder_in_use and sync.old_folder == src and sync.folder == home / "minuta-extension"
    # Once the browser loads the new folder, nothing is said.
    prefs["extensions"]["settings"]["abc"]["path"] = str(home / "minuta-extension")
    (profile / "Secure Preferences").write_text(json.dumps(prefs))
    assert not browser.sync_extension(cfg).old_folder_in_use
