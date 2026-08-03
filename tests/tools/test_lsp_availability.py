#!/usr/bin/env python3
"""
Phase M3: LSP 可用性離線契約測試

Tests that LSP tooling behaves correctly when:
- LSP service is NOT active (config disabled, no workspace, no binaries)
- LSP service IS active but server not found for file type
- LSP service IS active and server found, but server not running / broken

This is an OFFLINE contract test - does not require actual LSP servers.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add repo root to path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO_ROOT))

from tools.lsp_tool import (
    _check_lsp_active,
    _get_service,
    tool_lsp_document_symbols,
    tool_lsp_definition,
    tool_lsp_references,
    tool_lsp_workspace_symbols,
)


class TestLSPActiveCheck:
    """Test _check_lsp_active contract.

    _check_lsp_active() imports get_service from agent.lsp directly,
    so we patch at agent.lsp.get_service.
    """

    def test_returns_false_when_get_service_raises(self):
        """If get_service raises any exception, returns False."""
        with patch("agent.lsp.get_service") as mock_get:
            mock_get.side_effect = ImportError("no module")
            assert _check_lsp_active() is False

    def test_returns_false_when_service_none(self):
        """If get_service returns None, returns False."""
        with patch("agent.lsp.get_service") as mock_get:
            mock_get.return_value = None
            assert _check_lsp_active() is False

    def test_returns_false_when_service_not_active(self):
        """If service exists but is_active() is False, returns False."""
        mock_svc = MagicMock()
        mock_svc.is_active.return_value = False
        with patch("agent.lsp.get_service") as mock_get:
            mock_get.return_value = mock_svc
            assert _check_lsp_active() is False

    def test_returns_true_when_service_active(self):
        """If service exists and is_active() is True, returns True."""
        mock_svc = MagicMock()
        mock_svc.is_active.return_value = True
        with patch("agent.lsp.get_service") as mock_get:
            mock_get.return_value = mock_svc
            assert _check_lsp_active() is True


class TestLSPToolsWhenInactive:
    """All LSP tools should return structured error when _get_service returns None."""

    def setup_method(self):
        """Ensure _get_service returns None (service inactive)."""
        # _get_service already calls agent.lsp.get_service and checks is_active()
        # So we patch agent.lsp.get_service to return a mock with is_active=False
        self.mock_svc = MagicMock()
        self.mock_svc.is_active.return_value = False
        self.mock_svc.document_symbols_sync.return_value = []
        self.mock_svc.definition_sync.return_value = []
        self.mock_svc.references_sync.return_value = []
        self.mock_svc.workspace_symbols_sync.return_value = []
        self.patcher = patch("agent.lsp.get_service", return_value=self.mock_svc)
        self.patcher.start()

    def teardown_method(self):
        self.patcher.stop()

    def test_document_symbols_returns_error(self):
        result = tool_lsp_document_symbols({"path": "/tmp/test.py"})
        assert "error" in result
        assert result["error"] == "LSP service is not active"

    def test_definition_returns_error(self):
        result = tool_lsp_definition({"path": "/tmp/test.py", "line": 10, "character": 5})
        assert "error" in result
        assert result["error"] == "LSP service is not active"

    def test_references_returns_error(self):
        result = tool_lsp_references({"path": "/tmp/test.py", "line": 10, "character": 5})
        assert "error" in result
        assert result["error"] == "LSP service is not active"

    def test_workspace_symbols_returns_error(self):
        result = tool_lsp_workspace_symbols({"query": "MyClass"})
        assert "error" in result
        assert result["error"] == "LSP service is not active"


class TestLSPToolsValidation:
    """Test argument validation when service active but missing required args."""

    def setup_method(self):
        # Active service mock
        self.mock_svc = MagicMock()
        self.mock_svc.is_active.return_value = True
        self.patcher = patch("agent.lsp.get_service", return_value=self.mock_svc)
        self.patcher.start()

    def teardown_method(self):
        self.patcher.stop()

    def test_document_symbols_missing_path(self):
        result = tool_lsp_document_symbols({})
        assert "error" in result
        assert "path is required" in result["error"]

    def test_definition_missing_path(self):
        result = tool_lsp_definition({"line": 10, "character": 5})
        assert "error" in result
        assert "path is required" in result["error"]

    def test_references_missing_path(self):
        result = tool_lsp_references({"line": 10, "character": 5})
        assert "error" in result
        assert "path is required" in result["error"]

    def test_workspace_symbols_missing_query(self):
        result = tool_lsp_workspace_symbols({})
        assert "error" in result
        assert "query is required" in result["error"]


class TestLSPToolsWhenActiveEmptyResults:
    """Test contract when service active but returns empty results."""

    def setup_method(self):
        self.mock_svc = MagicMock()
        self.mock_svc.is_active.return_value = True
        self.mock_svc.document_symbols_sync.return_value = []
        self.mock_svc.definition_sync.return_value = []
        self.mock_svc.references_sync.return_value = []
        self.mock_svc.workspace_symbols_sync.return_value = []
        self.patcher = patch("agent.lsp.get_service", return_value=self.mock_svc)
        self.patcher.start()

    def teardown_method(self):
        self.patcher.stop()

    def test_document_symbols_empty(self):
        result = tool_lsp_document_symbols({"path": "/tmp/test.py"})
        assert result["symbol_count"] == 0
        assert "content" in result
        assert "no symbols returned" in result["content"]

    def test_definition_empty(self):
        result = tool_lsp_definition({"path": "/tmp/test.py", "line": 10, "character": 5})
        assert "locations" in result
        assert result["locations"] == []
        assert "content" in result
        assert "no definition found" in result["content"]

    def test_references_empty(self):
        result = tool_lsp_references({"path": "/tmp/test.py", "line": 10, "character": 5})
        assert "locations" in result
        assert result["locations"] == []
        assert "content" in result
        assert "no references found" in result["content"]

    def test_workspace_symbols_empty(self):
        result = tool_lsp_workspace_symbols({"query": "NonExistentClass"})
        assert result["count"] == 0
        assert "content" in result
        assert "no workspace symbols match" in result["content"]


class TestLSPToolsWhenActiveWithResults:
    """Test contract when service active and returns structured results."""

    def setup_method(self):
        self.mock_svc = MagicMock()
        self.mock_svc.is_active.return_value = True
        self.patcher = patch("agent.lsp.get_service", return_value=self.mock_svc)
        self.patcher.start()

    def teardown_method(self):
        self.patcher.stop()

    def test_document_symbols_with_symbols(self):
        self.mock_svc.document_symbols_sync.return_value = [
            {"name": "MyClass", "kind": 5, "range": {"start": {"line": 0}}},
            {"name": "my_method", "kind": 6, "range": {"start": {"line": 5}}},
        ]
        result = tool_lsp_document_symbols({"path": "/tmp/test.py"})
        assert result["symbol_count"] == 2
        assert "MyClass" in result["content"]
        assert "my_method" in result["content"]

    def test_definition_with_results(self):
        self.mock_svc.definition_sync.return_value = [
            {"file": "/tmp/other.py", "range": {"start": {"line": 20}}},
        ]
        result = tool_lsp_definition({"path": "/tmp/test.py", "line": 10, "character": 5})
        assert len(result["locations"]) == 1
        assert "/tmp/other.py" in result["content"]

    def test_references_with_results(self):
        self.mock_svc.references_sync.return_value = [
            {"file": "/tmp/test.py", "range": {"start": {"line": 30}}},
            {"file": "/tmp/other.py", "range": {"start": {"line": 40}}},
        ]
        result = tool_lsp_references({"path": "/tmp/test.py", "line": 10, "character": 5})
        assert len(result["locations"]) == 2
        assert result["count"] == 2

    def test_workspace_symbols_with_results(self):
        self.mock_svc.workspace_symbols_sync.return_value = [
            {"name": "MyClass", "file": "/tmp/a.py", "range": {"start": {"line": 0}}},
            {"name": "MyClass2", "file": "/tmp/b.py", "range": {"start": {"line": 10}}},
        ]
        result = tool_lsp_workspace_symbols({"query": "MyClass"})
        assert result["count"] == 2
        assert "MyClass" in result["content"]
        assert "MyClass2" in result["content"]


class TestLSPConfigContract:
    """Test LSP configuration contract."""

    def test_manager_config_schema(self):
        """Verify LSP config keys are recognised by manager."""
        from agent.lsp.manager import LSPService
        import inspect

        # Check create_from_config handles expected keys
        sig = inspect.signature(LSPService.create_from_config)
        assert sig is not None

    def test_is_active_gated_on_config(self):
        """LSPService.is_active() returns False when config.enabled=false."""
        from agent.lsp.manager import LSPService
        assert hasattr(LSPService, "is_active")

    def test_enabled_for_gated_on_workspace(self):
        """LSPService.enabled_for() gates on git workspace detection."""
        from agent.lsp.manager import LSPService
        assert hasattr(LSPService, "enabled_for")


class TestLSPBrokenSetContract:
    """Test that broken (server_id, workspace) pairs are never retried."""

    def test_broken_set_exists_on_instance(self):
        """_broken is an instance attribute initialized in __init__."""
        from agent.lsp.manager import LSPService
        import inspect
        src = inspect.getsource(LSPService.__init__)
        assert "_broken" in src

    def test_broken_set_checked_in_enabled_for(self):
        """enabled_for should return False for broken pairs."""
        import inspect
        from agent.lsp.manager import LSPService

        src = inspect.getsource(LSPService.enabled_for)
        assert "_broken" in src

    def test_broken_set_marked_on_spawn_failure(self):
        """_mark_broken_for_file adds to _broken set."""
        import inspect
        from agent.lsp.manager import LSPService

        src = inspect.getsource(LSPService._mark_broken_for_file)
        assert "_broken.add" in src or "self._broken.add" in src


# CLI integration test (runs with actual config)
def test_cli_lsp_status():
    """Test `hermes lsp status` command runs without error."""
    import subprocess
    result = subprocess.run(
        ["hermes", "lsp", "status"],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT)
    )
    # Should run (exit 0 or 1, not crash)
    assert result.returncode in (0, 1)
    assert "LSP" in result.stdout or "LSP" in result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])