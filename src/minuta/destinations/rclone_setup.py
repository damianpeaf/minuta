"""Interactive part of `minuta destinations add NAME --type rclone`: pick or create a remote, pick a folder.

Creating a remote is always rclone's own interactive command (`rclone config create`, `rclone config`)
running in the user's terminal: the OAuth sign-in and the credentials stay between the user and rclone.
"""

from __future__ import annotations

import subprocess

from minuta.destinations.base import DestinationError, PermanentError
from minuta.i18n import t

NEW_DRIVE, NEW_OTHER = "__new_drive__", "__new_other__"


def _ask_valid(prompter, message: str, default: str, validate) -> str | None:
    for _ in range(5):
        value = prompter.text(message, default)
        try:
            return validate(value)
        except ValueError as e:
            print(t("rclone.invalid", error=e))
    return None


def configure(cls, name: str, options: dict, prompter=None, secrets=None, runner=subprocess.run) -> dict | None:
    """Returns the options to save, or None if the user cannot continue (no rclone, cancelled)."""
    from minuta.destinations.rclone import validate_folder, validate_remote

    if options.get("remote") or prompter is None:
        return options  # given with --option, or nobody to ask: check() reports what is missing
    probe = cls(name, options, secrets, runner)  # a config_password typed with --option is used as is
    try:
        binary = probe.binary()
    except PermanentError as e:
        print(e)
        return None
    config = ["--config", probe.config_path] if probe.config_path else []
    print(t("rclone.intro"))
    default = None
    for _ in range(5):
        try:
            remotes = probe.listremotes()
        except DestinationError as e:
            print(t("rclone.list_failed", error=e))
            return None
        choices = [(r, r) for r in remotes] + [(NEW_DRIVE, t("rclone.new_drive")), (NEW_OTHER, t("rclone.new_other"))]
        if default not in remotes:
            default = remotes[0] if remotes else NEW_DRIVE
        choice = prompter.select(t("rclone.pick_remote"), choices, default)
        if choice == NEW_DRIVE:
            remote = _ask_valid(prompter, t("rclone.drive_name"), "gdrive", validate_remote)
            if remote is None:
                return None
            if remote not in remotes:
                print(t("rclone.drive_explain", remote=remote))
                # rclone opens the browser for Google's sign-in. drive.file: rclone only sees what it creates.
                runner([binary, *config, "config", "create", remote, "drive", "scope=drive.file"], shell=False)
            default = remote
            continue
        if choice == NEW_OTHER:
            print(t("rclone.other_explain"))
            runner([binary, *config, "config"], shell=False)
            continue
        remote = validate_remote(choice)
        break
    else:
        return None
    folder = _ask_valid(prompter, t("rclone.folder"), options.get("folder") or "minuta", validate_folder)
    if folder is None:
        return None
    return {**options, "remote": remote, "folder": folder}
