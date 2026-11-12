# Destinations

A destination receives finished meetings and, later, their transcripts. minuta ships six:

| Type | What it does | Receives |
|---|---|---|
| `local` | Keeps meetings in the data folder; optionally copies them to another folder | audio, transcript, metadata |
| `webhook` | POSTs signed JSON to a URL (n8n, Zapier, Make, your own server) | metadata, transcript, audio (optional) |
| `command` | Runs a program of yours for each meeting and transcript | metadata, transcript (the folder path) |
| `obsidian` | Writes one Markdown note per meeting, with frontmatter, in a vault | metadata, transcript |
| `rclone` | Uploads each meeting to Google Drive, Dropbox, OneDrive, S3, SFTP, WebDAV and about 70 more services through your own rclone | audio, transcript, metadata |
| `notion` | Creates one page per meeting in a Notion database, with the audio attached and the transcript inside | audio, transcript, metadata |

## Adding one

```bash
minuta destinations list
minuta destinations add NAME --type TYPE                  # in a terminal: asks for what is missing
minuta destinations add NAME --type TYPE --option key=value ... [--send metadata,transcript] [--non-interactive]
minuta destinations test NAME [--live]
minuta destinations remove NAME        # also deletes its secrets
minuta retry                           # sends existing meetings to a destination added later
```

`--type` defaults to `NAME`, so `minuta destinations add notion` is enough. In a terminal, `add`
asks only for the options not given with `--option`; with `--non-interactive` (or without a
terminal) it asks nothing and uses the options as given. Either way it then runs the destination's
check and refuses to save one that fails (`--force` saves it anyway). `--send` defaults to the parts
listed in the table above. `NAME` is up to 32 lowercase letters, digits, `-` or `_`, starting with a
letter.

The background service notices the change to `config.toml` and uses it from the next meeting on;
no restart is needed.

`minuta setup` offers to add one more destination of any type and asks the same questions. With
flags, it adds or changes any number of them: `--destination TYPE[:NAME]` followed by that
destination's `--option key=value` (`send=metadata,transcript` included), the same options as
`destinations add`:

```bash
minuta setup --non-interactive \
  --destination obsidian --option vault=~/Documents/Vault \
  --destination webhook:n8n --option url=http://127.0.0.1:5678/webhook/minuta --option signing_key@env=KEY
```

Options marked *secret* below are moved to the keyring (or the 0600 `.env` fallback) when the
destination is added; `config.toml` only keeps their name.

## local

The `local` destination is created by `minuta setup`: meetings stay in the data folder. Add
`copy_to` to also copy each meeting, raw files and audio, to `<copy_to>/<date> <time> <title>/`
(local time, like `2026-11-12 16-50 Weekly sync`; the Meet code when the call has no title), for
example a folder synced by Drive, Dropbox or iCloud:

```bash
minuta destinations add backup --type local --option copy_to=~/Dropbox/minuta
```

| Option | Default | |
|---|---|---|
| `copy_to` | none | Folder to copy meetings to. Without it nothing is copied. |
| `private` | `true` | The folders and files minuta writes there are only for your user (0700 and 0600 on macOS and Linux), like the originals. `false` keeps the system's default permissions. `copy_to` itself is never changed. |

The meeting's id stays in its `meeting.json`. Two meetings with the same name get `(2)`, `(3)`.
Meetings copied before this naming keep their folder.

A local destination with `copy_to` holds the audio, so `delete_local_audio_after_upload` may
delete the recorded copy; `minuta transcribe` then reads it back from `copy_to`.

## webhook

```bash
minuta destinations add hook --type webhook \
  --option url=https://example.com/minuta \
  --option signing_key="$(openssl rand -hex 32)" \
  --option "headers=Authorization: Bearer <token>"     # optional
minuta destinations test hook --live                    # sends a signed "ping" event
```

| Option | Default | |
|---|---|---|
| `url` | required | `https://` only. Plain `http://` is accepted only for `127.0.0.1`, `localhost` and `::1` (a local n8n). No user or password in the URL. |
| `signing_key` | required, *secret* | At least 16 characters. Your receiver uses the same value to check the signature. |
| `headers` | none, *secret* | Extra headers: `Name: value` (one per line) or a JSON object. Cannot override minuta's own headers. |
| `send` | `metadata,transcript` | Add `audio` to attach the recording. |
| `max_audio_mb` | `100` | Audio larger than this is not attached; the event is still sent, with `"audio": {"included": false, "reason": "too_large"}`. |
| `timeout_seconds` | `30` | Per request. A timeout, a connection error, 408, 429 or 5xx is retried by the router, which honours `Retry-After` up to a minute (a longer wait is left to `minuta retry`). |

What it sends, per meeting:

- `meeting.ready` when the recording is ready. It includes the transcript if one already exists.
- `transcript.ready` when a transcript is made later (`minuta transcribe`, or automatic mode).

The body is JSON (`Content-Type: application/json`), or `multipart/form-data` with a `payload` part
(that same JSON) and an `audio` part when the audio is attached:

```json
{
  "event": "transcript.ready",
  "delivery_id": "2026-02-02T09-00-00-000Z_abc-defg-hij.transcript.ready",
  "request_id": "5f0c2a9e41d7b3c86a1e0f27",
  "destination": "hook",
  "minuta_version": "0.1.0",
  "meeting_id": "2026-02-02T09-00-00-000Z_abc-defg-hij",
  "meeting": {"id": "...", "title": "...", "started_at": "...", "duration_seconds": 1830, "url": "..."},
  "transcript": {"title": "...", "turns": [{"start": 0.0, "end": 4.2, "speaker": "Me", "text": "..."}]},
  "transcript_markdown": "# ...",
  "audio": {"included": true, "file": "audio.opus", "bytes": 1048576, "sha256": "..."}
}
```

Headers:

| Header | |
|---|---|
| `X-Minuta-Event` | `meeting.ready`, `transcript.ready` or `ping` |
| `X-Minuta-Timestamp` | Unix seconds when this attempt was signed |
| `X-Minuta-Signature` | `sha256=` + hex HMAC-SHA256 of `<timestamp>.<raw body>` with the signing key |
| `Idempotency-Key` | `<meeting id>.<event>`: the same on every attempt and every `minuta retry` (also `delivery_id` in the body) |

`request_id` in the body is new on every request, so no two requests share a signature: a receiver
may refuse a signature it has already seen.

A 2xx response is success. If it is JSON with `id` or `url`, those are kept in the receipt. Any
other 4xx is a permanent failure (fix the URL or the receiver, then `minuta retry`). Redirects
are followed only when they are 307/308 to the same scheme, host and port; anything else fails.
TLS certificates are always verified (a private CA can be added with `REQUESTS_CA_BUNDLE`). The
webhook never counts as holding the audio, so it never allows deleting the local copy.

### Verifying the signature

Check the signature over the **raw** body, before parsing it, reject old timestamps, and use
`Idempotency-Key` to ignore deliveries you already processed: minuta retries with a fresh
timestamp and signature but the same key.

Python:

```python
import hashlib
import hmac
import time


def verify(secret: bytes, headers, body: bytes, tolerance: int = 300) -> bool:
    """headers: any mapping with case-insensitive get (Flask, FastAPI, http.server)."""
    ts = headers.get("X-Minuta-Timestamp") or ""
    sig = headers.get("X-Minuta-Signature") or ""
    if not ts.isdigit() or abs(time.time() - int(ts)) > tolerance:
        return False  # too old or from the future: possibly a replay
    expected = "sha256=" + hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
```

Node.js (with Express, read the body with `express.raw({ type: "*/*" })`):

```js
const crypto = require("crypto");

function verify(secret, headers, rawBody, toleranceSeconds = 300) {
  const ts = headers["x-minuta-timestamp"] || "";
  const sig = headers["x-minuta-signature"] || "";
  if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > toleranceSeconds) return false;
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(ts + ".").update(rawBody).digest("hex");
  return sig.length === expected.length && crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
}

module.exports = { verify };
```

`tests/test_webhook.py` runs both snippets against real requests.

## command

```bash
minuta destinations add notify --type command --option "command=python3 /home/me/on_meeting.py --dir {meeting_dir}"
```

| Option | Default | |
|---|---|---|
| `command` | required | A TOML array, or a command line split like a shell would but **never run by a shell**: `;`, `$()`, `|` and quotes are not interpreted. |
| `timeout_seconds` | `300` | The program and everything it started are stopped after this. |
| `pass_env` | none | Names of extra environment variables to pass through. |

A non-zero exit is retried like any failed delivery. The last 4000 bytes of stdout and stderr go to
the log, with secrets masked.

For each event the program gets:

- Arguments: `{meeting_dir}`, `{meeting_id}` and `{event}` inside any argument are replaced. If no
  argument uses them, the event and the folder are appended: `program ... meeting.ready /path/to/meeting`.
- Environment: `MINUTA_EVENT`, `MINUTA_MEETING_ID`, `MINUTA_MEETING_DIR`, `MINUTA_DESTINATION`, plus
  a short allowlist (`PATH`, `HOME`, locale and temp folders; on Windows also `SYSTEMROOT`, `APPDATA`
  and the like) and `pass_env`. Nothing else from minuta's environment is passed, and a variable whose
  value is one of minuta's secrets is never passed, not even through `pass_env`.
- Working folder: the meeting folder, with `meeting.json`, the audio and `transcript.*`.

stdout and stderr are logged (truncated, secrets masked). Exit 0 is success. The program may run
again for the same meeting (`minuta retry`), so make it idempotent. On Windows, `.bat` and `.cmd`
files are refused because `cmd.exe` would re-interpret the arguments; run `python script.py`,
`powershell -File script.ps1` or an `.exe`.

## obsidian

```bash
minuta destinations add notes --type obsidian --option vault=~/Documents/Vault --option folder=Meetings
```

| Option | Default | |
|---|---|---|
| `vault` | required | The vault folder. |
| `folder` | `Meetings` | Inside the vault; it cannot point outside. |
| `tags` | `meeting` | Comma-separated, or a TOML list. |
| `private` | `true` | Notes (and the folder, when minuta creates it) only for your user: 0600 and 0700 on macOS and Linux. `false` keeps the system's default. |

Each meeting becomes `<folder>/<date> <title>.md` with frontmatter (`title`, `date`,
`duration_minutes`, `meet`, `minuta_id`, `tags`) and the transcript between
`<!-- minuta:transcript:start -->` and `<!-- minuta:transcript:end -->`. Later deliveries only rewrite
that block, so your own notes in the file and changes to the frontmatter are kept. Unlike
`local` with `copy_to`, it writes one readable note instead of copying the raw files and the audio.

## rclone

[rclone](https://rclone.org) talks to the storage service; minuta runs it. You sign in once with
rclone's own commands, and that sign-in lives in rclone's config file (`rclone config file` prints
where). minuta stores no cloud credentials and never asks for them.

Each meeting becomes a folder:

```
<remote>:<folder>/2026-03-02 1030 Weekly sync/
    audio.opus          the recording
    meeting.json        id, title, Meet code and link, start, end, duration, audio check
    transcript.md       } once there is a transcript; replaced in place
    transcript.json     } if you transcribe again
    transcript.vtt      }
```

The time is local time; a meeting without a title uses its id. If another meeting already has that
folder, the meeting id is appended. Every upload is compared with the local files through `rclone
check` (hashes when the service and your computer share one, sizes otherwise) before minuta counts
it as delivered, so `delete_local_audio_after_upload` only deletes audio that is really there.

### 1. Install rclone

| System | Command |
|---|---|
| macOS | `brew install rclone` |
| Linux | `sudo apt install rclone` or `sudo dnf install rclone` (distributions can lag; `sudo -v ; curl https://rclone.org/install.sh \| sudo bash` installs the current release) |
| Windows | `winget install Rclone.Rclone` (or `scoop install rclone`, `choco install rclone`) |

minuta does not install it for you. `rclone version` should answer.

### 2. Connect the service and add the destination

The quickest way is the assistant, which lists your rclone remotes, can create a Google Drive one
and asks for the folder:

```bash
minuta destinations add drive --type rclone
```

Or step by step:

**Google Drive**

```bash
rclone config create gdrive drive scope=drive.file
minuta destinations add drive --type rclone --option remote=gdrive --option folder=minuta
minuta destinations test drive
```

`rclone config create` opens the browser to sign in to Google. `scope=drive.file` lets rclone see
only the files it creates, not the rest of your Drive; leave it out (or use `scope=drive`) to upload
into a folder you made by hand. rclone's shared Google app is rate limited; for heavy use, [make
your own client id](https://rclone.org/drive/#making-your-own-client-id) and add
`client_id=... client_secret=...` to the `config create` line.

**Dropbox**

```bash
rclone config create dropbox dropbox
minuta destinations add dropbox --type rclone --option remote=dropbox --option folder=minuta
minuta destinations test dropbox
```

The browser opens to approve rclone's access to your Dropbox.

**OneDrive**

```bash
rclone config
#   n (new remote) -> name: onedrive -> storage: onedrive -> leave client_id/secret empty
#   -> region: global -> edit advanced config: n -> use web browser: y (sign in)
#   -> choose "OneDrive Personal or Business" and the drive -> y to keep -> q
rclone lsd onedrive:
minuta destinations add onedrive --type rclone --option remote=onedrive --option folder=minuta
minuta destinations test onedrive
```

**S3, SFTP, WebDAV, Box, pCloud, Nextcloud...**: `rclone config`, then the same `minuta destinations
add` line with your remote's name. For S3 the folder starts with the bucket
(`--option folder=my-bucket/minuta`).

**A computer without a browser** (a server): in `rclone config`, answer *n* to "Use web browser to
automatically authenticate?"; rclone tells you to run `rclone authorize "drive"` (or `dropbox`,
`onedrive`) on a computer that has one and to paste the result back.

### Options

| Option | Default | Meaning |
|---|---|---|
| `remote` | (required) | the remote's name as `rclone listremotes` prints it, colon optional |
| `folder` | the remote's root | folder inside the remote; created on the first upload |
| `config` | rclone's default | another rclone config file |
| `binary` | `rclone` on PATH | full path to the rclone program |
| `timeout_seconds` | 3600 | longest a single upload may take before it is retried |
| `config_password` | - | password of an encrypted rclone config (`rclone config` -> *s*); stored with the keyring, never in `config.toml` |

`minuta destinations test NAME` lists the folder without writing anything. Errors that need you
(signed out, wrong password, unknown remote) are reported as permanent with the rclone command that
fixes them, usually `rclone config reconnect <remote>:`; network errors are retried.

### Safety

- rclone always runs with an argument list, never through a shell. The remote and folder are checked
  first: no `..`, nothing starting with `-`, none of ``\ : * ? " < > | ; $ ` &`` and no control
  characters; paths go after `--`, so they can never be read as rclone options.
- Nothing secret is put on rclone's command line or in minuta's logs. A config password reaches
  rclone through the `RCLONE_CONFIG_PASS` environment variable, and rclone is told never to ask for
  one (`--ask-password=false`), so a locked config fails fast instead of hanging.
- `meeting.json` on the remote carries only the public fields listed above; minuta's delivery state
  and anything that looks like a credential stay local.
- minuta never creates share links. Who can see the files is whatever the service and folder allow.

## notion

Each meeting becomes a page in a Notion database: title, date, duration and Meet link as
properties, the audio attached, and the transcript inside a "Transcript" toggle once it exists.

1. Create an internal integration at https://www.notion.so/profile/integrations with read, update
   and insert content, and copy its secret.
2. In Notion, open the database (or the page that should hold a new one), menu `...` >
   Connections, and add the integration.
3. `minuta destinations add notion` asks for the secret and for the database link, or creates the
   database inside the page. Without questions:

```bash
minuta destinations add notion --non-interactive --option token@env=NOTION_TOKEN --option database=<link>
minuta destinations add notion --non-interactive --option token@env=NOTION_TOKEN --option parent_page=<link>
```

| Option | Default | |
|---|---|---|
| `token` | required, *secret* | The integration's secret. |
| `database` | required | Database id or link. |
| `parent_page` | none | Only when adding: create a new database inside this page (id or link) and save its id as `database`. |
| `date_property`, `duration_property`, `url_property`, `status_property` | matched by type | Pin a property when the database has more than one of that type. |

Properties are matched by type, not by name: the title, a date, a number (minutes), a URL and a
checkbox (ticked once transcribed); a select property works as status when pinned. Free
workspaces take files up to 5 MB, so long meetings need a paid plan or `--send metadata,transcript`.

Notion holds the audio, so with `[storage] delete_local_audio_after_upload = true` the local audio
is deleted once Notion confirms it has it, and `minuta transcribe` downloads it again. The `local`
destination must then not list `audio` in its `send` (unless it has `copy_to`). The setup assistant
offers both after adding Notion or rclone.

## How meetings flow

1. The call ends. The server joins the audio, checks it, and marks the meeting `ready`.
2. The router calls `send_meeting()` on every enabled destination.
3. When a transcript exists (automatic mode, or `minuta transcribe`), the router calls
   `send_transcript()` on every enabled destination that takes transcripts.
4. If `[storage] delete_local_audio_after_upload = true` and every enabled destination that
   receives audio returned a receipt with `audio = true`, the local audio is deleted.

The outcome of each step is stored in `meeting.json`:

```json
"destinations": {
  "local": {
    "meeting":    {"status": "ok", "attempts": 1, "receipt": {"remote_id": "...", "url": "...", "audio": true, "extra": {}}},
    "transcript": {"status": "failed", "attempts": 3, "error": "DestinationError: timeout", "permanent": false}
  }
}
```

A step already `ok` is never sent again. `minuta retry` sends again every step that failed, and
anything for a destination enabled after the meeting was delivered.

## Writing your own

Adding a destination is one file plus a test.

### The interface

```python
from minuta.destinations import (
    Capabilities, CheckResult, Destination, DestinationError, MeetingParts, PermanentError, Receipt,
)

class Destination(ABC):
    kind: ClassVar[str]                      # type name used in config.toml
    capabilities: ClassVar[Capabilities]     # audio, transcript, metadata, fetch_audio
    secret_options: ClassVar[tuple[str, ...]] = ()

    def __init__(self, name: str, options: dict, secrets: SecretStore | None = None): ...
    @classmethod
    def configure(cls, name: str, options: dict, prompter: Prompter | None = None,
                  secrets: SecretStore | None = None) -> dict | None: ...
    def check(self) -> CheckResult: ...
    def send_meeting(self, meeting: Meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt: ...
    def send_transcript(self, meeting: Meeting, transcript: dict[str, Path], previous: Receipt | None = None) -> Receipt: ...
    # default: send_meeting() with MeetingParts(transcript=..., event="transcript.ready")
    def fetch_audio(self, meeting: Meeting, receipt: Receipt, dest: Path) -> Path: ...
```

- `options` is the destination's table in `config.toml` (`type`, `enabled`, `send` and your own keys).
- `parts` is already filtered by the user's `send` option and your capabilities: a part you should
  not receive is `None`. `parts.transcript` is `{"json": Path, "md": Path, "vtt": Path}`.
- `meeting.id`, `meeting.title`, `meeting.dir`, `meeting.metadata()` (title, code, link, times,
  duration, audio check) and `meeting.read()` (the whole `meeting.json`) are available.
- `minuta.destinations.base` has helpers: `self.secret(option)`, `self.number(option, default, low,
  high)`, `safe_name()`, `file_sha256()`, `write_atomic()`, `copy_atomic()` and
  `retry_after_seconds()`. Do not write to `meeting.json`: return what you need to remember in the receipt.

#### Rules

- **`check()` has no side effects.** Validate options and that credentials exist. Adding a
  destination and `minuta destinations test` call it.
- **`configure()` runs once, when the destination is added.** `minuta destinations add` and
  `minuta setup` both call it, through `minuta.setup.add_destination()`, before `check()`. It gets
  the options given so far (secrets still in plain text) and asks for what is missing through
  `prompter` (`text`, `password`, `path`, `select`, `confirm`). `prompter` is `None` when nobody can
  answer: then it must not ask, and `check()` reports what is missing. Unlike `check()` it may
  create things on the remote side, such as the Notion database. It returns the options to save
  (secret options still in them are moved to the keyring afterwards), returns `None` to cancel, or
  raises `DestinationError` or `ValueError` with a message. The default returns the options
  unchanged.
- **Sends are idempotent.** `previous` is the receipt of an earlier successful step for this meeting
  (for `send_transcript`, the transcript receipt or else the meeting receipt). Use
  `previous.remote_id` to update the same remote object instead of creating another one.
- **Errors.** Raise `DestinationError` (or let any exception through) for failures worth retrying,
  and do not retry inside the adapter: the router retries up to `[delivery] max_attempts` with
  backoff and records the error. When the remote side says how long to wait, pass it:
  `DestinationError(message, retry_after=seconds)`. Raise `PermanentError` when retrying cannot
  help, such as a rejected token.
- **`Receipt.audio = True` only when the remote side really holds the audio.** It is what allows
  deleting the local copy. If you declare `fetch_audio`, implement `fetch_audio()` so a meeting whose
  local audio was deleted can still be transcribed.
- **Secrets never go in `config.toml`.** List option names in `secret_options`; adding the
  destination stores the value with the keyring (or the 0600 `.env` fallback) and
  writes `token_secret = "<NAME>_TOKEN"` instead. Read it back with `self.secret("token")`. Values
  read this way are masked in logs.

### Registering it

Every destination is an entry point in the `minuta.destinations` group. Inside minuta, add it to
`[project.entry-points."minuta.destinations"]` in `pyproject.toml`. From a separate package,
declare the same in its own `pyproject.toml`; minuta finds it at startup (a plugin cannot take the
name of a built-in type):

```toml
# pyproject.toml of your package
[project.entry-points."minuta.destinations"]
mydest = "minuta_mydest:MyDestination"
```

Install it into minuta's environment with `uv tool install --with minuta-mydest ...` (or
`uv tool install --with-editable ./minuta-mydest ...` while developing).

### A complete example

The built-in adapters are short and cover the whole interface:
[`webhook.py`](../src/minuta/destinations/webhook.py) (secrets, questions in `configure()`,
permanent vs retryable errors, idempotent sends), [`obsidian.py`](../src/minuta/destinations/obsidian.py) (updating the same remote
object through the previous receipt) and [`local.py`](../src/minuta/destinations/local.py)
(audio and `fetch_audio`). Their tests, `tests/test_webhook.py` and `tests/test_obsidian.py`, are
the shape a test for a new adapter should take: no network beyond a local endpoint, and a check
that a second send with the previous receipt does not create a duplicate.
