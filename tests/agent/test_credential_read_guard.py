"""Regression tests for WO-2026-08-30 credential read-side hardening.

Two gaps closed by this work order:

1. ``agent.file_safety.get_read_block_error()`` previously only blocked
   basename-exact credential files (``.env``, ``auth.json``, ...). It did
   NOT block anything under ``~/.creds`` or ``~/.ssh`` by prefix, so
   ``~/.creds/creds.json`` (the actual file leaked on 2026-08-29) and
   ``~/.ssh/id_rsa`` were readable via the ``read_file`` tool. Fixed by
   reusing the existing ``CREDENTIAL_HOME_SUBPATHS`` constant (already used
   by the write-denylist / media-delivery guard in
   ``gateway/platforms/base.py``) for a prefix match against the live
   ``$HOME``.

2. ``agent.auxiliary_client.resolve_provider_client()``'s ``custom`` provider
   branch resolved a scoped ``OPENAI_API_KEY`` env var *before* checking
   whether the custom endpoint's host matches the main model's host. When
   the host matched, this let an unrelated global ``OPENAI_API_KEY`` shadow
   the correctly host-scoped main-model key. Fixed by reordering the
   fallback chain so the host-gated main-key lookup runs first.

All tests use ``monkeypatch`` to redirect ``HOME`` to a tmp directory and
create fake (non-secret) files there — the real ``~/.creds`` tree is never
read.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.file_safety import CREDENTIAL_HOME_SUBPATHS, get_read_block_error


# ── Step 1: read-side credential directory prefix guard ────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point $HOME (and os.path.expanduser) at an isolated tmp tree with
    fake credential-shaped files. No real credential is ever created or
    read — the file *contents* below are placeholder strings, not secrets.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    creds_dir = home / ".creds"
    creds_dir.mkdir()
    (creds_dir / "creds.json").write_text('{"placeholder": "not-a-real-secret"}')

    creds_github = creds_dir / "github"
    creds_github.mkdir()
    (creds_github / ".env").write_text("GITHUB_TOKEN=ghp_FAKEFAKEFAKE1234567890\n")
    # Prefix-style filename (not basename ".env") — must still be caught by
    # the prefix match, not the basename check.
    (creds_dir / "anything.env").write_text("TOKEN=ghp_FAKEFAKEFAKE1234567890\n")

    ssh_dir = home / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("-----BEGIN FAKE PRIVATE KEY-----\nplaceholder\n")

    docs_dir = home / "Documents"
    docs_dir.mkdir()
    (docs_dir / "note.md").write_text("just a normal note\n")

    return home


class TestReadBlockCredentialPrefix:
    def test_creds_json_blocked(self, fake_home):
        err = get_read_block_error(str(fake_home / ".creds" / "creds.json"))
        assert err is not None

    def test_creds_subdir_dotenv_blocked(self, fake_home):
        err = get_read_block_error(str(fake_home / ".creds" / "github" / ".env"))
        assert err is not None

    def test_creds_prefix_named_file_blocked(self, fake_home):
        """Proves this is a PREFIX match, not a basename match: the file is
        named ``anything.env`` (not one of the recognized ``.env*``
        basenames) but still lives under ``~/.creds`` so it must be blocked.
        """
        err = get_read_block_error(str(fake_home / ".creds" / "anything.env"))
        assert err is not None

    def test_ssh_id_rsa_blocked(self, fake_home):
        err = get_read_block_error(str(fake_home / ".ssh" / "id_rsa"))
        assert err is not None

    def test_ordinary_file_not_blocked(self, fake_home):
        """Guards against over-blocking: a normal file outside any
        protected subpath must resolve to None (allowed)."""
        err = get_read_block_error(str(fake_home / "Documents" / "note.md"))
        assert err is None

    def test_shares_constant_with_write_write_media_denylist(self):
        """Read guard and write/media-delivery guard must consume the same
        CREDENTIAL_HOME_SUBPATHS constant so they can't drift apart (this is
        exactly the divergence this work order closes for the read side)."""
        from gateway.platforms.base import _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS

        for sub in CREDENTIAL_HOME_SUBPATHS:
            assert sub in _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS
        assert ".creds" in CREDENTIAL_HOME_SUBPATHS
        assert ".ssh" in CREDENTIAL_HOME_SUBPATHS


# ── Step 2: auxiliary_client custom-provider key shadowing ─────────────────


def _capture_custom_key(monkeypatch, *, main_base_url, main_api_key,
                         custom_base_url, openai_api_key_env=""):
    """Drive resolve_provider_client("custom", ...) far enough to observe
    which api_key value reaches the OpenAI client constructor, without any
    network I/O.
    """
    import agent.auxiliary_client as aux

    token = aux.set_runtime_main("anthropic", "some-model", base_url=main_base_url, api_key=main_api_key)
    try:
        if openai_api_key_env:
            monkeypatch.setenv("OPENAI_API_KEY", openai_api_key_env)
        else:
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        captured = {}

        def _fake_create_openai_client(*, api_key, base_url, **_extra):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            return MagicMock()

        with patch("agent.auxiliary_client._create_openai_client", side_effect=_fake_create_openai_client):
            aux.resolve_provider_client(
                "custom",
                model="gpt-4o-mini",
                explicit_base_url=custom_base_url,
            )
        return captured.get("api_key")
    finally:
        aux._RUNTIME_MAIN_CONTEXT.reset(token)


class TestCustomProviderKeyShadowing:
    def test_different_host_does_not_leak_main_key(self, monkeypatch):
        """Custom endpoint host differs from the main model's host → the
        host-gated main key must NOT be used. (No OPENAI_API_KEY env set,
        so the only source of a leaked credential here would be the main
        key crossing hosts — which must not happen.)
        """
        key = _capture_custom_key(
            monkeypatch,
            main_base_url="https://api.anthropic.com/v1",
            main_api_key="sk-main-FAKEFAKE0000000000",
            custom_base_url="https://attacker.example.com/v1",
        )
        assert key != "sk-main-FAKEFAKE0000000000"
        assert key == "no-key-required"

    def test_same_host_still_inherits_main_key(self, monkeypatch):
        """Same-host inheritance (#9318) must keep working after the
        reorder — this is the existing behavior the fix must not break."""
        key = _capture_custom_key(
            monkeypatch,
            main_base_url="https://api.example-host.com/v1",
            main_api_key="sk-main-FAKEFAKE1111111111",
            custom_base_url="https://api.example-host.com/v1/custom",
        )
        assert key == "sk-main-FAKEFAKE1111111111"

    def test_same_host_prefers_main_key_over_shadowing_openai_env(self, monkeypatch):
        """This is the shadowing bug itself: previously a scoped
        OPENAI_API_KEY env var was checked BEFORE the host-gated main key,
        so an unrelated global OPENAI_API_KEY could shadow the correct,
        verified same-host main key. After the reorder, the host-gated main
        key wins when the host matches.
        """
        key = _capture_custom_key(
            monkeypatch,
            main_base_url="https://api.example-host.com/v1",
            main_api_key="sk-main-FAKEFAKE2222222222",
            custom_base_url="https://api.example-host.com/v1/custom",
            openai_api_key_env="sk-openai-FAKEFAKE3333333333",
        )
        assert key == "sk-main-FAKEFAKE2222222222"
        assert key != "sk-openai-FAKEFAKE3333333333"
