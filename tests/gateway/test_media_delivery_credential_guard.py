"""Tests for media delivery credential guard — path and content validation."""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from gateway.platforms.base import validate_media_delivery_path, _check_file_content_for_secrets
from agent.file_safety import CREDENTIAL_HOME_SUBPATHS


class TestMediaDeliveryCredentialGuard:
    """Credential guard for both path-based and content-based rejection."""

    def test_creds_directory_rejected_nonstrict_mode(self, tmp_path, monkeypatch):
        """~/.creds/creds.json is rejected by path denylist in non-strict mode."""
        # Mock the home directory
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")

        creds_dir = fake_home / ".creds"
        creds_dir.mkdir()
        creds_file = creds_dir / "creds.json"
        creds_file.write_text('{"key": "ghp_FAKEFAKEFAKE1234567890"}')

        # Should be rejected
        assert validate_media_delivery_path(str(creds_file)) is None

    def test_creds_directory_rejected_strict_mode(self, tmp_path, monkeypatch):
        """~/.creds/creds.json is rejected by path denylist in strict mode."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")

        creds_dir = fake_home / ".creds"
        creds_dir.mkdir()
        creds_file = creds_dir / "creds.json"
        creds_file.write_text('{"key": "ghp_FAKEFAKEFAKE1234567890"}')

        # Should be rejected even in strict mode
        assert validate_media_delivery_path(str(creds_file)) is None

    def test_env_file_rejected_by_content(self, tmp_path, monkeypatch):
        """Arbitrary .env file with credentials is rejected by content guard."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")

        # .env at home root is blocked by path denylist, test one elsewhere
        tmp_env = tmp_path / "project" / ".env"
        tmp_env.parent.mkdir()
        tmp_env.write_text("API_KEY=sk-1234567890abcdef")

        # Should be rejected by content guard (contains sk- token)
        assert validate_media_delivery_path(str(tmp_env)) is None

    def test_content_guard_github_token(self, tmp_path, monkeypatch):
        """File containing GitHub PAT is rejected by content guard."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")

        secret_file = tmp_path / "secrets.txt"
        secret_file.write_text("Here is my GitHub token: ghp_FAKEFAKEFAKE1234567890")

        # Should be rejected due to content
        assert validate_media_delivery_path(str(secret_file)) is None

    def test_content_guard_env_style_uppercase_key(self, tmp_path, monkeypatch):
        """File with env-style uppercase secret keys is rejected."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")

        config_file = tmp_path / "config.json"
        config_file.write_text(
            '{"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_FAKEFAKEFAKE1234567890"}'
        )

        # Should be rejected due to JSON field with secret-like key
        assert validate_media_delivery_path(str(config_file)) is None

    def test_normal_file_allowed(self, tmp_path, monkeypatch):
        """Normal file without credentials is allowed."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")

        report_file = tmp_path / "report.md"
        report_file.write_text("# Project Report\n\nAll is well.")

        # Should be allowed
        result = validate_media_delivery_path(str(report_file))
        assert result == str(report_file)

    def test_large_file_skips_content_guard(self, tmp_path, monkeypatch):
        """Large files skip content guard but still respect path denylist."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")

        # Create a file larger than 1 MB with secret content
        large_file = tmp_path / "large.txt"
        large_file.write_text("x" * (2 * 1024 * 1024))  # 2 MB

        # Should be allowed (size > limit, content guard skipped)
        result = validate_media_delivery_path(str(large_file))
        assert result == str(large_file)

    def test_binary_file_skips_content_guard(self, tmp_path, monkeypatch):
        """Binary files (non-text extensions) skip content guard."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")

        # Create a .pdf (or any binary file)
        binary_file = tmp_path / "document.pdf"
        binary_file.write_bytes(b"%PDF-1.4\nsome binary data here")

        # Should be allowed (not text extension)
        result = validate_media_delivery_path(str(binary_file))
        assert result == str(binary_file)

    def test_credential_home_subpaths_consistency(self):
        """CREDENTIAL_HOME_SUBPATHS includes .creds."""
        # Ensure .creds is in the shared constant
        assert ".creds" in CREDENTIAL_HOME_SUBPATHS
        # And verify base.py uses it
        from gateway.platforms.base import _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS
        assert ".creds" in _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS

    def test_check_file_content_for_secrets_prefix_pattern(self, tmp_path):
        """Direct check: prefix patterns like ghp_ are detected."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Token: ghp_FAKEFAKEFAKE1234567890")

        assert _check_file_content_for_secrets(test_file) is True

    def test_check_file_content_for_secrets_json_pattern(self, tmp_path):
        """Direct check: JSON secret field patterns are detected."""
        test_file = tmp_path / "config.json"
        test_file.write_text('{"api_key": "sk-FAKEFAKEFAKE1234567890"}')

        assert _check_file_content_for_secrets(test_file) is True

    def test_check_file_content_for_secrets_env_pattern(self, tmp_path):
        """Direct check: ENV assignment patterns are detected."""
        test_file = tmp_path / "vars.txt"
        test_file.write_text("DATABASE_PASSWORD=my_secret_password")

        assert _check_file_content_for_secrets(test_file) is True

    def test_check_file_content_for_secrets_normal_file(self, tmp_path):
        """Direct check: normal file without secrets returns False."""
        test_file = tmp_path / "normal.txt"
        test_file.write_text("This is a normal report file with no secrets.")

        assert _check_file_content_for_secrets(test_file) is False

    def test_check_file_content_for_secrets_binary_skipped(self, tmp_path):
        """Direct check: binary files are skipped (non-text extension)."""
        test_file = tmp_path / "image.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n... binary data")

        assert _check_file_content_for_secrets(test_file) is False

    def test_check_file_content_for_secrets_size_limit(self, tmp_path):
        """Direct check: large files skip content scan."""
        test_file = tmp_path / "huge.txt"
        # Write 2 MB with secret content
        test_file.write_text("ghp_FAKEFAKEFAKE1234567890" * (100000))

        assert _check_file_content_for_secrets(test_file) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
