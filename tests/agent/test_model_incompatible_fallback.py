"""Regression tests for _is_model_incompatible_error and fallback path.

Covers the 400-class capability-mismatch errors that should trigger
provider fallback instead of aborting the auxiliary task.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.auxiliary_client import (
    _is_model_incompatible_error,
    _is_model_not_found_error,
    _is_rate_limit_error,
    _is_connection_error,
    async_call_llm,
    call_llm,
)


def _openai_api_error(status_code: int, message: str, headers: dict | None = None):
    """Mimic openai.APIError / APIStatusError shape."""
    exc = Exception(message)
    exc.status_code = status_code
    if headers:
        response = SimpleNamespace(headers=headers)
        exc.response = response
    return exc


def _openai_no_status(message: str, headers: dict | None = None):
    """Some SDK versions omit status_code on the exception object itself."""
    exc = Exception(message)
    if headers:
        response = SimpleNamespace(headers=headers)
        exc.response = response
    return exc


class TestIsModelIncompatibleError:
    """Capability mismatch 400s that should trigger fallback."""

    def test_codex_chatgpt_account_gating(self):
        """Codex with ChatGPT account: 'glm-5.2 model is not supported when using Codex'."""
        exc = _openai_api_error(
            400,
            "Error code: 400 - {'detail': \"The 'glm-5.2' model is not supported when using Codex with a ChatGPT account.\"}",
        )
        assert _is_model_incompatible_error(exc) is True

    def test_model_not_supported_generic(self):
        exc = _openai_api_error(
            400, "The requested model is not supported with this account."
        )
        assert _is_model_incompatible_error(exc) is True

    def test_not_supported_for_this_account(self):
        exc = _openai_api_error(400, "Model 'x' not supported for this account.")
        assert _is_model_incompatible_error(exc) is True

    def test_model_not_supported_keyword(self):
        exc = _openai_api_error(400, "model_not_supported for this provider.")
        assert _is_model_incompatible_error(exc) is True

    def test_does_not_support_this_model(self):
        exc = _openai_api_error(400, "This provider does not support this model.")
        assert _is_model_incompatible_error(exc) is True

    def test_unsupported_model_keyword(self):
        exc = _openai_api_error(400, "Unsupported model for current tier.")
        assert _is_model_incompatible_error(exc) is True

    # ---- Exclusions ----

    def test_model_not_found_400_excluded(self):
        """400 'invalid model ID' goes to _is_model_not_found_error."""
        exc = _openai_api_error(400, "is not a valid model ID")
        assert _is_model_incompatible_error(exc) is False
        assert _is_model_not_found_error(exc) is True

    def test_model_does_not_exist_excluded(self):
        """400 'model does not exist' goes to _is_model_not_found_error."""
        exc = _openai_api_error(400, "model does not exist in our configuration")
        assert _is_model_incompatible_error(exc) is False
        assert _is_model_not_found_error(exc) is True

    def test_billing_free_tier_400_excluded(self):
        """400 with billing/free-tier language excluded from incompatible.

        _is_payment_error is status-gated to {402,403,404,429,None} — it won't
        claim 400s. The _is_model_incompatible_error has its own billing
        keyword exclusion to prevent overlap."""
        exc = _openai_api_error(
            400, "This model is not available on the free tier"
        )
        assert _is_model_incompatible_error(exc) is False
        # _is_payment_error doesn't claim 400 status codes — that's by design

    def test_billing_credits_400_excluded(self):
        """400 with credits/quota language excluded from incompatible."""
        exc = _openai_api_error(400, "insufficient credits for this model")
        assert _is_model_incompatible_error(exc) is False
        # _is_payment_error doesn't claim 400 status codes — that's by design

    def test_rate_limit_not_incompatible(self):
        """429 is rate limit, not capability mismatch."""
        exc = _openai_api_error(429, "Rate limit exceeded")
        assert _is_model_incompatible_error(exc) is False
        assert _is_rate_limit_error(exc) is True

    def test_connection_error_not_incompatible(self):
        """Connection errors are separate."""
        exc = Exception("Connection refused")
        assert _is_model_incompatible_error(exc) is False
        assert _is_connection_error(exc) is True

    def test_status_not_400_or_none_returns_false(self):
        """Status 500, 503, 401 etc. are not capability mismatches."""
        for status in [500, 503, 401, 403, 404]:
            exc = _openai_api_error(status, "some error")
            assert _is_model_incompatible_error(exc) is False


class TestFallbackPath:
    """Integration: ensure incompatible errors trigger fallback chain."""

    def test_incompatible_error_bypasses_explicit_provider_gate(
        self,
    ):
        """A capability 400 traverses the explicit-provider capacity gate."""
        exc = _openai_api_error(
            400,
            "Error code: 400 - {'detail': \"The 'glm-5.2' model is not supported when using Codex with a ChatGPT account.\"}",
        )

        primary = MagicMock()
        primary.chat.completions.create.side_effect = exc
        fallback = MagicMock()
        fallback.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="fallback response"))]
        )
        order = MagicMock()
        order.attach_mock(primary.chat.completions.create, "primary")
        order.attach_mock(fallback.chat.completions.create, "fallback")

        with patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("openai-codex", "glm-5.2", None, None, None)), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary, "glm-5.2")), \
             patch("agent.auxiliary_client._try_configured_fallback_chain",
                   return_value=(fallback, "fallback-model", "configured")) as mock_chain, \
             patch("agent.auxiliary_client._try_main_agent_model_fallback") as mock_main:
            order.attach_mock(mock_chain, "configured")
            result = call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "fallback response"
        mock_chain.assert_called_once_with(
            "compression",
            "openai-codex",
            reason="model incompatible with route",
            failed_model="glm-5.2",
        )
        mock_main.assert_not_called()
        primary.chat.completions.create.assert_called_once()
        fallback.chat.completions.create.assert_called_once()
        assert [entry[0] for entry in order.mock_calls] == [
            "primary", "configured", "fallback"
        ]

    def test_incompatible_error_is_in_should_fallback_conditions(self):
        """An auto route reaches fallback discovery after earlier layers exhaust."""
        exc = _openai_api_error(
            400,
            "The 'glm-5.2' model is not supported when using Codex with a ChatGPT account.",
        )

        primary = MagicMock()
        primary.chat.completions.create.side_effect = exc
        fallback = MagicMock()
        fallback.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="auto fallback"))]
        )
        order = MagicMock()

        with patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("auto", "glm-5.2", None, None, None)), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary, "glm-5.2")), \
             patch("agent.auxiliary_client._try_configured_fallback_chain",
                   return_value=(None, None, "")) as mock_configured, \
             patch("agent.auxiliary_client._try_main_fallback_chain",
                   return_value=(None, None, "")) as mock_main, \
             patch("agent.auxiliary_client._try_payment_fallback",
                   return_value=(fallback, "fallback-model", "discovery")) as mock_discovery:
            order.attach_mock(mock_configured, "configured")
            order.attach_mock(mock_main, "main")
            order.attach_mock(mock_discovery, "discovery")
            result = call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "auto fallback"
        mock_configured.assert_called_once()
        mock_main.assert_called_once()
        mock_discovery.assert_called_once()
        assert [entry[0] for entry in order.mock_calls] == [
            "configured", "main", "discovery"
        ]
        fallback.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_incompatible_error_runs_auto_fallback_order(self):
        """The public async API exhausts configured/main before discovery."""
        exc = _openai_api_error(
            400,
            "The 'glm-5.2' model is not supported when using Codex with a ChatGPT account.",
        )
        primary = MagicMock()
        primary.chat.completions.create = AsyncMock(side_effect=exc)
        fallback = MagicMock()
        fallback.chat.completions.create = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content="async fallback"))]
            )
        )
        fallback_source = MagicMock()
        order = MagicMock()

        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", "glm-5.2", None, None, None),
        ), patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "glm-5.2"),
        ), patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(None, None, ""),
        ) as mock_configured, patch(
            "agent.auxiliary_client._try_main_fallback_chain",
            return_value=(None, None, ""),
        ) as mock_main, patch(
            "agent.auxiliary_client._try_payment_fallback",
            return_value=(fallback_source, "fallback-model", "discovery"),
        ) as mock_discovery, patch(
            "agent.auxiliary_client._to_async_client",
            return_value=(fallback, "fallback-model"),
        ) as mock_to_async:
            order.attach_mock(mock_configured, "configured")
            order.attach_mock(mock_main, "main")
            order.attach_mock(mock_discovery, "discovery")
            order.attach_mock(fallback.chat.completions.create, "fallback")
            result = await async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "async fallback"
        primary.chat.completions.create.assert_awaited_once()
        fallback.chat.completions.create.assert_awaited_once()
        mock_configured.assert_called_once_with(
            "compression",
            "auto",
            reason="model incompatible with route",
            failed_model="glm-5.2",
        )
        mock_main.assert_called_once_with(
            "compression", "auto", reason="model incompatible with route"
        )
        mock_discovery.assert_called_once_with(
            "auto", "compression", reason="model incompatible with route"
        )
        mock_to_async.assert_called_once_with(
            fallback_source, "fallback-model", is_vision=False
        )
        assert [
            entry[0]
            for entry in order.mock_calls
            if entry[0] in {"configured", "main", "discovery", "fallback"}
        ] == ["configured", "main", "discovery", "fallback"]


class TestIncompatibleVsNotFoundBoundary:
    """Ensure the boundary between incompatible and not-found is sharp."""

    def test_model_not_found_keywords_exclude_incompatible(self):
        for msg in [
            "model does not exist",
            "is not a valid model",
            "no such model",
            "model not found",
            "the model `x` does not exist",
            "unknown model",
        ]:
            exc = _openai_api_error(400, msg)
            assert _is_model_incompatible_error(exc) is False, f"Failed on: {msg}"

    def test_incompatible_keywords_exclude_not_found(self):
        for msg in [
            "is not supported when using",
            "model is not supported",
            "not supported with this",
            "not supported for this account",
            "model_not_supported",
            "does not support this model",
            "unsupported model",
        ]:
            exc = _openai_api_error(400, msg)
            assert _is_model_incompatible_error(exc) is True, f"Failed on: {msg}"
            assert _is_model_not_found_error(exc) is False, f"Not-found claimed: {msg}"
