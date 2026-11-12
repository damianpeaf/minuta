"""Transcription through Vercel AI Gateway, with the user's own key.

The gateway does not expose OpenAI's `/audio/transcriptions`: it has its own route, the one
`@ai-sdk/gateway` calls (`POST /v4/ai/transcription-model`, audio in base64, model in a header).

The gateway passes `providerOptions` as is to the AI SDK provider, and each provider reads only
its own key and option names. A wrong key or option name is ignored without an error, which is
why `provider_options` below is tested per model.
"""

from __future__ import annotations

import base64

import requests

from minuta.transcription.common import (
    WINDOW,
    are_words,
    by_windows,
    glossary,
    language_iso,
    phrases,
    prompt,
    with_punctuation,
)

URL = "https://ai-gateway.vercel.sh/v4/ai/transcription-model"
KEY_NAME = "AI_GATEWAY_API_KEY"

# Short names offered by the setup. Prices are estimates in US$ per minute of audio sent; the
# gateway reports the real cost of each request and the budget is corrected with it. Override with
# `usd_per_minute` under [transcription.vercel] when your provider's price is different.
PRESETS = {
    "whisper": {"model": "openai/whisper-1", "usd_per_minute": 0.006},
    "gpt-4o-transcribe": {"model": "openai/gpt-4o-transcribe", "usd_per_minute": 0.006, "window_max_seconds": 30},
    "grok": {"model": "spacexai/grok-stt", "usd_per_minute": None},
    "gemini": {"model": "google/gemini-3.5-transcribe", "usd_per_minute": None, "word_timestamps": False,
               "window_max_seconds": 30},
}
# Used when a model's price is unknown: deliberately high, so the cap errs on the safe side.
FALLBACK_USD_PER_MINUTE = 0.02


def preset_for(model: str) -> dict:
    for p in PRESETS.values():
        if p["model"] == model:
            return p
    return {}


def usd_per_minute(opts: dict) -> float:
    if opts.get("usd_per_minute") is not None:
        return float(opts["usd_per_minute"])
    known = preset_for(opts.get("model", "")).get("usd_per_minute")
    return float(known) if known is not None else FALLBACK_USD_PER_MINUTE


def option(opts: dict, key: str, default):
    if key in opts:
        return opts[key]
    return preset_for(opts.get("model", "")).get(key, default)


def provider_options(model: str, tcfg: dict, word_timestamps: bool = True) -> dict:
    """`providerOptions` for the model, with language and glossary the way each provider expects."""
    provider, name = model.split("/", 1)
    language = tcfg.get("language")
    terms = glossary(tcfg)

    if provider == "openai":
        o: dict = {"temperature": 0}
        if language:
            o["language"] = language_iso(language)  # ISO-639-1: "es-419" is rejected
        if prompt(tcfg):
            o["prompt"] = prompt(tcfg)
        if name == "whisper-1":
            # gpt-4o-transcribe only answers json without times: asking it for segments does nothing.
            o["timestampGranularities"] = ["segment"]
        return {"openai": o}

    if provider == "google":
        o = {"mode": "VERBATIM"}
        if language:
            o["languageCodes"] = [language]  # BCP-47; a hint, does not force the language
        if word_timestamps:
            # Without wordTimestamp no time comes back at all. With it, customVocabulary is
            # rejected with a 400: one or the other.
            o["wordTimestamp"] = True
        elif terms:
            o["customVocabulary"] = terms
        return {"google": o}

    if provider in ("xai", "spacexai"):
        # `language` in Grok does not force the language: it only turns on number formatting. The
        # SDK provider reads the "xai" key; it is repeated as "spacexai" because the model is now
        # published under that prefix, and each provider ignores the other's key.
        o = {}
        if language:
            o["language"] = language_iso(language)
            o["format"] = True
        if terms:
            o["keyterm"] = [t[:50] for t in terms[:100]]
        return {"xai": o, "spacexai": o}

    return {}


def real_cost(response: dict) -> float | None:
    """The gateway returns what it charged in providerMetadata.gateway.cost."""
    try:
        return float(response["providerMetadata"]["gateway"]["cost"])
    except (KeyError, TypeError, ValueError):
        return None


def segments_from(response: dict, seconds: float) -> list[dict]:
    """{"start","end","text"} from a gateway response, already grouped into phrases."""
    segs = [
        {"start": float(s["startSecond"]), "end": float(s["endSecond"]), "text": (s.get("text") or "").strip()}
        for s in response.get("segments") or []
    ]
    segs = [s for s in segs if s["text"]]
    text = (response.get("text") or "").strip()
    if not segs:
        # A model without times: the whole text spans the window. That is why those models use
        # short windows (`window_max_seconds`).
        return [{"start": 0.0, "end": seconds, "text": text}] if text else []
    if are_words(segs):
        return phrases(with_punctuation(segs, text))
    return segs


class VercelEngine:
    name = "vercel"

    def __init__(self, opts: dict, api_key: str | None, session: requests.Session | None = None):
        if not api_key:
            raise RuntimeError(f"{KEY_NAME} is not set: run `minuta setup` or store it with the keyring")
        self.opts = opts
        self.model = opts.get("model") or PRESETS["whisper"]["model"]
        self.api_key = api_key
        self.http = session or requests.Session()
        self.usd_per_minute = usd_per_minute(opts)

    @property
    def window(self) -> float:
        return float(option(self.opts, "window_max_seconds", WINDOW) or WINDOW)

    def request(self, audio: bytes, tcfg: dict) -> dict:
        r = self._post(audio, tcfg)
        if r.status_code == 400 and "-" in (tcfg.get("language") or "") and self.model.startswith("google/"):
            # If Gemini does not know the regional variant, retry with the bare language.
            r = self._post(audio, {**tcfg, "language": language_iso(tcfg["language"])})
        if r.status_code >= 400:
            raise RuntimeError(f"vercel {self.model} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _post(self, audio: bytes, tcfg: dict) -> requests.Response:
        return self.http.post(
            URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "ai-gateway-protocol-version": "0.0.1",
                "ai-gateway-auth-method": "api-key",
                "ai-transcription-model-specification-version": "4",
                "ai-model-id": self.model,
            },
            json={"audio": base64.b64encode(audio).decode(), "mediaType": "audio/mpeg",
                  "providerOptions": provider_options(self.model, tcfg,
                                                      preset_for(self.model).get("word_timestamps", True))},
            timeout=600,
        )

    def transcribe_channel(self, x, segments, tcfg: dict, budget=None, cost=None) -> list[dict]:
        def one(audio: bytes, sec: float) -> list[dict]:
            r = self.request(audio, tcfg)
            if budget is not None and cost is not None:
                budget.adjust(cost(sec), real_cost(r))
            return segments_from(r, sec)

        return by_windows(x, segments, one, maximum=self.window, budget=budget, cost=cost)
