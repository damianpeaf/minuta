"""No private names, hosts or company jargon in the repository.

The forbidden terms are stored as sha256 hashes of their lowercase form, so this test does not
publish them. Every word of every tracked (or new, not ignored) text file, and every path, is hashed
and compared. To add a term: `python -c "import hashlib; print(hashlib.sha256('term'.encode()).hexdigest())"`.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = {
    "900a871b6f252ead3f35b38ff052b429d285a757455afe53243414dc34c60032",
    "568215cea055350ff6a3b3794db85c9b6e167bf06621385f822f956820edb19a",
    "17948953967fbb5b789b224b4c74acadba3dbd94380ed3eed72609fd6e8d7d05",
    "c9fd92c735c7609969a0ab48b6dc2fda85a06e135196571c06708222baf5a2e7",
    "ef31635ceda978303ac396fef7eda1977797cdb41f2eb82821ae784ae1b88e8f",
    "82d3042b941378bc994b8c2596fa69e4370bd2cb5400c4c043c996dc197fda7c",
    "5ef68465886fa04d3e0bbe86b59d964dd98e5775e95717df978d8bedee6ff16c",
    "0f83de146cdf1f860db9e1173aa734f43ca0c61374e5b8ed650ec4dcf86c5512",
    "7dbf7f81fb0094bfba81cff10f80182ba6875f7a52c2d0578efbb32bd7cd21ef",
    "c28f28a284eadc147f0a50c19219b7d3784af2603c1496e53707ab206ec3fb84",
    "3cb9bd30615f42c80b0d1e8e3f16838b747426a1464a8502067c58d5be3eab59",
    "4445368414bd3e63fe07988f75bfa4556350ba3b71db78a0c3b57e562cd670cb",
    "b4da8c3e348d45704768253ca87cbdd9ced6b14f4662e274a0f4227d523193be",
    "1eea0ac129efd9b9abceefeaeab5e8efb93556c5b4980d8f262d95a98f55e831",
    "ef956f84257a3e9f86dbabf4d5075b77ac3c3efdf188b2b5f183415bc890ca8d",
    "2f6b4110d597254c5ac9f0a97c880c7727cbc79817de697e5fafbacf3eaeae9c",
    "dae73c138fe593c9321b82529283484ee39d7f5f8e5c2f423692b12dc8023bc3",
    "78193ef266c1e3c2ce4ea2a86d7fc87e8c52799653faaac8536533a1c9300f82",
    "7e79f5f27dd43f09a8ca806ccd7773324292f12ae4ff68bec12bd22bf67275c6",
    "8870e6cbc014734d787e5fc27e777f1fc8009bd258357141a36e763269b124b9",
}
# The author's name is allowed only where authorship is declared.
AUTHOR = "9d452a5f7c9dd57db7389ed785b24f9b702bfcf80dd25fd48f27a6a2cba95a39"
AUTHOR_FILES = {"LICENSE", "pyproject.toml"}

WORD = re.compile(r"[^\W_]+", re.UNICODE)


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
                             capture_output=True, check=True).stdout.decode()
        return [ROOT / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
        return [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]


def digest(word: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", word.lower()).encode()).hexdigest()


def offenders(text: str, allow_author: bool) -> set[str]:
    found = set()
    for word in WORD.findall(text):
        h = digest(word)
        if h in FORBIDDEN or (h == AUTHOR and not allow_author):
            found.add(word)
    return found


def test_the_check_itself_works():
    assert offenders("hello world", False) == set()
    assert digest("ÁBC") == digest("ábc")


def test_no_private_terms_in_the_repository():
    problems = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        hits = offenders(rel, False)
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                text = ""  # binary files (icons)
            hits |= offenders(text, path.name in AUTHOR_FILES)
        if hits:
            problems.append(f"{rel}: {len(hits)} forbidden term(s)")
    assert not problems, "\n".join(problems)
