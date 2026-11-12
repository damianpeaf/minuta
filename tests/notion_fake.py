"""An in-memory Notion API, mounted on the destination's requests session.

It keeps state (databases, pages, blocks, file uploads) and enforces the limits the adapter must
respect, answering 400 like Notion does when one is broken: 2000 characters per rich text item,
100 blocks per append, 500 KB per body, part sizes of a multi-part upload, a file upload attached
only once it is complete. Signed file links live on another host and must be fetched without the
Notion token.
"""

from __future__ import annotations

import itertools
import json
import re
import uuid
from datetime import datetime
from email.parser import BytesParser
from email.policy import HTTP
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

TOKEN = "ntn_fake_token_0123456789abcdef"
BOT_ID = "b0b0b0b0-0000-4000-8000-000000000001"
FILES_HOST = "files.notion-fake.test"
MB = 1024 * 1024


def _id() -> str:
    return str(uuid.uuid4())


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class Reply(Exception):
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None, content: bytes = b""):
        self.status, self.body, self.headers, self.content = status, body, headers or {}, content


def error(status: int, code: str, message: str) -> Reply:
    return Reply(status, {"object": "error", "status": status, "code": code, "message": message})


class FakeNotion(BaseAdapter):
    def __init__(self, token: str = TOKEN, upload_limit: int = 5 * 1024 * MB):
        super().__init__()
        self.token = token
        self.upload_limit = upload_limit
        self.shared_pages: set[str] = set()
        self.databases: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}
        self.uploads: dict[str, dict] = {}
        self.log: list[tuple[str, str]] = []  # (method, path) of every API call
        self.downloads: list[dict] = []  # headers of every file download
        self.injected: list[dict] = []
        self.signature = itertools.count(1)
        self.expired: set[str] = set()
        self.corrupt_downloads = False
        self.expire_next_downloads = 0  # answer 403 like an expired signed link

    # --- test helpers -----------------------------------------------------------------
    def add_database(self, properties: dict[str, dict], title: str = "Meetings") -> str:
        db_id = _id()
        self.databases[db_id] = {"object": "database", "id": db_id, "title": [{"plain_text": title}],
                                 "url": f"https://www.notion.so/{db_id.replace('-', '')}",
                                 "properties": {n: {"id": n, "name": n, **p} for n, p in properties.items()}}
        return db_id

    def share_page(self) -> str:
        pid = _id()
        self.shared_pages.add(pid)
        return pid

    def inject(self, method: str, path_re: str, status: int, *, headers: dict | None = None, times: int = 1,
               after: bool = False, body: dict | None = None) -> None:
        """Answer `status` to the next `times` matching calls. `after`: do the work, then fail (a lost answer)."""
        self.injected.append({"method": method, "re": re.compile(path_re), "status": status, "headers": headers or {},
                              "times": times, "after": after, "body": body})

    def db_pages(self, db_id: str, include_trash: bool = False) -> list[dict]:
        return [p for p in self.pages.values() if p["parent"]["database_id"] == db_id
                and (include_trash or not p["archived"])]

    def page_children(self, page_id: str) -> list[dict]:
        return [self.blocks[b] for b in self.children.get(page_id, []) if not self.blocks[b].get("archived")]

    def calls(self, method: str, prefix: str) -> list[str]:
        return [p for m, p in self.log if m == method and p.startswith(prefix)]

    # --- transport --------------------------------------------------------------------
    def send(self, request, **kwargs):
        url = urlparse(request.url)
        headers = dict(request.headers)
        if url.hostname == FILES_HOST:
            return self._download(request, url, headers)
        path = url.path.removeprefix("/v1")
        self.log.append((request.method, path))
        try:
            if headers.get("Authorization") != f"Bearer {self.token}":
                raise error(401, "unauthorized", "API token is invalid.")
            if not headers.get("Notion-Version"):
                raise error(400, "missing_version", "Notion-Version header is required.")
            if request.body and len(request.body) > 500 * 1024 and "file_uploads" not in path:
                raise error(413, "payload_too_large", "Request body too large.")
            rule = self._rule(request.method, path)
            if rule and not rule["after"]:
                raise Reply(rule["status"], rule["body"] or {"object": "error", "code": "injected", "message": "x"},
                            rule["headers"])
            result = self._route(request, path, url, headers)
            if rule:
                raise Reply(rule["status"], rule["body"] or {"object": "error", "code": "injected", "message": "x"},
                            rule["headers"])
            return self._response(request, Reply(200, result))
        except Reply as r:
            return self._response(request, r)

    def close(self):
        pass

    def _rule(self, method: str, path: str) -> dict | None:
        for rule in self.injected:
            if rule["times"] > 0 and rule["method"] == method and rule["re"].search(path):
                rule["times"] -= 1
                return rule
        return None

    @staticmethod
    def _response(request, r: Reply) -> requests.Response:
        resp = requests.Response()
        resp.status_code = r.status
        resp._content = r.content if r.content else (json.dumps(r.body).encode() if r.body is not None else b"")
        resp.headers = CaseInsensitiveDict({"Content-Type": "application/json", **r.headers})
        resp.url = request.url
        resp.request = request
        resp.encoding = "utf-8"
        resp._content_consumed = True  # the body is all here: iter_content reads it, no raw stream
        return resp

    # --- routes -------------------------------------------------------------------------
    def _route(self, request, path: str, url, headers: dict) -> dict:
        m = request.method
        body = json.loads(request.body) if request.body and "json" in headers.get("Content-Type", "") else {}
        if m == "GET" and path == "/users/me":
            return {"object": "user", "id": BOT_ID, "type": "bot",
                    "bot": {"workspace_limits": {"max_file_upload_size_in_bytes": self.upload_limit}}}
        if m == "POST" and path == "/databases":
            return self._create_database(body)
        if mm := re.fullmatch(r"/databases/([^/]+)", path):
            if m == "GET":
                return self._database(mm.group(1))
        if mm := re.fullmatch(r"/databases/([^/]+)/query", path):
            return self._query(mm.group(1), body)
        if m == "POST" and path == "/pages":
            return self._create_page(body)
        if mm := re.fullmatch(r"/pages/([^/]+)", path):
            page = self._page(mm.group(1))
            if m == "GET":
                return page
            if m == "PATCH":
                return self._update_page(page, body)
        if mm := re.fullmatch(r"/blocks/([^/]+)/children", path):
            parent = mm.group(1)
            if parent not in self.pages and parent not in self.blocks:
                raise error(404, "object_not_found", f"Could not find block with ID: {parent}.")
            if m == "GET":
                return self._list_children(parent, parse_qs(url.query))
            if m == "PATCH":
                return {"object": "list", "results": self._append(parent, body.get("children") or [])}
        if (mm := re.fullmatch(r"/blocks/([^/]+)", path)) and m == "DELETE":
            b = self.blocks.get(mm.group(1))
            if b is None or b.get("archived"):
                raise error(404, "object_not_found", "Could not find block.")
            b["archived"] = True
            return b
        if m == "POST" and path == "/file_uploads":
            return self._new_upload(body)
        if mm := re.fullmatch(r"/file_uploads/([^/]+)/send", path):
            return self._send_part(mm.group(1), request, headers)
        if mm := re.fullmatch(r"/file_uploads/([^/]+)/complete", path):
            return self._complete(mm.group(1))
        raise error(400, "invalid_request_url", f"Invalid request URL {m} {path}")

    def _database(self, db_id: str) -> dict:
        db = self.databases.get(db_id)
        if db is None:
            raise error(404, "object_not_found", f"Could not find database with ID: {db_id}.")
        return db

    def _page(self, page_id: str) -> dict:
        page = self.pages.get(page_id)
        if page is None:
            raise error(404, "object_not_found", f"Could not find page with ID: {page_id}.")
        return page

    def _create_database(self, body: dict) -> dict:
        parent = (body.get("parent") or {}).get("page_id")
        if parent not in self.shared_pages:
            raise error(404, "object_not_found", f"Could not find page with ID: {parent}.")
        props = body.get("properties") or {}
        if sum(1 for p in props.values() if "title" in p) != 1:
            raise error(400, "validation_error", "A database needs exactly one title property.")
        typed = {n: {"type": next(iter(p)), **p} for n, p in props.items()}
        db_id = self.add_database(typed, title="".join(t["text"]["content"] for t in body.get("title") or []))
        self.databases[db_id]["parent"] = {"type": "page_id", "page_id": parent}
        return self.databases[db_id]

    def _check_rich(self, rich: list) -> None:
        if len(rich) > 100:
            raise error(400, "validation_error", "rich_text should have at most 100 items.")
        for item in rich:
            content = item["text"]["content"]
            if _u16(content) > 2000:
                raise error(400, "validation_error",
                            f"body.rich_text[].text.content.length should be <= 2000, instead was {_u16(content)}.")

    def _properties(self, db: dict, props: dict, page: dict | None = None) -> dict:
        schema = db["properties"]
        out = dict(page["properties"]) if page else {}
        for name, value in props.items():
            if name not in schema:
                raise error(400, "validation_error", f"{name} is not a property that exists.")
            kind = schema[name]["type"]
            if kind not in value:
                raise error(400, "validation_error", f"{name} is expected to be {kind}.")
            v = value[kind]
            if kind == "title":
                self._check_rich(v)
                v = [{**t, "plain_text": t["text"]["content"]} for t in v]
            if kind == "select" and v is not None:
                opts = schema[name].setdefault("select", {}).setdefault("options", [])
                if v["name"] not in [o["name"] for o in opts]:
                    opts.append({"name": v["name"]})
            if kind == "date" and v is not None:
                start = v["start"]
                if "T" in start:  # like Notion: to the minute
                    when = datetime.fromisoformat(start.replace("Z", "+00:00")).replace(second=0, microsecond=0)
                    start = when.isoformat(timespec="milliseconds")
                v = {"start": start, "end": None, "time_zone": None}
            out[name] = {"type": kind, kind: v}
        return out

    def _create_page(self, body: dict) -> dict:
        db_id = (body.get("parent") or {}).get("database_id")
        db = self._database(db_id)
        props = body.get("properties") or {}
        if not any(db["properties"][n]["type"] == "title" for n in props if n in db["properties"]):
            raise error(400, "validation_error", "Title is not provided.")
        children = body.get("children") or []
        if len(children) > 100:
            raise error(400, "validation_error", "body.children.length should be <= 100.")
        page_id = _id()
        page = {"object": "page", "id": page_id, "archived": False, "in_trash": False,
                "parent": {"type": "database_id", "database_id": db_id}, "created_by": {"object": "user", "id": BOT_ID},
                "url": f"https://www.notion.so/{page_id.replace('-', '')}",
                "properties": self._properties(db, props)}
        self.pages[page_id] = page
        self._append(page_id, children)
        return page

    def _update_page(self, page: dict, body: dict) -> dict:
        if page["archived"] and not ("archived" in body or "in_trash" in body):
            raise error(400, "validation_error", "Can't edit block that is archived.")
        if "properties" in body:
            page["properties"] = self._properties(self.databases[page["parent"]["database_id"]], body["properties"],
                                                  page)
        for k in ("archived", "in_trash"):
            if k in body:
                page["archived"] = page["in_trash"] = bool(body[k])
        return page

    def _query(self, db_id: str, body: dict) -> dict:
        self._database(db_id)
        pages = self.db_pages(db_id)
        f = body.get("filter") or {}
        if "title" in f:
            want = f["title"].get("equals")
            pages = [p for p in pages
                     if "".join(t["plain_text"] for t in p["properties"][f["property"]]["title"]) == want]
        return {"object": "list", "results": pages, "has_more": False, "next_cursor": None}

    def _list_children(self, parent: str, q: dict) -> dict:
        size = min(int((q.get("page_size") or ["100"])[0]), 100)
        start = int((q.get("start_cursor") or ["0"])[0])
        ids = [b for b in self.children.get(parent, []) if not self.blocks[b].get("archived")]
        out = [self._render(self.blocks[b]) for b in ids[start:start + size]]
        more = start + size < len(ids)
        return {"object": "list", "results": out, "has_more": more, "next_cursor": str(start + size) if more else None}

    def _render(self, block: dict) -> dict:
        b = json.loads(json.dumps(block))
        if b["type"] == "audio":
            sig = next(self.signature)
            b["audio"] = {"type": "file", "file": {"url": f"https://{FILES_HOST}/{block['upload']}/audio.ogg?sig={sig}",
                                                  "expiry_time": "2099-01-01T00:00:00.000Z"}}
            b.pop("upload", None)
        return b

    def _append(self, parent: str, children: list[dict]) -> list[dict]:
        if len(children) > 100:
            raise error(400, "validation_error", "body.children.length should be <= 100.")
        made = []
        for c in children:
            kind = c["type"]
            block = {"object": "block", "id": _id(), "type": kind, "created_by": {"object": "user", "id": BOT_ID},
                     "has_children": False, "archived": False}
            if kind == "audio":
                up = self.uploads.get(c["audio"]["file_upload"]["id"])
                if up is None or up["status"] != "uploaded":
                    raise error(400, "validation_error", "File upload is not in the uploaded status.")
                block["upload"] = up["id"]
                block["audio"] = {"type": "file"}
            else:
                data = c[kind]
                self._check_rich(data.get("rich_text") or [])
                block[kind] = {**data, "rich_text": [{**t, "plain_text": t["text"]["content"]}
                                                     for t in data.get("rich_text") or []]}
            self.blocks[block["id"]] = block
            self.children.setdefault(parent, []).append(block["id"])
            if parent in self.blocks:
                self.blocks[parent]["has_children"] = True
            made.append(self._render(block))
        return made

    # --- file uploads -----------------------------------------------------------------------
    def _new_upload(self, body: dict) -> dict:
        mode = body.get("mode", "single_part")
        up = {"object": "file_upload", "id": _id(), "status": "pending", "mode": mode, "filename": body.get("filename"),
              "content_type": body.get("content_type"), "number_of_parts": body.get("number_of_parts"),
              "parts": {}}
        if mode == "multi_part" and not (1 <= int(up["number_of_parts"] or 0) <= 1000):
            raise error(400, "validation_error", "number_of_parts must be between 1 and 1000.")
        self.uploads[up["id"]] = up
        return {k: v for k, v in up.items() if k != "parts"}

    def _send_part(self, upload_id: str, request, headers: dict) -> dict:
        up = self.uploads.get(upload_id)
        if up is None:
            raise error(404, "object_not_found", "File upload not found.")
        if up["status"] != "pending":
            raise error(400, "validation_error", f"File upload is in status {up['status']}.")
        raw = b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + request.body
        msg = BytesParser(policy=HTTP).parsebytes(raw)
        fields = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            fields[name] = (part.get_content_type(), part.get_payload(decode=True), part.get_filename())
        if "file" not in fields:
            raise error(400, "validation_error", "file is required.")
        ctype, data, filename = fields["file"]
        if ctype != up["content_type"] or filename != up["filename"]:
            raise error(400, "validation_error", "content type or file name does not match the upload.")
        if up["mode"] == "single_part":
            if len(data) > 20 * MB:
                raise error(400, "validation_error", "Single-part uploads are limited to 20 MB.")
            if len(data) > self.upload_limit:
                raise error(400, "validation_error", "File exceeds the workspace limit.")
            up["parts"][1] = data
            up["status"] = "uploaded"
        else:
            n = int(fields.get("part_number", (None, b"0", None))[1])
            if not 1 <= n <= up["number_of_parts"]:
                raise error(400, "validation_error", "part_number out of range.")
            if n < up["number_of_parts"] and not (5 * MB <= len(data) <= 20 * MB):
                raise error(400, "validation_error", "Parts must be between 5 and 20 MB, except the last one.")
            up["parts"][n] = data
        return {k: v for k, v in up.items() if k != "parts"}

    def _complete(self, upload_id: str) -> dict:
        up = self.uploads.get(upload_id)
        if up is None or up["mode"] != "multi_part":
            raise error(400, "validation_error", "Only multi-part uploads are completed.")
        if sorted(up["parts"]) != list(range(1, up["number_of_parts"] + 1)):
            raise error(400, "validation_error", "Some parts are missing.")
        up["status"] = "uploaded"
        return {k: v for k, v in up.items() if k != "parts"}

    def upload_bytes(self, upload_id: str) -> bytes:
        up = self.uploads[upload_id]
        return b"".join(up["parts"][i] for i in sorted(up["parts"]))

    def _download(self, request, url, headers: dict) -> requests.Response:
        self.downloads.append(headers)
        upload_id = url.path.strip("/").split("/")[0]
        sig = (parse_qs(url.query).get("sig") or [""])[0]
        if "Authorization" in headers:
            return self._response(request, Reply(400, {"message": "signed links take no Authorization header"}))
        if self.expire_next_downloads > 0:
            self.expire_next_downloads -= 1
            self.expired.add(sig)
        if sig in self.expired or upload_id not in self.uploads:
            expired = Reply(403, None, {"Content-Type": "application/xml"}, b"<Error>expired</Error>")
            return self._response(request, expired)
        data = self.upload_bytes(upload_id)
        if self.corrupt_downloads:
            data = data[:-1] + bytes([data[-1] ^ 1])
        return self._response(request, Reply(200, None, {"Content-Type": "audio/ogg"}, data))
