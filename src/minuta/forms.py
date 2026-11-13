"""The questions a destination asks when it is added, as data.

A destination lists them in `form(name, known, secrets)`: the fields still missing given what is
known so far (its options plus earlier answers). A question that depends on an earlier answer comes
in a later step. `ask()` walks the steps: the terminal answers each field with questionary, the
extension's page answers a whole step at once through `Answers`, and both run the same validation,
so the same answers give the same options.

Keys that start with `_` are answers that are not options themselves ("copy to another folder?");
the destination's `configure()` turns them into options and drops them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from minuta.i18n import STRINGS, t

KINDS = ("text", "secret", "bool", "select", "path")
MAX_STEPS = 20
MAX_TRIES = 5


@dataclass
class Field:
    key: str
    kind: str  # text | secret | bool | select | path
    label: str  # i18n key
    help: str | None = None  # i18n key, shown before the question
    default: Any = None
    choices: list[tuple[str, str]] = field(default_factory=list)  # (value, i18n key or plain text)
    required: bool = False
    terminal: tuple[str, ...] = ()  # choices only a terminal can take (they run a program there)
    terminal_hint: str | None = None  # i18n key: what to do in a terminal instead
    validate: Callable[[Any], Any] | None = None  # returns the clean value or raises ValueError

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown field kind {self.kind!r}")

    def to_dict(self) -> dict:
        """What the page needs to draw the field, in the current language. Never holds a secret."""
        out = {"key": self.key, "kind": self.kind, "label": _text(self.label), "required": self.required}
        if self.help:
            out["help"] = _text(self.help)
        if self.default is not None and self.kind != "secret":
            out["default"] = self.default
        if self.kind == "select":
            out["choices"] = [{"value": v, "label": _text(label), "terminal": v in self.terminal}
                              for v, label in self.choices]
        if self.terminal_hint:
            out["terminal_hint"] = _text(self.terminal_hint)
        return out


def _text(key: str) -> str:
    return t(key) if key in STRINGS else key


def clean(f: Field, value: Any, terminal: bool = True) -> Any:
    """The answer to `f` as it is saved, or ValueError with a message for the user."""
    if f.kind == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float)):
        raise ValueError(t("form.invalid_value"))
    value = str(value).strip()
    if f.kind == "select":
        allowed = [v for v, _ in f.choices if terminal or v not in f.terminal]
        if value not in allowed:
            raise ValueError(t("form.not_a_choice"))
        return value
    if not value:
        if f.required:
            raise ValueError(t("form.required"))
        return value
    if len(value) > 4096:
        raise ValueError(t("form.too_long"))
    return f.validate(value) if f.validate is not None else value


class Incomplete(Exception):
    """A step that `Answers` cannot finish: fields without an answer, or answers that are not valid."""

    def __init__(self, fields: list[Field], errors: dict[str, str]):
        super().__init__(", ".join(f.key for f in fields))
        self.fields, self.errors = fields, errors


class Answers:
    """Answers given all at once (the extension's page). A step with a missing or invalid answer raises
    Incomplete instead of asking. Choices that need a terminal are refused."""

    terminal = False
    interactive = True

    def __init__(self, answers: dict):
        self.answers = dict(answers or {})

    def step(self, fields: list[Field]) -> dict:
        values, errors, missing = {}, {}, False
        for f in fields:
            if f.key not in self.answers:
                missing = True
                continue
            try:
                values[f.key] = clean(f, self.answers[f.key], terminal=False)
            except ValueError as e:
                errors[f.key] = str(e)
        if errors or missing:
            raise Incomplete(fields, errors)
        return values


def _prompt(prompter, f: Field):
    label = _text(f.label)
    if f.kind == "secret":
        return prompter.password(label)
    if f.kind == "bool":
        return prompter.confirm(label, bool(f.default))
    if f.kind == "select":
        return prompter.select(label, [(v, _text(lb)) for v, lb in f.choices], f.default)
    if f.kind == "path":
        return prompter.path(label, f.default or "")
    return prompter.text(label, "" if f.default is None else str(f.default))


def ask(cls, name: str, options: dict, prompter, secrets=None, **kw) -> dict | None:
    """Ask every step of `cls.form()` through `prompter` and return the options with the answers, or
    None when an answer stays invalid after a few tries. Raises Incomplete through `Answers`."""
    known = dict(options)
    terminal = getattr(prompter, "terminal", True)
    for _ in range(MAX_STEPS):
        fields = cls.form(name, known, secrets, **kw)
        if not fields:
            return known
        if hasattr(prompter, "step"):
            known.update(prompter.step(fields))
        else:
            for f in fields:
                if f.help:
                    print(_text(f.help))
                for _try in range(MAX_TRIES):
                    try:
                        known[f.key] = clean(f, _prompt(prompter, f), terminal=terminal)
                        break
                    except ValueError as e:
                        print(t("form.not_valid", error=e))
                else:
                    return None
        known = cls.form_effect(name, known, secrets, **kw)
    return None


def next_step(cls, name: str, options: dict, answers: dict, secrets=None) -> dict:
    """For the page: the next fields to show (with errors for the answers that are not valid), or
    `{"complete": True}` when every question has an answer. Nothing is created or saved here."""
    try:
        ask(cls, name, options, Answers(answers), secrets)
    except Incomplete as e:
        return {"complete": False, "fields": [f.to_dict() for f in e.fields], "errors": e.errors}
    return {"complete": True, "fields": [], "errors": {}}
