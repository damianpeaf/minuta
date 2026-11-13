"""The server's endpoints for the extension's page (`app.html`) and for `minuta` itself.

Each one is a thin call into `actions`: what an action does lives there, here only the HTTP shape.
The server checks host, origin, token and body size before a request gets here; secrets can be sent
(a destination's token, the API key) but are never sent back, only whether they are configured.

    GET  /i18n                          no token. {"language", "strings"}: the page's texts
    GET  /app/status                    `minuta status` plus the server's own state
    GET  /doctor                        `minuta doctor`, each problem with where to fix it
    GET  /meetings/<id>                 one meeting: details, deliveries, transcript
    GET  /meetings/<id>/engines         engines to transcribe it with, time and cost of each
    POST /meetings/<id>/transcribe      {"engine", "model", "max_usd", "confirm"} -> {"job"}; 409 asks to confirm a cost
    POST /meetings/<id>/open            {"what": "folder" | "transcript"}
    POST /retry                         {"meetings": [ids]} (empty: everything that failed) -> {"job"}
    GET  /jobs  GET /jobs/<id>  POST /jobs/<id>/cancel
    GET  /destinations                  configured (secrets as yes/no) and types
    POST /destinations/form             {"type", "name", "answers"} -> next fields, or complete
    POST /destinations                  {"type", "name", "answers", "force"}: add
    POST /destinations/<name>/test      {"live"}
    POST /destinations/<name>/enable | /disable | /remove
    GET  /settings  POST /settings      recording mode, engine, language, name...
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

from minuta import actions
from minuta.i18n import forced_language, strings, t
from minuta.meetings import MeetingLookupError
from minuta.redact import redact

log = logging.getLogger(__name__)
MAX_API_BODY = 64 * 1024
OPEN = {("GET", "/i18n")}  # answered without a token


def is_api(method: str, path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    return bool(parts) and parts[0] in ("i18n", "app", "doctor", "meetings", "retry", "jobs", "destinations",
                                        "settings") and not (method == "GET" and path == "/meetings")


def needs_token(method: str, path: str) -> bool:
    return (method, path) not in OPEN


def handle(app, method: str, path: str, body: dict) -> tuple[int, dict]:
    """Route one request. `app` is the server's App (config, jobs, its own state)."""
    app.reload_config()
    lang = app.cfg["general"].get("language", "en")
    with forced_language(lang):
        try:
            return _route(app, method, [unquote(p) for p in path.split("/") if p], body)
        except MeetingLookupError as e:
            return 404, {"error": t(e.key, **e.kw)}
        except actions.NotFound:
            return 404, {"error": t("api.not_found")}
        except ValueError as e:
            return 400, {"error": redact(str(e))}
        except Exception as e:  # noqa: BLE001 - an answer instead of a dropped connection
            log.exception("%s %s failed", method, path)
            return 500, {"error": redact(str(e))[:300]}


def _route(app, method: str, p: list[str], body: dict) -> tuple[int, dict]:
    cfg = app.cfg
    head = p[0]
    if head == "i18n" and method == "GET":
        lang = cfg["general"].get("language", "en")
        return 200, {"language": lang, "strings": strings(lang)}
    if p == ["app", "status"] and method == "GET":
        return 200, actions.status(cfg, live=app.status())
    if head == "doctor" and method == "GET":
        return 200, {"checks": actions.doctor(cfg)}
    if head == "meetings":
        return _meetings(app, method, p[1:], body)
    if head == "retry" and method == "POST":
        return _retry(app, body)
    if head == "jobs":
        return _jobs(app, method, p[1:])
    if head == "destinations":
        return _destinations(app, method, p[1:], body)
    if head == "settings":
        if method == "GET":
            return 200, actions.settings(cfg)
        if method == "POST":
            out = actions.save_settings(_dict(body, "settings"))
            app.reload_config()
            return 200, out
    return 404, {"error": t("api.not_found")}


def _meetings(app, method: str, p: list[str], body: dict) -> tuple[int, dict]:
    if not p:
        return 404, {"error": t("api.not_found")}
    m = actions.meeting(app.cfg, p[0])
    action = p[1] if len(p) > 1 else None
    if method == "GET" and action is None:
        return 200, actions.meeting_detail(app.cfg, m)
    if method == "GET" and action == "engines":
        return 200, {"engines": actions.engine_options(app.cfg, m)}
    if method == "POST" and action == "open":
        return 200, actions.open_meeting(app.cfg, m, str(body.get("what") or ""))
    if method == "POST" and action == "transcribe":
        return _transcribe(app, m, body)
    return 404, {"error": t("api.not_found")}


def _transcribe(app, m, body: dict) -> tuple[int, dict]:
    max_usd = body.get("max_usd")
    window = body.get("window")
    if window is not None:
        if not (isinstance(window, list) and len(window) == 2 and all(isinstance(x, (int, float)) for x in window)):
            raise ValueError("window must be [from_seconds, to_seconds]")
        window = (float(window[0]), float(window[1]))
    plan = actions.plan_transcription(app.cfg, m, body.get("engine") or None, body.get("model") or None,
                                      float(max_usd) if max_usd is not None else None, window)
    if plan["over_cap"]:
        return 402, {"error": t("transcribe.over_cap", cap=plan["cap"]), "plan": plan}
    if plan["paid"] and body.get("confirm") is not True:
        return 409, {"needs_confirmation": True, "plan": plan}
    cfg = app.cfg
    job = app.jobs.submit("transcribe", lambda progress: actions.run_transcription(cfg, m, plan, progress),
                          meeting=m.id, serial=True)
    return 202, {"job": _job(app, job), "plan": plan}


def _retry(app, body: dict) -> tuple[int, dict]:
    refs = body.get("meetings") or None
    if refs is not None and not (isinstance(refs, list) and all(isinstance(r, str) for r in refs)):
        raise ValueError("meetings must be a list of ids")
    cfg = app.cfg
    targets = actions.retry_targets(cfg, refs)
    job = app.jobs.submit("retry", lambda progress: {"rows": actions.retry(cfg, targets, progress)} if targets
                          else {"rows": []})
    return 202, {"job": _job(app, job), "meetings": len(targets)}


def _jobs(app, method: str, p: list[str]) -> tuple[int, dict]:
    if not p and method == "GET":
        return 200, {"jobs": [_job(app, j) for j in app.jobs.all()]}
    job = app.jobs.get(p[0]) if p else None
    if job is None:
        return 404, {"error": t("api.not_found")}
    if method == "GET" and len(p) == 1:
        return 200, {"job": _job(app, job)}
    if method == "POST" and p[1:] == ["cancel"]:
        app.jobs.cancel(job.id)
        return 200, {"job": _job(app, job)}
    return 404, {"error": t("api.not_found")}


def _job(app, job) -> dict:
    """A job, with its step said in words, as the terminal says it."""
    out = job.to_dict()
    out["message"] = actions.step_label(job.kind, job.step, job.info) if job.step else ""
    finished = job.state == "done"
    out["done"] = [label for s in job.steps if s != "done" and (finished or s != job.step)
                   if (label := actions.step_label(job.kind, s, job.step_info.get(s, {}), done=True))]
    out["position"] = app.jobs.position(job)
    return out


def _destinations(app, method: str, p: list[str], body: dict) -> tuple[int, dict]:
    cfg = app.cfg
    if not p and method == "GET":
        data = actions.destinations(cfg)
        for d in data["configured"]:
            d["secrets"] = actions.destination_secrets(cfg["destinations"][d["name"]])
        for x in data["types"]:
            key = f"dest.type.{x['type']}"
            x["label"] = t(key) if t(key) != key else x["description"]
        return 200, data
    if method != "POST":
        return 404, {"error": t("api.not_found")}
    if p == ["form"]:
        return 200, actions.destination_form(cfg, str(body.get("type") or ""), str(body.get("name") or ""),
                                             _dict(body.get("answers") or {}, "answers"))
    if not p:
        out = actions.add_destination(str(body.get("type") or ""), str(body.get("name") or ""),
                                      _dict(body.get("answers") or {}, "answers"), force=body.get("force") is True)
        app.reload_config()
        return (200 if out["ok"] else 400), out
    if len(p) == 2:
        name, action = p
        if action == "test":
            return 200, actions.test_destination(cfg, name, live=body.get("live") is True)
        if action in ("enable", "disable"):
            out = actions.set_destination_enabled(name, action == "enable")
        elif action == "remove":
            out = actions.remove_destination(name)
        else:
            return 404, {"error": t("api.not_found")}
        app.reload_config()
        return 200, out
    return 404, {"error": t("api.not_found")}


def _dict(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")
    return value
