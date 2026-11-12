"""The built-in `notion` destination: one page per meeting in a Notion database.

- Title, start date, duration (minutes), Meet link and a "transcribed" mark go to the database
  properties of the matching TYPE (title, date, number, url, checkbox or select), so any database
  works whatever its properties are called. When a type appears more than once, pin the property
  with an option (`date_property`, `duration_property`, `url_property`, `status_property`).
- The audio goes through Notion's File Upload API (in parts above 20 MB) into an audio block.
- The transcript goes inside a toggle heading, "Transcript", which is replaced, never duplicated,
  when it is sent again.
- Sends are idempotent: a previous receipt updates the same page, and a page whose creation
  answer was lost is found again (same title, same start, created by this integration). That is
  what makes the router's retries safe; nothing is retried in here.

    [destinations.notion]
    type = "notion"
    token_secret = "NOTION_TOKEN"   # name of the secret; the token itself is in the keyring
    database = "<database id or link>"
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from minuta.destinations.base import (
    PARTS,
    Capabilities,
    CheckResult,
    Destination,
    DestinationError,
    MeetingParts,
    PermanentError,
    Receipt,
    retry_after_seconds,
)
from minuta.meetings import parse_time
from minuta.redact import redact, register_secret
from minuta.transcription.export import mmss

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
SINGLE_PART_MAX = 20 * 1024 * 1024  # larger files go in parts
PART_SIZE = 10 * 1024 * 1024  # Notion wants parts of 5 to 20 MB, except the last one
DEFAULT_UPLOAD_LIMIT = 5 * 1024**3  # paid workspaces; the API tells the real one
TEXT_MAX = 2000  # characters (UTF-16 units) per rich text item
BLOCKS_PER_REQUEST = 100
BODY_MAX = 400_000  # bytes; Notion rejects request bodies over 500 KB
TIMEOUT = 60
TRANSFER_TIMEOUT = 300
TRANSCRIPT_HEADING = "Transcript"
DATABASE_TITLE = "Meetings"  # a database created by `configure`
RECORDED, TRANSCRIBED = "Recorded", "Transcribed"

# role -> property types that can hold it, in order of preference
ROLES: dict[str, tuple[str, ...]] = {
    "date": ("date",),
    "duration": ("number",),
    "url": ("url",),
    "status": ("checkbox", "select"),
}

_ID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE)


def parse_id(text: str | None) -> str | None:
    """A Notion id, bare or dashed, or a link to a page or database. Returns the dashed id.
    In a link, the id is the last one in the path: `?v=` holds a view id, not the database."""
    found = _ID.findall(urlparse((text or "").strip()).path)
    if not found:
        return None
    raw = found[-1].replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def new_session() -> requests.Session:
    """Every HTTP call goes through a session from here, so tests can mount a fake Notion."""
    return requests.Session()


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


# --- the HTTP client -------------------------------------------------------------------
class NotFound(PermanentError):
    """Notion answered 404: the object does not exist or is not shared with the integration."""


class NotionAPI:
    def __init__(self, token: str):
        if not token:
            raise PermanentError("no Notion token: add the destination again with the integration secret")
        register_secret(token)
        self._token = token
        self.http = new_session()
        self._me: dict | None = None

    def _headers(self, json_body: bool) -> dict:
        h = {"Authorization": f"Bearer {self._token}", "Notion-Version": NOTION_VERSION}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method: str, path: str, body: dict | None = None, *, files=None, data=None,
                timeout: float = TIMEOUT) -> dict:
        """One API call; a failure raises (429 with Notion's Retry-After for the router)."""
        url = API + path
        try:
            if files is not None:
                r = self.http.request(method, url, headers=self._headers(False), files=files, data=data,
                                      timeout=timeout)
            else:
                payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                r = self.http.request(method, url, headers=self._headers(payload is not None), data=payload,
                                      timeout=timeout)
        except requests.RequestException as e:
            raise DestinationError(redact(f"{method} {path}: {type(e).__name__}: {e}")) from None
        if r.status_code >= 400:
            raise self._error(method, path, r)
        return r.json() if r.content else {}

    @staticmethod
    def _error(method: str, path: str, r: requests.Response) -> DestinationError:
        try:
            info = r.json()
        except ValueError:
            info = {}
        code = info.get("code") or ""
        message = redact(str(info.get("message") or r.text[:200]))
        where = f"{method} {path} -> {r.status_code} {code}".strip()
        if r.status_code == 401:
            return PermanentError(f"{where}: Notion rejected the token. Copy the integration secret again from "
                                  "https://www.notion.so/profile/integrations and add the destination again.")
        if r.status_code == 403:
            return PermanentError(f"{where}: {message}. In the integration settings, allow it to read, "
                                  "update and insert content.")
        if r.status_code == 404:
            return NotFound(f"{where}: {message}. Open the database (or page) in Notion, menu ... > "
                            "Connections, and add the integration.")
        if r.status_code == 429 or r.status_code >= 500 or r.status_code == 409:
            wait = retry_after_seconds(r.headers.get("Retry-After"))
            return DestinationError(f"{where}: {message}", retry_after=wait)
        return PermanentError(f"{where}: {message}")

    def me(self) -> dict:
        if self._me is None:
            self._me = self.request("GET", "/users/me")
        return self._me

    def upload_limit(self) -> int:
        limits = (self.me().get("bot") or {}).get("workspace_limits") or {}
        return int(limits.get("max_file_upload_size_in_bytes") or DEFAULT_UPLOAD_LIMIT)

    def children(self, block_id: str) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            q = "?page_size=100" + (f"&start_cursor={quote(cursor)}" if cursor else "")
            r = self.request("GET", f"/blocks/{block_id}/children{q}")
            out += r.get("results") or []
            if not r.get("has_more") or not r.get("next_cursor"):
                return out
            cursor = r["next_cursor"]

    def append(self, block_id: str, blocks: list[dict]) -> list[dict]:
        """Append in requests of at most 100 blocks and BODY_MAX bytes. A half-written transcript is
        deleted and written again by the next attempt."""
        created: list[dict] = []
        for batch in batches(blocks):
            r = self.request("PATCH", f"/blocks/{block_id}/children", {"children": batch})
            created += r.get("results") or []
        return created


def batches(blocks: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for b in blocks:
        n = len(json.dumps(b, ensure_ascii=False).encode("utf-8")) + 1
        if cur and (len(cur) >= BLOCKS_PER_REQUEST or size + n > BODY_MAX):
            out.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += n
    if cur:
        out.append(cur)
    return out


# --- rich text and blocks ----------------------------------------------------------------
def _units(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def split_text(text: str, limit: int = TEXT_MAX) -> list[str]:
    """Pieces of at most `limit` UTF-16 units (how Notion counts), cut at a space when one is near."""
    out = []
    while sum(_units(c) for c in text) > limit:
        units = n = 0
        for ch in text:
            if units + _units(ch) > limit:
                break
            units += _units(ch)
            n += 1
        cut = text.rfind(" ", n // 2, n)
        if cut > 0:
            out.append(text[:cut])
            text = text[cut + 1:]
        else:
            out.append(text[:n])
            text = text[n:]
    out.append(text)
    return out


def _text(content: str, **annotations) -> dict:
    item: dict = {"type": "text", "text": {"content": content}}
    if annotations:
        item["annotations"] = annotations
    return item


def _paragraph(rich: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def transcript_blocks(doc: dict) -> list[dict]:
    turns = doc.get("turns") or []
    if not turns:
        return [_paragraph([_text("No speech was detected in the recording.")])]
    blocks = []
    for t in turns:
        pieces = split_text(str(t.get("text") or ""))
        head = [_text(f"[{mmss(t.get('start') or 0)}] ", color="gray"),
                _text(f"{split_text(str(t.get('speaker') or ''), 200)[0]}: ", bold=True)]
        blocks.append(_paragraph(head + ([_text(pieces[0])] if pieces[0] else [])))
        blocks += [_paragraph([_text(p)]) for p in pieces[1:]]
    return blocks


def _transcript_doc(files: dict[str, Path]) -> dict:
    if files.get("json") and Path(files["json"]).exists():
        return json.loads(Path(files["json"]).read_text(encoding="utf-8"))
    if files.get("md") and Path(files["md"]).exists():  # no JSON: the Markdown, one paragraph per line
        lines = [ln for ln in Path(files["md"]).read_text(encoding="utf-8").splitlines() if ln.strip()]
        return {"lines": lines}
    raise DestinationError("the transcript has no json or md file")


def _blocks_for(doc: dict) -> list[dict]:
    if "lines" in doc:
        return [_paragraph([_text(p)]) for ln in doc["lines"] for p in split_text(ln)]
    return transcript_blocks(doc)


def _plain(rich: list | None) -> str:
    return "".join(r.get("plain_text") or (r.get("text") or {}).get("content", "") for r in rich or [])


def _is_transcript_heading(block: dict) -> bool:
    if block.get("type") != "heading_2":
        return False
    h = block.get("heading_2") or {}
    return bool(h.get("is_toggleable")) and _plain(h.get("rich_text")) == TRANSCRIPT_HEADING


def _audio_url(block: dict) -> str | None:
    if block.get("type") != "audio":
        return None
    a = block.get("audio") or {}
    return (a.get("file") or {}).get("url") if a.get("type") == "file" else None


def _same_minute(a: datetime | None, b: datetime | None) -> bool:
    """Notion may keep a date to the minute only."""
    if a is None or b is None:
        return False
    return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)


# --- the database schema ------------------------------------------------------------------
@dataclass
class Schema:
    title_text: str
    title: str
    roles: dict[str, str] = field(default_factory=dict)  # role -> property name
    types: dict[str, str] = field(default_factory=dict)  # property name -> type
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_database(cls, db: dict, options: dict) -> Schema:
        props = db.get("properties") or {}
        types = {name: (p or {}).get("type") for name, p in props.items()}
        titles = [n for n, ty in types.items() if ty == "title"]
        if not titles:
            raise PermanentError("the Notion database has no title property")
        s = cls(title_text=_plain(db.get("title")) or "untitled", title=titles[0], types=types)
        for role, kinds in ROLES.items():
            pinned = (options.get(f"{role}_property") or "").strip()
            if pinned:
                if types.get(pinned) in kinds:
                    s.roles[role] = pinned
                else:
                    s.notes.append(f"{role}_property {pinned!r} is not a {' or '.join(kinds)} property")
                continue
            for kind in kinds:
                found = sorted(n for n, ty in types.items() if ty == kind)
                if len(found) > 1:
                    s.notes.append(f"several {kind} properties ({', '.join(found)}): set {role}_property to use one")
                    break
                if found:
                    name = found[0]
                    if kind == "select" and not _has_status_options(props[name]):
                        break  # a select of the user's own (a category, a team): never written unless pinned
                    s.roles[role] = name
                    break
        return s

    def describe(self) -> str:
        parts = [f"title={self.title}"] + [f"{r}={n}" for r, n in self.roles.items()]
        return ", ".join(parts)


def _has_status_options(prop: dict) -> bool:
    names = {(o.get("name") or "").casefold() for o in ((prop.get("select") or {}).get("options") or [])}
    return bool(names & {RECORDED.casefold(), TRANSCRIBED.casefold()})


# --- the destination ----------------------------------------------------------------------
class NotionDestination(Destination):
    """One page per meeting in a Notion database: details, audio and transcript."""

    kind = "notion"
    capabilities = Capabilities(audio=True, transcript=True, metadata=True, fetch_audio=True)
    secret_options = ("token",)

    def __init__(self, name, options, secrets=None):
        super().__init__(name, options, secrets)
        self._api: NotionAPI | None = None
        self._schema: Schema | None = None

    # --- options and connection -------------------------------------------------------
    @property
    def database_id(self) -> str | None:
        return parse_id(self.options.get("database"))

    @property
    def api(self) -> NotionAPI:
        if self._api is None:
            token = self.secret("token")
            if not token:
                raise PermanentError(f"no Notion token stored for {self.name!r}: run `minuta destinations add "
                                     f"{self.name} --type notion` again")
            self._api = NotionAPI(token)
        return self._api

    def _sends(self, part: str) -> bool:
        return part in (self.options.get("send") or PARTS)

    def schema(self) -> Schema:
        if self._schema is None:
            if not self.database_id:
                raise PermanentError("no database: set `database` to the id or link of a Notion database")
            db = self.api.request("GET", f"/databases/{self.database_id}")
            self._schema = Schema.from_database(db, self.options)
        return self._schema

    def check(self) -> CheckResult:
        if not self.database_id:
            return CheckResult(False, "no database: set `database` to the id or link of a Notion database")
        if not self.secret("token"):
            return CheckResult(False, "no Notion token stored for this destination")
        try:
            limit = self.api.upload_limit()
            s = self.schema()
        except DestinationError as e:
            return CheckResult(False, redact(str(e)))
        msg = f"database {s.title_text!r} ({s.describe()})"
        if self._sends("audio"):
            msg += f"; audio files up to {_mb(limit)}"
        if s.notes:
            msg += "; " + "; ".join(s.notes)
        return CheckResult(True, msg)

    # --- pages ----------------------------------------------------------------------------
    def _properties(self, s: Schema, meeting, meta: dict, transcribed: bool | None) -> dict:
        props: dict = {s.title: {"title": [_text(split_text(meeting.title, TEXT_MAX)[0])]}}
        if "date" in s.roles and parse_time(meta.get("started_at")):
            props[s.roles["date"]] = {"date": {"start": meta["started_at"]}}
        if "duration" in s.roles and isinstance(meta.get("duration_seconds"), (int, float)):
            props[s.roles["duration"]] = {"number": round(meta["duration_seconds"] / 60, 1)}
        url = meta.get("url")
        if "url" in s.roles and isinstance(url, str) and url.startswith(("https://", "http://")) and len(url) <= 2000:
            props[s.roles["url"]] = {"url": url}
        if "status" in s.roles and transcribed is not None:
            name = s.roles["status"]
            if s.types.get(name) == "checkbox":
                if transcribed:  # never untick: the box may be ticked by hand
                    props[name] = {"checkbox": True}
            else:
                props[name] = {"select": {"name": TRANSCRIBED if transcribed else RECORDED}}
        return props

    def _meta(self, meeting, parts_meta: dict | None) -> dict:
        if not self._sends("metadata"):
            return {}
        return parts_meta if parts_meta is not None else meeting.metadata()

    def _page(self, previous: Receipt | None) -> dict | None:
        """The page of an earlier send, if it is still there and in this database."""
        if not (previous and previous.remote_id):
            return None
        try:
            page = self.api.request("GET", f"/pages/{previous.remote_id}")
        except NotFound:
            log.warning("%s: the Notion page of an earlier send is gone; creating another", self.name)
            return None
        if page.get("archived") or page.get("in_trash"):
            log.warning("%s: the Notion page of an earlier send is in the trash; creating another", self.name)
            return None
        if parse_id((page.get("parent") or {}).get("database_id")) != self.database_id:
            return None  # the destination now points to another database
        return page

    def _find(self, s: Schema, meeting, meta: dict) -> dict | None:
        """A page this integration already created for the meeting (its answer may have been lost)."""
        start = parse_time(meta.get("started_at"))
        if "date" not in s.roles or start is None:
            return None
        title = split_text(meeting.title, TEXT_MAX)[0]
        r = self.api.request("POST", f"/databases/{self.database_id}/query",
                             {"filter": {"property": s.title, "title": {"equals": title}}, "page_size": 100})
        bot = self.api.me().get("id")
        for page in r.get("results") or []:
            date = ((page.get("properties") or {}).get(s.roles["date"]) or {}).get("date") or {}
            if (_same_minute(parse_time(date.get("start")), start) and (page.get("created_by") or {}).get("id") == bot
                    and not page.get("archived") and not page.get("in_trash")):
                log.info("%s: found the page of an earlier attempt for %s", self.name, meeting.id)
                return page
        return None

    def _create(self, s: Schema, props: dict, children: list[dict]) -> dict:
        return self.api.request("POST", "/pages", {"parent": {"database_id": self.database_id},
                                                   "properties": props, "children": children})

    # --- audio ------------------------------------------------------------------------------
    def _upload(self, audio: Path) -> dict:
        """Upload the audio; returns what fetch_audio needs to give the same bytes back."""
        with tempfile.TemporaryDirectory(prefix="minuta-notion-") as tmp:
            src, name, ctype, suffix = _uploadable(audio, Path(tmp))
            size = src.stat().st_size
            limit = self.api.upload_limit()
            if size > limit:
                raise PermanentError(
                    f"the audio is {_mb(size)} and this Notion workspace takes files up to {_mb(limit)} (the free "
                    "plan allows 5 MB). Remove audio from this destination's `send` option, or upgrade the workspace.")
            digest = hashlib.sha256()
            if size <= SINGLE_PART_MAX:
                up = self.api.request("POST", "/file_uploads",
                                      {"mode": "single_part", "filename": name, "content_type": ctype})
                data = src.read_bytes()
                digest.update(data)
                res = self.api.request("POST", f"/file_uploads/{up['id']}/send", files={"file": (name, data, ctype)},
                                       timeout=TRANSFER_TIMEOUT)
            else:
                n = math.ceil(size / PART_SIZE)
                up = self.api.request("POST", "/file_uploads", {"mode": "multi_part", "number_of_parts": n,
                                                                "filename": name, "content_type": ctype})
                with open(src, "rb") as f:
                    for i in range(1, n + 1):
                        chunk = f.read(PART_SIZE)
                        digest.update(chunk)
                        self.api.request("POST", f"/file_uploads/{up['id']}/send", files={"file": (name, chunk, ctype)},
                                         data={"part_number": str(i)}, timeout=TRANSFER_TIMEOUT)
                res = self.api.request("POST", f"/file_uploads/{up['id']}/complete")
            if res.get("status") != "uploaded":
                raise DestinationError(f"the Notion upload ended as {res.get('status')!r}")
            return {"id": up["id"], "audio_suffix": suffix, "audio_bytes": size, "audio_sha256": digest.hexdigest()}

    def _audio_present(self, page_id: str) -> bool:
        return any(_audio_url(b) for b in self.api.children(page_id))

    # --- transcript -------------------------------------------------------------------------
    def _write_transcript(self, page_id: str, files: dict[str, Path]) -> str:
        blocks = _blocks_for(_transcript_doc(files))
        for b in self.api.children(page_id):  # an earlier copy (a retry, or a new transcription)
            if _is_transcript_heading(b):
                self.api.request("DELETE", f"/blocks/{b['id']}")
        heading = {"object": "block", "type": "heading_2",
                   "heading_2": {"rich_text": [_text(TRANSCRIPT_HEADING)], "is_toggleable": True}}
        created = self.api.append(page_id, [heading])
        heading_id = created[0]["id"]
        self.api.append(heading_id, blocks)
        return heading_id

    def _mark_transcribed(self, s: Schema, meeting, page_id: str) -> None:
        props = self._properties(s, meeting, {}, transcribed=True)
        props.pop(s.title)
        if props:
            self.api.request("PATCH", f"/pages/{page_id}", {"properties": props})

    # --- the interface ------------------------------------------------------------------------
    def send_meeting(self, meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        s = self.schema()
        meta = self._meta(meeting, parts.metadata)
        page = self._page(previous) or self._find(s, meeting, meta)
        same = bool(page and previous and page["id"] == parse_id(previous.remote_id))
        extra = dict(previous.extra) if (same and previous) else {}
        had_audio = False
        if page is not None and parts.audio is not None:
            had_audio = self._audio_present(page["id"])
        upload = self._upload(parts.audio) if (parts.audio is not None and not had_audio) else None
        block = ([{"object": "block", "type": "audio", "audio": {"type": "file_upload",
                                                                  "file_upload": {"id": upload["id"]}}}]
                 if upload else [])
        if page is None:
            # A new page starts as "recorded"; an existing one keeps its mark (it may be transcribed).
            page = self._create(s, self._properties(s, meeting, meta, transcribed=False), block)
        else:
            self.api.request("PATCH", f"/pages/{page['id']}",
                             {"properties": self._properties(s, meeting, meta, transcribed=None)})
            if block:
                self.api.append(page["id"], block)
        if upload:
            extra.update({k: v for k, v in upload.items() if k != "id"})
        extra["database"] = self.database_id
        if parts.transcript:
            extra["transcript_block"] = self._write_transcript(page["id"], parts.transcript)
            self._mark_transcribed(s, meeting, page["id"])
        has_audio = bool(upload) or had_audio or bool(same and previous and previous.audio)
        return Receipt(remote_id=page["id"], url=page.get("url"), audio=has_audio, extra=extra)

    def fetch_audio(self, meeting, receipt: Receipt, dest: Path) -> Path:
        if not receipt.remote_id:
            raise FileNotFoundError("the receipt has no Notion page")
        suffix = receipt.extra.get("audio_suffix") or ".ogg"
        target = dest.with_suffix(suffix) if dest.suffix == "" else dest
        tmp = target.with_name(f".{target.name}.partial")
        error = "no audio"
        for _ in range(3):
            # File links are signed and expire within the hour: read the page again on every try.
            url = next((u for u in map(_audio_url, self.api.children(receipt.remote_id)) if u), None)
            if url is None:
                raise FileNotFoundError("the Notion page has no audio")
            if not url.startswith("https://"):
                raise DestinationError("Notion gave a file link that is not https")
            digest = hashlib.sha256()
            try:
                # A plain GET: the link is already signed and must not carry the Notion token.
                with new_session().get(url, stream=True, timeout=TRANSFER_TIMEOUT) as r:
                    if r.status_code != 200:
                        error = f"download -> {r.status_code}"
                        continue
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1024 * 1024):
                            digest.update(chunk)
                            f.write(chunk)
            except requests.RequestException as e:
                error = f"download: {type(e).__name__}"
                continue
            expected = receipt.extra.get("audio_sha256")
            if expected and digest.hexdigest() != expected:
                tmp.unlink(missing_ok=True)
                raise DestinationError("the audio downloaded from Notion is not the one that was uploaded")
            os.replace(tmp, target)
            return target
        tmp.unlink(missing_ok=True)
        raise DestinationError(f"could not download the audio from Notion ({error})")

    # --- adding the destination ---------------------------------------------------------------
    @classmethod
    def configure(cls, name: str, options: dict, prompter=None, secrets=None) -> dict:
        """Ask for the token and the database, or create the database inside a page the user
        shared with the integration (`parent_page`)."""
        from minuta.i18n import t

        opts = dict(options)
        token = opts.get("token") or None
        if not token and secrets is not None and opts.get("token_secret"):
            token = secrets.get(opts["token_secret"])
        if not token and prompter is not None:
            print(t("notion.token_help"))
            token = prompter.password(t("notion.token")) or None
            if token:
                opts["token"] = token
        parent = opts.pop("parent_page", None)
        database = opts.get("database")
        if not database and not parent and prompter is not None:
            how = prompter.select(t("notion.how"), [("existing", t("notion.existing")), ("create", t("notion.create"))],
                             "existing")
            print(t("notion.share_help"))
            if how == "create":
                parent = prompter.text(t("notion.parent"))
            else:
                database = prompter.text(t("notion.database"))
        if not database and parent:
            page_id = parse_id(parent)
            if not page_id:
                raise PermanentError(f"{parent!r} is not a Notion page id or link")
            if not token:
                raise PermanentError("a token is needed to create the database")
            db = create_database(NotionAPI(token), page_id)
            print(t("notion.created", url=db.get("url") or db["id"]))
            database = db["id"]
        if database:
            opts["database"] = parse_id(database) or database
        return opts


def create_database(api: NotionAPI, page_id: str, title: str = DATABASE_TITLE) -> dict:
    """A database with one property per role, inline in the given page."""
    return api.request("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": page_id},
        "title": [_text(title)],
        "is_inline": True,
        "properties": {
            "Title": {"title": {}},
            "Date": {"date": {}},
            "Duration (min)": {"number": {"format": "number"}},
            "Meet": {"url": {}},
            "Transcribed": {"checkbox": {}},
        },
    })


def _uploadable(audio: Path, tmp: Path) -> tuple[Path, str, str, str]:
    """(file to upload, name for Notion, content type, suffix for fetch_audio). Notion takes Ogg but
    not the .opus name, so Ogg/Opus goes up unchanged as audio.ogg; WebM is remuxed to Ogg."""
    suffix = audio.suffix.lower()
    if suffix == ".mp3":
        return audio, "audio.mp3", "audio/mpeg", ".mp3"
    with open(audio, "rb") as f:
        magic = f.read(4)
    if magic == b"OggS":
        return audio, "audio.ogg", "audio/ogg", suffix if suffix in (".opus", ".ogg") else ".ogg"
    from minuta.audio import ffmpeg

    out = tmp / "audio.ogg"
    try:
        ffmpeg("-i", str(audio), "-vn", "-c:a", "copy", "-f", "ogg", str(out))
    except RuntimeError as e:
        raise PermanentError(f"cannot convert {audio.name} to Ogg for Notion: {e}") from None
    return out, "audio.ogg", "audio/ogg", ".ogg"
