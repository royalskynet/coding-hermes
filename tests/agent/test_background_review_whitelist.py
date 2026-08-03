"""Regression test: background review tool whitelist deny message rendering.

Ensures the deny message rendered by `set_thread_tool_whitelist` contains
the four actual tool names (memory, skills_list, skill_view, skill_manage)
and NO `{tool_name}` placeholder residue.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.background_review import spawn_background_review_thread
from hermes_cli.plugins import (
    clear_thread_tool_whitelist,
    get_pre_tool_call_block_message,
)
from model_tools import get_tool_definitions


class TestBackgroundReviewWhitelist:
    """Test the exact whitelist tools used in background review."""

    def test_skills_toolset_has_three_tools(self):
        """Skills toolset should have exactly 3 tools."""
        defs = get_tool_definitions(enabled_toolsets=["skills"], quiet_mode=True)
        tool_names = {d["function"]["name"] for d in defs}
        assert tool_names == {"skills_list", "skill_view", "skill_manage"}

    def test_memory_toolset_has_one_tool(self):
        """Memory toolset should have exactly 1 tool."""
        defs = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
        tool_names = {d["function"]["name"] for d in defs}
        assert tool_names == {"memory"}

    def test_combined_whitelist_is_four_tools(self):
        """Combined whitelist should have exactly 4 tools."""
        defs = get_tool_definitions(
            enabled_toolsets=["skills", "memory"], quiet_mode=True
        )
        tool_names = {d["function"]["name"] for d in defs}
        assert tool_names == {"skills_list", "skill_view", "skill_manage", "memory"}


class TestBackgroundReviewDenyMessage:
    """Read the runtime deny installed by the real background-review producer."""

    def setup_method(self):
        clear_thread_tool_whitelist()

    def teardown_method(self):
        clear_thread_tool_whitelist()

    def _run_producer(self, *, memory_enabled, user_profile_enabled=False):
        observed = {}

        class ReviewAgent:
            def __init__(self, **_kwargs):
                self._session_messages = []

            def run_conversation(self, **_kwargs):
                observed["deny"] = get_pre_tool_call_block_message("terminal", {})
                observed["memory"] = get_pre_tool_call_block_message("memory", {})

            def shutdown_memory_provider(self):
                pass

            def close(self):
                pass

        parent = SimpleNamespace(
            provider="openrouter",
            model="test-model",
            platform="cli",
            session_id="parent-session",
            session_start="start",
            reasoning_config=None,
            enabled_toolsets=None,
            disabled_toolsets=None,
            _credential_pool=None,
            request_overrides={},
            max_tokens=None,
            acp_command=None,
            acp_args=[],
            _memory_store=None,
            _memory_enabled=memory_enabled,
            _user_profile_enabled=user_profile_enabled,
            _cached_system_prompt="cached",
            memory_notifications="off",
            background_review_callback=None,
            _current_main_runtime=lambda: {},
            _safe_print=lambda *_args, **_kwargs: None,
            _emit_auxiliary_failure=lambda *_args, **_kwargs: None,
        )

        with patch("run_agent.AIAgent", ReviewAgent), \
             patch("agent.background_review._resolve_review_runtime", return_value={
                 "provider": "openrouter", "model": "test-model", "routed": False,
             }):
            target, _prompt = spawn_background_review_thread(
                parent,
                [{"role": "user", "content": "review"}],
                review_memory=True,
                review_skills=True,
            )
            target()
        return observed

    def test_deny_message_contains_all_four_tool_names(self):
        """Producer denial names every tool it actually whitelists."""
        deny_msg = self._run_producer(memory_enabled=True)["deny"]

        # All four tool names must appear in the deny message
        assert deny_msg is not None
        assert "skills_list" in deny_msg
        assert "skill_view" in deny_msg
        assert "skill_manage" in deny_msg
        assert "memory" in deny_msg

    def test_deny_message_has_no_placeholder_residue(self):
        """Producer denial substitutes the runtime denied tool name."""
        deny_msg = self._run_producer(memory_enabled=True)["deny"]

        assert deny_msg is not None
        assert "{tool_name}" not in deny_msg
        assert "terminal" in deny_msg
        # All four tool names must be present
        assert "memory" in deny_msg
        assert "skills_list" in deny_msg

    def test_deny_message_format_matches_background_review(self):
        """Producer leaves its memory tool callable through the runtime gate."""
        assert self._run_producer(memory_enabled=True)["memory"] is None

    def test_whitelist_only_skills_when_memory_disabled(self):
        """Producer omits memory when both memory/profile features are disabled."""
        observed = self._run_producer(memory_enabled=False)
        deny_msg = observed["deny"]
        assert deny_msg is not None
        assert "skills_list" in deny_msg
        assert "skill_view" in deny_msg
        assert "skill_manage" in deny_msg
        assert "memory" not in deny_msg
        assert "{tool_name}" not in deny_msg
        assert "terminal" in deny_msg
        assert observed["memory"] is not None
