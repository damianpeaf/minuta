"""Did this phrase come out in Spanish?

Some API models pick the language on their own and, on greetings or short stretches, sometimes
write Spanish speech as Portuguese or English. Function words (articles, pronouns, connectors)
that exist in one language and not the other are counted. Technical English jargon does not
count, so a Spanish sentence with English terms is not flagged.

Only used when the configured transcription language is Spanish.
"""

from __future__ import annotations

import re

# Only words not spelled the same in Spanish: "a", "e", "o", "de", "que", "no" are useless here.
PORTUGUESE = frozenset("""
não você vocês obrigado obrigada tchau estou então também muito muita isso isto ele ela eles elas
uma com em ao às pra pro né meu minha seu sua nós eu graças deus ontem hoje bom boa sim agora
fazer tem têm vai vou está estão é são foi tá tô aí até mais mas porém coisa gente legal beleza
""".split()) - {"mas", "más", "gente", "está", "tem", "vai"}

ENGLISH = frozenset("""
the and you your is are was were it's i'm that's what this with have has we they just thank thanks
good night morning yes of to for my not but be do does did don't can't i she them there here
yeah yep nope huh go gonna wanna i'll we're you're
""".split())

SPANISH = frozenset("""
el la los las un una unos unas del al lo es son fue ser estar está están estoy era hay
y pero porque pues entonces bueno digamos verdad va vamos ya sí también muy más menos
yo tú usted ustedes nosotros él ella ellos ellas mi mis tu tus su sus nuestro me te nos les le se
este esta esto estos estas ese esa eso esos esas aquí ahí allí qué cómo cuándo dónde
con sin para por en entre hasta desde sobre como cuando donde todo todos nada algo
tengo tiene tienen hacer hace puede pueden creo sé quiero gracias hola buenas tardes días
""".split())

# What models write over noise or murmur. If the fallback only gives this, there was no phrase.
FILLER = {"gracias", "ahora", "mmm", "mm", "eh", "y", "ok", "okay", "sí", "si"}

_WORD = re.compile(r"[a-záéíóúüñçãõâêôà']+", re.IGNORECASE)


def scores(text: str) -> dict[str, int]:
    words = [w.lower() for w in _WORD.findall(text)]
    return {
        "es": sum(w in SPANISH for w in words),
        "pt": sum(w in PORTUGUESE for w in words),
        "en": sum(w in ENGLISH for w in words),
    }


def off_spanish(text: str) -> str | None:
    """"pt" or "en" if the phrase looks like that language; None if Spanish or unknown."""
    s = scores(text)
    other = max(("pt", "en"), key=lambda k: s[k])
    if s[other] >= 2 and s[other] > s["es"]:
        return other
    # Short phrases with nothing Spanish in them, like "huh. Bye." over noise.
    if s[other] >= 1 and s["es"] == 0 and len(_WORD.findall(text)) <= 4:
        return other
    return None


def applies(language: str | None) -> bool:
    return bool(language) and language.lower().split("-")[0] == "es"
