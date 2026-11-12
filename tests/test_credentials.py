"""Secrets: keyring with a 0600 file fallback, environment override, and no secret ever in a log."""

from __future__ import annotations

import logging
import os
import stat

import pytest

from minuta import credentials, paths
from minuta.redact import RedactingFilter, redact, register_secret, setup_logging


class MemoryKeyring:
    priority = 5

    def __init__(self):
        self.data = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        self.data.pop((service, name), None)


class BrokenKeyring(MemoryKeyring):
    def set_password(self, *a):
        raise RuntimeError("locked")

    def get_password(self, *a):
        raise RuntimeError("locked")


def test_file_fallback_is_private(home):
    s = credentials.SecretStore("file")
    where = s.set("AI_GATEWAY_API_KEY", "vk_test_1234567890")
    assert where == str(paths.env_file())
    assert s.get("AI_GATEWAY_API_KEY") == "vk_test_1234567890"
    if os.name == "posix":
        assert stat.S_IMODE(paths.env_file().stat().st_mode) == 0o600
    s.delete("AI_GATEWAY_API_KEY")
    assert s.get("AI_GATEWAY_API_KEY") is None


def test_environment_wins(monkeypatch):
    s = credentials.SecretStore("file")
    s.set("AI_GATEWAY_API_KEY", "from-file-123456")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "from-env-123456")
    assert s.get("AI_GATEWAY_API_KEY") == "from-env-123456"


def test_keyring_is_used_and_cleans_old_file_copy(monkeypatch):
    kr = MemoryKeyring()
    monkeypatch.delenv("MINUTA_NO_KEYRING")
    monkeypatch.setattr(credentials, "_usable_keyring", lambda: kr)
    credentials.SecretStore("file").set("TOKEN_A", "old-value-123456")
    s = credentials.SecretStore("auto")
    assert s.set("TOKEN_A", "new-value-123456") == "keyring"
    assert kr.data[("minuta", "TOKEN_A")] == "new-value-123456"
    assert "TOKEN_A" not in paths.env_file().read_text()
    assert s.get("TOKEN_A") == "new-value-123456"


def test_broken_keyring_falls_back_to_file(monkeypatch):
    monkeypatch.delenv("MINUTA_NO_KEYRING")
    monkeypatch.setattr(credentials, "_usable_keyring", lambda: BrokenKeyring())
    s = credentials.SecretStore("auto")
    assert s.set("TOKEN_B", "value-b-123456") == str(paths.env_file())
    assert s.get("TOKEN_B") == "value-b-123456"


def test_names_are_validated():
    with pytest.raises(ValueError):
        credentials.SecretStore("file").set("lower", "x" * 10)


# --- logs -------------------------------------------------------------------------
def test_stored_secrets_never_reach_logs(caplog):
    s = credentials.SecretStore("file")
    s.set("MY_API_KEY", "zz-very-secret-value-42")
    logger = logging.getLogger("minuta.test")
    handler = caplog.handler
    handler.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info("calling with %s", "zz-very-secret-value-42")
        logger.warning("failed: %s", {"key": s.get("MY_API_KEY")})
        try:
            raise RuntimeError("bad key zz-very-secret-value-42")
        except RuntimeError:
            logger.exception("boom")
    text = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert "zz-very-secret-value-42" not in text
    assert "***" in text


@pytest.mark.parametrize("line", [
    "Authorization: Bearer abcdef123456789",
    "X-Minuta-Token: abcdefghijk12345",
    "api_key=sk-abcdefghijklmnop",
    '{"token": "abcdefghijklmnop"}',
    "password=hunter22222",
])
def test_credential_shapes_are_masked_even_if_unknown(line):
    out = redact(line)
    assert "***" in out
    assert not any(s in out for s in ("abcdef123456789", "abcdefghijk12345", "sk-abcdefghijklmnop", "hunter22222"))


def test_setup_logging_installs_the_filter_on_file_logs(tmp_path):
    register_secret("file-secret-123456")
    log_file = tmp_path / "x.log"
    setup_logging(logfile=log_file)
    logging.getLogger("minuta.x").error("value file-secret-123456 here")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "file-secret-123456" not in log_file.read_text()
    setup_logging()


def test_server_token_file_is_private_and_redacted(home):
    from minuta import pairing

    token = pairing.get_token(create=True)
    f = paths.config_dir() / "server-token"
    if os.name == "posix":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert token not in redact(f"header {token}")
