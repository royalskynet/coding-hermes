"""Regression tests for _is_invalid_aux_response_error and _try_payment_fallback edge cases."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.auxiliary_client import (
    _is_invalid_aux_response_error,
    _try_payment_fallback,
    async_call_llm,
    call_llm,
)


# ---------------------------------------------------------------------------
# _is_invalid_aux_response_error
# ---------------------------------------------------------------------------

def test_is_invalid_aux_response_error_true():
    """Must be RuntimeError with the exact three substrings."""
    exc = RuntimeError(
        "auxiliary llm returned invalid response missing choices[0].message"
    )
    assert _is_invalid_aux_response_error(exc) is True


def test_is_invalid_aux_response_error_false_on_non_runtime():
    exc = Exception(
        "auxiliary llm returned invalid response missing choices[0].message"
    )
    assert _is_invalid_aux_response_error(exc) is False


def test_is_invalid_aux_response_error_false_missing_any_substring():
    base = "auxiliary llm returned invalid response"
    for missing in ("auxiliary ", "llm returned invalid response", "choices[0].message"):
        msg = base.replace(missing, "")
        exc = RuntimeError(msg)
        assert _is_invalid_aux_response_error(exc) is False, f"Failed on missing: {missing}"


def test_is_invalid_aux_response_error_case_insensitive():
    exc = RuntimeError(
        "AUXILIARY LLM RETURNED INVALID RESPONSE MISSING CHOICES[0].MESSAGE"
    )
    assert _is_invalid_aux_response_error(exc) is True


# ---------------------------------------------------------------------------
# _try_payment_fallback edge cases
# ---------------------------------------------------------------------------

class TestTryPaymentFallback:
    """Edge cases for the payment/connection fallback chain.

    These tests patch _get_provider_chain to return synthetic entries
    so we can test the skip/resolve/exhaust logic without relying on
    real credentials.
    """

    def _make_try_fn(self, result):
        """Return a callable that returns the given (client, model) or raises."""
        def try_fn():
            if isinstance(result, Exception):
                raise result
            return result
        return try_fn

    def test_skips_unhealthy_provider(self):
        """If a chain entry is unhealthy, it should be skipped."""
        mock_chain = [
            ("provider-a", self._make_try_fn(Exception("should not be called"))),
            ("provider-b", self._make_try_fn((MagicMock(), "model-b"))),
            ("provider-c", self._make_try_fn((MagicMock(), "model-c"))),
        ]

        with patch("agent.auxiliary_client._is_provider_unhealthy") as mock_unhealthy, \
             patch("agent.auxiliary_client._get_provider_chain", return_value=mock_chain):

            mock_unhealthy.side_effect = [True, False, False]

            client, model, label = _try_payment_fallback("failed-provider", task="aux")

            assert label == "provider-b"
            assert model == "model-b"
            assert client is not None
            # unhealthy one should not have been called
            assert mock_unhealthy.call_count == 2  # first unhealthy, second checked

    def test_resolve_failure_is_skipped(self):
        """try_fn returning (None, None) skips to next candidate.

        The real chain's _try_openrouter / _try_nous functions handle their own
        exceptions and return (None, None) on failure. So 'resolve failure'
        manifests as a None client, not as a raised exception from try_fn."""
        mock_chain = [
            ("provider-a", self._make_try_fn((None, None))),
            ("provider-b", self._make_try_fn((MagicMock(), "model-b"))),
            ("provider-c", self._make_try_fn((MagicMock(), "model-c"))),
        ]

        with patch("agent.auxiliary_client._is_provider_unhealthy", return_value=False), \
             patch("agent.auxiliary_client._get_provider_chain", return_value=mock_chain):

            client, model, label = _try_payment_fallback("failed-provider", task="aux")

            assert label == "provider-b"
            assert model == "model-b"
            assert client is not None

    def test_none_client_is_skipped(self):
        """When try_fn returns (None, None), skip and try next."""
        mock_chain = [
            ("provider-a", self._make_try_fn((None, None))),
            ("provider-b", self._make_try_fn((MagicMock(), "model-b"))),
            ("provider-c", self._make_try_fn((MagicMock(), "model-c"))),
        ]

        with patch("agent.auxiliary_client._is_provider_unhealthy", return_value=False), \
             patch("agent.auxiliary_client._get_provider_chain", return_value=mock_chain):

            client, model, status = _try_payment_fallback("failed-provider", task="aux")

            assert status == "provider-b"
            assert model == "model-b"
            assert client is not None

    def test_exhausted_chain_returns_none(self):
        """When every entry returns (None, None), returns (None, None, "")."""
        mock_chain = [
            ("provider-a", self._make_try_fn((None, None))),
            ("provider-b", self._make_try_fn((None, None))),
        ]

        with patch("agent.auxiliary_client._is_provider_unhealthy", return_value=False), \
             patch("agent.auxiliary_client._get_provider_chain", return_value=mock_chain):

            client, model, status = _try_payment_fallback("failed-provider", task="aux")

            assert (client, model, status) == (None, None, "")

    def test_all_unhealthy_is_exhausted(self):
        """When all providers are unhealthy, returns none."""
        mock_chain = [
            ("provider-a", self._make_try_fn(Exception("should never be called"))),
            ("provider-b", self._make_try_fn(Exception("should never be called"))),
        ]

        with patch("agent.auxiliary_client._is_provider_unhealthy", return_value=True), \
             patch("agent.auxiliary_client._get_provider_chain", return_value=mock_chain):

            client, model, status = _try_payment_fallback("failed-provider", task="aux")

            assert (client, model, status) == (None, None, "")

    def test_calls_try_fn_in_order(self):
        """It should call try_fn in chain order, stopping on first success."""
        tried = []

        def make_try_fn(name, result):
            def fn():
                tried.append(name)
                return result
            return fn

        mock_chain = [
            ("a", make_try_fn("a", (None, None))),
            ("b", make_try_fn("b", (MagicMock(), "model-b"))),
            ("c", make_try_fn("c", (MagicMock(), "model-c"))),
        ]

        with patch("agent.auxiliary_client._is_provider_unhealthy", return_value=False), \
             patch("agent.auxiliary_client._get_provider_chain", return_value=mock_chain):

            client, model, status = _try_payment_fallback("failed", task="aux")

            assert tried == ["a", "b"]
            assert status == "b"


# ---------------------------------------------------------------------------
# Integration: invalid aux response is in should_fallback conditions
# ---------------------------------------------------------------------------

class TestInvalidAuxResponseInFallback:
    """Ensure _is_invalid_aux_response_error triggers fallback."""

    def test_invalid_aux_response_in_should_fallback(self):
        """An invalid response runs the production auto fallback chain."""
        exc = RuntimeError(
            "auxiliary llm returned invalid response missing choices[0].message"
        )
        primary = MagicMock()
        primary.chat.completions.create.return_value = MagicMock(choices=[])
        fallback = MagicMock()
        fallback.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="recovered"))]
        )
        order = MagicMock()

        with patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("auto", "primary-model", None, None, None)), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary, "primary-model")), \
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

        assert _is_invalid_aux_response_error(exc) is True
        assert result.choices[0].message.content == "recovered"
        mock_configured.assert_called_once()
        mock_main.assert_called_once()
        mock_discovery.assert_called_once()
        assert [entry[0] for entry in order.mock_calls] == [
            "configured", "main", "discovery"
        ]
        fallback.chat.completions.create.assert_called_once()

    def test_invalid_aux_response_is_capacity_error(self):
        """Invalid output bypasses the explicit-provider capacity gate."""
        primary = MagicMock()
        primary.chat.completions.create.return_value = MagicMock(choices=[])
        fallback = MagicMock()
        fallback.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="explicit recovered"))]
        )
        order = MagicMock()
        order.attach_mock(primary.chat.completions.create, "primary")
        order.attach_mock(fallback.chat.completions.create, "fallback")

        with patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("explicit-provider", "primary-model", None, None, None)), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary, "primary-model")), \
             patch("agent.auxiliary_client._try_configured_fallback_chain",
                   return_value=(fallback, "fallback-model", "configured")) as mock_configured, \
             patch("agent.auxiliary_client._try_main_agent_model_fallback") as mock_main:
            order.attach_mock(mock_configured, "configured")
            result = call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "explicit recovered"
        mock_configured.assert_called_once_with(
            "compression",
            "explicit-provider",
            reason="invalid provider response",
            failed_model="primary-model",
        )
        mock_main.assert_not_called()
        primary.chat.completions.create.assert_called_once()
        fallback.chat.completions.create.assert_called_once()
        assert [entry[0] for entry in order.mock_calls
                if entry[0] in {"primary", "configured", "fallback"}] == [
            "primary", "configured", "fallback"
        ]

    @pytest.mark.asyncio
    async def test_async_invalid_response_bypasses_explicit_provider_gate(self):
        """The public async API treats malformed output as model-scoped capacity."""
        primary = MagicMock()
        primary.chat.completions.create = AsyncMock(
            return_value=MagicMock(choices=[])
        )
        fallback = MagicMock()
        fallback.chat.completions.create = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content="async recovered"))]
            )
        )
        fallback_source = MagicMock()
        order = MagicMock()
        order.attach_mock(primary.chat.completions.create, "primary")

        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "explicit-provider", "primary-model", None, None, None
            ),
        ), patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "primary-model"),
        ), patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(fallback_source, "fallback-model", "configured"),
        ) as mock_configured, patch(
            "agent.auxiliary_client._try_main_agent_model_fallback"
        ) as mock_main, patch(
            "agent.auxiliary_client._to_async_client",
            return_value=(fallback, "fallback-model"),
        ) as mock_to_async:
            order.attach_mock(mock_configured, "configured")
            order.attach_mock(fallback.chat.completions.create, "fallback")
            result = await async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "async recovered"
        primary.chat.completions.create.assert_awaited_once()
        fallback.chat.completions.create.assert_awaited_once()
        mock_configured.assert_called_once_with(
            "compression",
            "explicit-provider",
            reason="invalid provider response",
            failed_model="primary-model",
        )
        mock_main.assert_not_called()
        mock_to_async.assert_called_once_with(
            fallback_source, "fallback-model", is_vision=False
        )
        assert [
            entry[0]
            for entry in order.mock_calls
            if entry[0] in {"primary", "configured", "fallback"}
        ] == ["primary", "configured", "fallback"]
