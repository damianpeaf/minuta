"""Questions of `minuta destinations add NAME --type rclone`: pick or create a remote, pick a folder.

Creating a remote is always rclone's own interactive command (`rclone config create`, `rclone config`)
running in the user's terminal: the OAuth sign-in and the credentials stay between the user and rclone.
So those two choices exist only in a terminal; the extension's page lists the remotes that exist.
"""

from __future__ import annotations

import subprocess

from minuta.destinations.base import DestinationError
from minuta.i18n import t

NEW_DRIVE, NEW_OTHER = "__new_drive__", "__new_other__"


def form(cls, name: str, known: dict, secrets=None, runner=subprocess.run) -> list:
    from minuta.destinations.rclone import validate_folder, validate_remote
    from minuta.forms import Field

    if known.get("remote"):
        return []  # given with --option: check() reports what is missing
    if "_remote" not in known:
        probe = cls(name, known, secrets, runner)  # a config_password typed with --option is used as is
        probe.binary()  # PermanentError when rclone is not installed
        try:
            remotes = probe.listremotes()
        except DestinationError as e:
            raise DestinationError(t("rclone.list_failed", error=e)) from None
        default = known.get("_last") if known.get("_last") in remotes else (remotes[0] if remotes else NEW_DRIVE)
        choices = [(r, r) for r in remotes] + [(NEW_DRIVE, "rclone.new_drive"), (NEW_OTHER, "rclone.new_other")]
        return [Field("_remote", "select", "rclone.pick_remote", help="rclone.intro", default=default, choices=choices,
                      terminal=(NEW_DRIVE, NEW_OTHER), terminal_hint="rclone.in_terminal")]
    if known["_remote"] == NEW_DRIVE:
        return [Field("_drive_name", "text", "rclone.drive_name", default="gdrive", required=True,
                      validate=validate_remote)]
    if "folder" not in known:
        return [Field("folder", "text", "rclone.folder", default="minuta", required=True, validate=validate_folder)]
    return []


def form_effect(cls, name: str, known: dict, secrets=None, runner=subprocess.run) -> dict:
    """In the terminal: run rclone's own setup for a new remote, then ask for the remote again."""
    choice = known.get("_remote")
    if choice not in (NEW_DRIVE, NEW_OTHER) or (choice == NEW_DRIVE and "_drive_name" not in known):
        return known
    probe = cls(name, known, secrets, runner)
    binary = probe.binary()
    config = ["--config", probe.config_path] if probe.config_path else []
    out = {k: v for k, v in known.items() if k not in ("_remote", "_drive_name")}
    if choice == NEW_DRIVE:
        remote = known["_drive_name"]
        if remote not in probe.listremotes():
            print(t("rclone.drive_explain", remote=remote))
            # rclone opens the browser for Google's sign-in. drive.file: rclone only sees what it creates.
            runner([binary, *config, "config", "create", remote, "drive", "scope=drive.file"], shell=False)
        out["_last"] = remote
    else:
        print(t("rclone.other_explain"))
        runner([binary, *config, "config"], shell=False)
    return out


def configure(cls, name: str, options: dict, prompter=None, secrets=None, runner=subprocess.run) -> dict | None:
    """Returns the options to save, or None if the user cannot continue in a terminal (no rclone)."""
    from minuta.destinations.rclone import validate_remote

    try:
        opts = cls.ask(name, options, prompter, secrets, runner=runner)
    except DestinationError as e:
        if not getattr(prompter, "terminal", True):
            raise
        print(e)
        return None
    if opts is None:
        return None
    if "_remote" in opts:
        opts["remote"] = validate_remote(opts.pop("_remote"))
    opts.pop("_last", None)
    opts.pop("_drive_name", None)
    return opts
