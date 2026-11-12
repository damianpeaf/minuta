"""The built-in `rclone` destination: Google Drive, Dropbox, OneDrive, S3, SFTP, WebDAV and ~70 more.

minuta never handles the cloud credentials. The user sets up a remote once with rclone itself
(`rclone config`), and this adapter runs the `rclone` program against it:

    [destinations.drive]
    type = "rclone"
    remote = "gdrive"          # a remote from `rclone listremotes`, without the colon
    folder = "minuta"          # folder inside the remote; created on the first upload
    # config = "/path/rclone.conf"   # optional: another rclone config file
    # binary = "/path/to/rclone"     # optional: rclone is looked up on PATH otherwise

Each meeting goes to `<remote>:<folder>/<YYYY-MM-DD HHMM title>/` with the audio, a `meeting.json`
holding only the public metadata, and the transcript files. Every upload is checked with
`rclone check` (hashes when both sides share one, sizes otherwise) before the receipt says the
remote holds the audio.

Security: rclone always runs with an argument list (never through a shell), the remote, folder and
paths are validated before they reach the command line, `--` separates options from paths, nothing
secret is put on the command line, and rclone never stops to ask for a password. An encrypted
rclone config can be unlocked with the `config_password` secret, which is handed to rclone through
the `RCLONE_CONFIG_PASS` environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from minuta.destinations.base import (
    Capabilities,
    CheckResult,
    Destination,
    DestinationError,
    MeetingParts,
    PermanentError,
    Receipt,
    safe_name,
)
from minuta.meetings import AUDIO_NAMES, METADATA_KEYS, TRANSCRIPT_FILES, parse_time
from minuta.redact import redact

log = logging.getLogger(__name__)

# rclone accepts letters, digits, `_ - . + @` and spaces in remote names; it must not start with `-` or
# a space. We are a bit stricter (no `:`/`/`, bounded length) so the value can only ever be a remote.
REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+@ -]{0,63}$")
# Characters refused in a remote folder. There is no shell, so `; $ ` & |` could do no harm, but a folder
# name never needs them and refusing them keeps a pasted command from ever looking like a path. The
# rest break paths on some backend or would make rclone read the value as something else.
_BAD_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f\\:*?\"<>|;$`&]")

# rclone exit codes (https://rclone.org/docs/#exit-code). Most runtime failures also exit with 1, so
# only 7 (fatal) is permanent by itself; otherwise stderr decides.
EXIT_DIR_NOT_FOUND, EXIT_FATAL = 3, 7

# stderr fragments that mean "fix your configuration": retrying cannot help.
_PERMANENT_PATTERNS = re.compile(
    r"didn't find section in config file|couldn't find (?:remote|section)|"
    r"unable to decrypt configuration|couldn't decrypt config|config file is encrypted|"
    r"invalid_grant|invalid_client|unauthorized_client|token (?:has been )?(?:expired|revoked)|"
    r"empty token found|failed to (?:get|refresh) token|\b401\b|\b403\b|unauthori[sz]ed|forbidden|access ?denied|"
    r"InvalidAccessKeyId|SignatureDoesNotMatch|unable to authenticate|no supported methods remain|"
    r"unexpected message type 51|permission denied|"
    r"unknown flag|unknown command|Usage:",
    re.IGNORECASE,
)
# ...unless it is a rate limit or a network hiccup (Drive answers 403 to rate limits).
_TRANSIENT_PATTERNS = re.compile(
    r"rate ?limit|too many requests|\b429\b|\b5\d\d\b|timeout|timed out|temporar|connection (?:reset|refused)|"
    r"no such host|network is unreachable|i/o timeout|EOF",
    re.IGNORECASE,
)

INSTALL_HINTS = {
    "Darwin": "brew install rclone   (or: sudo -v ; curl https://rclone.org/install.sh | sudo bash)",
    "Linux": "sudo apt install rclone, sudo dnf install rclone, or: sudo -v ; curl https://rclone.org/install.sh "
             "| sudo bash  (distribution packages can be old; the script installs the current release)",
    "Windows": "winget install Rclone.Rclone   (or: scoop install rclone, choco install rclone)",
}


def install_hint() -> str:
    hint = INSTALL_HINTS.get(platform.system(), "download it from https://rclone.org/downloads/")
    return f"rclone is not installed. Install it with: {hint}. More options: https://rclone.org/install/"


# --- validation ------------------------------------------------------------------------------
def validate_remote(value) -> str:
    """A remote name as listed by `rclone listremotes` (a trailing colon is accepted and dropped)."""
    if not isinstance(value, str):
        raise ValueError("remote must be text")
    name = value.strip()
    if name.endswith(":"):
        name = name[:-1]
    if not name:
        raise ValueError("remote is empty: give the name of an rclone remote (see `rclone listremotes`)")
    if name.startswith("-"):
        raise ValueError(f"remote must not start with '-': {value!r}")
    if not REMOTE_RE.match(name) or name.endswith(" "):
        raise ValueError(f"not a valid rclone remote name: {value!r} (letters, digits, space and _ - . + @)")
    return name


def validate_folder(value) -> str:
    """A folder inside the remote: no `..`, no option-looking value, no control or reserved characters.

    Returns it normalized: no duplicate or trailing slashes, `.` segments dropped. A leading `/` is kept
    (it means "from the root" on SFTP and similar backends)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("folder must be text")
    raw = value.strip()
    if raw.startswith("-"):
        raise ValueError(f"folder must not start with '-': {value!r}")
    if _BAD_PATH_CHARS.search(raw):
        raise ValueError(f"folder has characters rclone paths cannot use (\\ : * ? \" < > | or control): {value!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    for p in parts:
        if p == "..":
            raise ValueError(f"folder must not contain '..': {value!r}")
        if p.startswith("-"):
            raise ValueError(f"no folder segment may start with '-': {value!r}")
        if p != p.strip():
            raise ValueError(f"folder segments must not start or end with spaces: {value!r}")
    joined = "/".join(parts)
    return ("/" + joined) if raw.startswith("/") else joined


def validate_local_path(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} is empty")
    v = value.strip()
    if v.startswith("-") or "\x00" in v:
        raise ValueError(f"{what} must be a path, got {value!r}")
    return str(Path(v).expanduser())


def _time_from_id(meeting_id: str) -> datetime | None:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})", meeting_id or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
    except ValueError:
        return None


def meeting_folder_name(meeting_id: str, title: str | None, started_at: str | None) -> str:
    """`YYYY-MM-DD HHMM <title>` in local time; the id stands in for a missing title."""
    when = parse_time(started_at) or _time_from_id(meeting_id)
    stamp = when.astimezone().strftime("%Y-%m-%d %H%M") if when else ""
    label = safe_name(title, fallback="") if title and title != meeting_id else ""
    label = label or safe_name(meeting_id, fallback="")
    return f"{stamp} {label}".strip() or "meeting"


def sanitized_metadata(metadata: dict) -> dict:
    """Only the public keys, with anything that looks like a credential masked."""

    def clean(v):
        if isinstance(v, str):
            return redact(v)
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if not re.search(r"(?i)token|secret|password|key", str(k))}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    return {k: clean(metadata[k]) for k in METADATA_KEYS if k in metadata}


# --- running rclone --------------------------------------------------------------------------
Runner = Callable[..., subprocess.CompletedProcess]


class RcloneDestination(Destination):
    """Upload meetings with rclone: Google Drive, Dropbox, OneDrive, S3, SFTP, WebDAV and ~70 more.

    Uses the user's own rclone configuration; minuta stores no cloud credentials.
    """

    kind = "rclone"
    capabilities = Capabilities(audio=True, transcript=True, metadata=True, fetch_audio=True)
    secret_options = ("config_password",)

    # Seconds. Uploads of long calls on slow links need a generous ceiling; listing does not.
    DEFAULT_TIMEOUT = 3600
    LIST_TIMEOUT = 120

    def __init__(self, name: str, options: dict, secrets=None, runner: Runner = subprocess.run):
        super().__init__(name, options, secrets)
        self._run_fn = runner

    # --- options -------------------------------------------------------------------------
    @property
    def remote(self) -> str:
        return validate_remote(self.options.get("remote"))

    @property
    def folder(self) -> str:
        return validate_folder(self.options.get("folder") or "")

    @property
    def config_path(self) -> str | None:
        c = self.options.get("config")
        return validate_local_path(c, "config") if c else None

    @property
    def timeout(self) -> int:
        return int(self.number("timeout_seconds", self.DEFAULT_TIMEOUT, 1, 7 * 86400))

    def binary(self) -> str:
        b = self.options.get("binary")
        if b:
            path = validate_local_path(b, "binary")
            if not Path(path).is_file():
                raise PermanentError(f"rclone binary not found at {path}")
            return path
        found = shutil.which("rclone")
        if not found:
            raise PermanentError(install_hint())
        return found

    def _env(self) -> dict:
        env = dict(os.environ)
        password = self.secret("config_password")
        if password:
            env["RCLONE_CONFIG_PASS"] = password  # environment, never argv
        return env

    def _base(self) -> list[str]:
        args = [self.binary()]
        if self.config_path:
            args += ["--config", self.config_path]
        # Never wait for a password on a terminal nobody is looking at; bounded network waits.
        args += ["--ask-password=false", "--contimeout", "60s", "--timeout", "300s"]
        return args

    def rclone(self, args: list[str], paths: list[str] = (), timeout: int | None = None,
               check: bool = True) -> subprocess.CompletedProcess:
        """Run `rclone <args> -- <paths>` without a shell. Raises PermanentError / DestinationError."""
        argv = [*self._base(), *args]
        if paths:
            argv += ["--", *paths]
        timeout = timeout or self.timeout
        log.debug("rclone: %s", " ".join(argv[1:]))
        try:
            cp = self._run_fn(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=self._env(), shell=False)
        except FileNotFoundError as e:
            raise PermanentError(install_hint()) from e
        except PermissionError as e:
            raise PermanentError(f"cannot run rclone: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise DestinationError(f"rclone {args[0]} timed out after {timeout} s") from e
        cp.stdout, cp.stderr = cp.stdout or "", cp.stderr or ""
        if check and cp.returncode != 0:
            raise self._error(args[0], cp)
        return cp

    def _error(self, command: str, res: subprocess.CompletedProcess) -> DestinationError:
        err = redact(res.stderr.strip())
        # rclone also prints NOTICE lines (host keys, config hints): keep the ones that explain the failure.
        lines = [ln for ln in err.splitlines() if re.search(r"ERROR|CRITICAL|Failed|error", ln)] or err.splitlines()
        tail = "\n".join(lines)[-600:] if lines else f"exit code {res.returncode}"
        msg = f"rclone {command} failed (exit {res.returncode}): {tail}"
        if "didn't find section in config file" in err:
            return PermanentError(f"rclone has no remote named {self.remote!r}; run `rclone config` "
                                  f"or `rclone listremotes` to see the names. ({tail})")
        if res.returncode == EXIT_FATAL or (_PERMANENT_PATTERNS.search(tail) and not _TRANSIENT_PATTERNS.search(tail)):
            return PermanentError(msg + " -- check the remote with `rclone config reconnect "
                                  f"{self.remote}:` or `minuta destinations test {self.name}`")
        return DestinationError(msg)

    # --- queries ------------------------------------------------------------------------------
    def listremotes(self) -> list[str]:
        res = self.rclone(["listremotes"], timeout=self.LIST_TIMEOUT)
        return [line.strip().rstrip(":") for line in res.stdout.splitlines() if line.strip()]

    def check(self) -> CheckResult:
        """No writes: options, the binary, the remote in the config, and a listing of the folder."""
        try:
            remote, folder = self.remote, self.folder
            _ = self.config_path, self.timeout
            if self.config_path and not Path(self.config_path).is_file():
                return CheckResult(False, f"rclone config file not found: {self.config_path}")
            self.binary()
            remotes = self.listremotes()
            if remote not in remotes:
                listed = ", ".join(remotes) or "none"
                return CheckResult(False, f"rclone has no remote named {remote!r} (configured: {listed}). "
                                          f"Create it with `rclone config`.")
            target = self._join(remote, folder)
            res = self.rclone(["lsjson", "--max-depth", "1"], [target], timeout=self.LIST_TIMEOUT, check=False)
            if res.returncode == 0:
                return CheckResult(True, f"uploads to {target}")
            if res.returncode == EXIT_DIR_NOT_FOUND or "directory not found" in res.stderr.lower():
                root = f"{remote}:{'/' if folder.startswith('/') else ''}"
                self.rclone(["lsjson", "--max-depth", "1"], [root], timeout=self.LIST_TIMEOUT)
                return CheckResult(True, f"uploads to {target} (the folder is created on the first upload)")
            raise self._error("lsjson", res)
        except (ValueError, DestinationError) as e:
            return CheckResult(False, redact(str(e)))

    # --- sending ------------------------------------------------------------------------------
    def _place(self, remote: str, base: str, folder: str) -> tuple[str, str, str]:
        """Validate a (remote, base folder, meeting folder) triple coming from options or a receipt."""
        remote, base = validate_remote(remote), validate_folder(base)
        if not folder or "/" in folder or folder in (".", "..") or folder.startswith("-") \
                or _BAD_PATH_CHARS.search(folder):
            raise ValueError(f"invalid meeting folder name: {folder!r}")
        return remote, base, folder

    @staticmethod
    def _join(remote: str, *parts: str) -> str:
        path = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
        if parts and parts[0].startswith("/"):
            path = "/" + path
        return f"{remote}:{path}"

    def _target(self, meeting, metadata: dict | None, previous: Receipt | None) -> tuple[str, str, str]:
        """Where this meeting goes. A previous receipt wins (idempotency, even if the user later
        changed remote or folder); otherwise a new name that does not take another meeting's folder."""
        extra = previous.extra if previous else {}
        if extra.get("folder"):
            return self._place(extra.get("remote"), extra.get("base") or "", extra["folder"])
        meta = {**meeting.metadata(), **(metadata or {})}
        title = meta.get("title") or meta.get("code")
        name = meeting_folder_name(meeting.id, title, meta.get("started_at"))
        owner = self._folder_owner(self._join(self.remote, self.folder, name))
        if owner not in (None, meeting.id):
            name = f"{name} {safe_name(meeting.id)}"
        return self._place(self.remote, self.folder, name)

    def _folder_owner(self, path: str) -> str | None:
        """None if the folder is missing or empty, the meeting id in its meeting.json, or "?" when it
        holds something we cannot attribute."""
        res = self.rclone(["lsjson", "--files-only", "--max-depth", "1"], [path], timeout=self.LIST_TIMEOUT,
                          check=False)
        missing = res.returncode != 0 and "directory not found" in res.stderr.lower()
        if res.returncode == EXIT_DIR_NOT_FOUND or missing:
            return None
        if res.returncode != 0:
            raise self._error("lsjson", res)
        try:
            entries = json.loads(res.stdout or "[]")
        except json.JSONDecodeError as e:
            raise DestinationError("rclone lsjson returned something that is not JSON") from e
        if not entries:
            return None
        if not any(isinstance(e, dict) and e.get("Name") == "meeting.json" for e in entries):
            return "?"
        res = self.rclone(["cat"], [f"{path}/meeting.json"], timeout=self.LIST_TIMEOUT, check=False)
        try:
            return str(json.loads(res.stdout).get("id") or "?")
        except (json.JSONDecodeError, AttributeError):
            return "?"

    def _upload(self, meeting, place: tuple[str, str, str], files: dict[str, Path],
                metadata: dict | None = None) -> tuple[dict[str, int], str]:
        """Stage `files` ({remote name: local path}) and the sanitized meeting.json, copy each one to
        the meeting folder and verify them with `rclone check`. Returns (name -> size, "hash" | "size")."""
        target = self._join(*place)
        # Inside the meeting folder: same filesystem, so a hard link stands in for the recording.
        stage = Path(tempfile.mkdtemp(prefix=".rclone-", dir=meeting.dir))
        try:
            if metadata is not None:
                (stage / "meeting.json").write_text(
                    json.dumps(sanitized_metadata(metadata), ensure_ascii=False, indent=2), encoding="utf-8")
            for name, src in files.items():
                try:
                    os.link(src, stage / name)
                except OSError:
                    shutil.copyfile(src, stage / name)
            names = sorted(p.name for p in stage.iterdir())
            # meeting.json first: it is what tells a half-finished folder apart from another meeting's.
            for name in sorted(names, key=lambda n: n != "meeting.json"):
                self.rclone(["copyto"], [str(stage / name), f"{target}/{name}"])
            res = self.rclone(["check", "--one-way"], [str(stage), target], check=False)
            if res.returncode != 0:
                raise DestinationError("uploaded files do not match the local ones: "
                                       + redact(res.stderr.strip())[-400:])
            # rclone says so when both sides share no hash and it could only compare sizes.
            verified = "size" if "no common hash" in res.stderr.lower() else "hash"
            return {n: (stage / n).stat().st_size for n in names}, verified
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _receipt(self, place: tuple[str, str, str], sizes: dict[str, int], verified: str,
                 previous: Receipt | None, audio_name: str | None) -> Receipt:
        prev = dict(previous.extra) if previous else {}
        remote, base, folder = place
        audio_name = audio_name or prev.get("audio_name")
        extra = {"remote": remote, "base": base, "folder": folder,
                 "files": {**(prev.get("files") or {}), **sizes}, "verified": verified}
        if audio_name:
            extra["audio_name"] = audio_name
        return Receipt(remote_id=self._join(*place), url=None,
                       audio=bool(audio_name) or bool(previous and previous.audio), extra=extra)

    def send_meeting(self, meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        try:
            place = self._target(meeting, parts.metadata, previous)
        except ValueError as e:
            raise PermanentError(str(e)) from e
        files: dict[str, Path] = {}
        audio_name = None
        if parts.audio is not None:
            if parts.audio.name not in AUDIO_NAMES:
                raise PermanentError(f"unexpected audio file name: {parts.audio.name}")
            audio_name = parts.audio.name
            files[audio_name] = parts.audio
        for path in (parts.transcript or {}).values():
            if path.name in TRANSCRIPT_FILES:
                files[path.name] = path
        if not files and parts.metadata is None:
            raise PermanentError("nothing to send")
        sizes, verified = self._upload(meeting, place, files, parts.metadata)
        return self._receipt(place, sizes, verified, previous, audio_name)

    def fetch_audio(self, meeting, receipt: Receipt, dest: Path) -> Path:
        extra = receipt.extra or {}
        name = extra.get("audio_name")
        if name not in AUDIO_NAMES:
            raise PermanentError("the receipt does not say which audio file was uploaded")
        try:
            place = self._place(extra.get("remote"), extra.get("base") or "", extra.get("folder") or "")
        except ValueError as e:
            raise PermanentError(f"the receipt points to an invalid place: {e}") from e
        target = dest.with_suffix(Path(name).suffix) if dest.suffix == "" else dest
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.partial")
        try:
            self.rclone(["copyto"], [f"{self._join(*place)}/{name}", str(partial)])
            if not partial.exists():
                raise DestinationError(f"rclone did not write {partial.name}")
            expected = (extra.get("files") or {}).get(name)
            if expected is not None and partial.stat().st_size != int(expected):
                raise DestinationError(f"downloaded audio has {partial.stat().st_size} bytes, expected {expected}")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    # --- interactive setup ---------------------------------------------------------------------
    @classmethod
    def configure(cls, name: str, options: dict, prompter=None, secrets=None,
                  runner: Runner = subprocess.run) -> dict | None:
        from minuta.destinations.rclone_setup import configure

        return configure(cls, name, options, prompter, secrets, runner)

