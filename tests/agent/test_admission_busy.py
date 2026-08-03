"""Tests for OmniRoute gateway admission-busy 503 detection and retry logic.

These tests formalize the 17-condition regression suite originally run
offline. They ensure admission-busy 503 is:
- Detected correctly across SDK/urllib3 error shapes
- NEVER misclassified as payment/rate-limit/connection error
- Retry-After header parsed with clamping and defaults
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import (
    _is_admission_busy_error,
    _admission_retry_after,
    _is_payment_error,
    _is_rate_limit_error,
    _is_connection_error,
    _is_model_not_found_error,
    async_call_llm,
    call_llm,
)


# ---------------------------------------------------------------------------
# Helpers to construct error shapes seen in the wild
# ---------------------------------------------------------------------------

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


def _urllib3_error(message: str, status: int | None = None):
    """Mimic urllib3 / httpx exceptions carrying status in various ways."""
    exc = Exception(message)
    if status is not None:
        exc.status_code = status
    return exc


def _successful_response(content: str = "recovered"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _admission_error(retry_after: str):
    return _openai_api_error(
        503,
        "Structurally heavy chat request capacity is busy; retry shortly.",
        headers={"Retry-After": retry_after},
    )


# ---------------------------------------------------------------------------
# _is_admission_busy_error
# ---------------------------------------------------------------------------

class TestIsAdmissionBusyError:
    """Three canonical 503 shapes that MUST be detected."""

    def test_sdk_status_code_503_chat_admission_busy(self):
        exc = _openai_api_error(
            503, "Structurally heavy chat request capacity is busy; retry shortly."
        )
        assert _is_admission_busy_error(exc) is True

    def test_sdk_status_code_503_code_field(self):
        exc = _openai_api_error(
            503, '{"error": {"message": "Structurally heavy chat request capacity is busy; retry shortly.", "type": "server_error", "code": "chat_admission_busy", "reason": "structure_limit"}}'
        )
        assert _is_admission_busy_error(exc) is True

    def test_sdk_no_status_code_chat_admission_busy(self):
        """SDK sometimes puts status only on response, not exception."""
        exc = _openai_no_status(
            "Structurally heavy chat request capacity is busy; retry shortly.",
            headers={"Retry-After": "1"},
        )
        assert _is_admission_busy_error(exc) is True

    def test_urllib3_shape_chat_admission_busy(self):
        exc = _urllib3_error(
            "HTTP 503: Structurally heavy chat request capacity is busy; retry shortly.",
            status=503,
        )
        assert _is_admission_busy_error(exc) is True

    def test_second_variant_chat_admission_capacity_unavailable(self):
        exc = _openai_api_error(
            503, "Chat admission capacity is temporarily unavailable; retry shortly."
        )
        assert _is_admission_busy_error(exc) is True

    # ---- Non-admission 503 must NOT match ----

    def test_503_generic_server_error_not_admission(self):
        exc = _openai_api_error(503, "Internal server error, please try again later.")
        assert _is_admission_busy_error(exc) is False

    def test_503_model_overloaded_not_admission(self):
        exc = _openai_api_error(503, "Model overloaded, try again in a moment.")
        assert _is_admission_busy_error(exc) is False

    def test_504_gateway_timeout_not_admission(self):
        exc = _openai_api_error(504, "Gateway timeout")
        assert _is_admission_busy_error(exc) is False

    def test_500_not_admission(self):
        exc = _openai_api_error(500, "Internal server error")
        assert _is_admission_busy_error(exc) is False

    def test_400_not_admission(self):
        exc = _openai_api_error(400, "Bad request")
        assert _is_admission_busy_error(exc) is False

    def test_429_not_admission(self):
        exc = _openai_api_error(429, "Rate limit exceeded")
        assert _is_admission_busy_error(exc) is False


# ---------------------------------------------------------------------------
# Critical: admission 503 must NEVER be misclassified as other error types
# ---------------------------------------------------------------------------

class TestAdmissionNeverMisclassified:
    """Regression guard: admission 503 must not leak into fallback logic."""

    def test_admission_not_payment_error(self):
        exc = _openai_api_error(
            503, "Structurally heavy chat request capacity is busy; retry shortly."
        )
        assert _is_payment_error(exc) is False

    def test_admission_not_rate_limit_error(self):
        exc = _openai_api_error(
            503, "Structurally heavy chat request capacity is busy; retry shortly."
        )
        assert _is_rate_limit_error(exc) is False

    def test_admission_not_connection_error(self):
        exc = _openai_api_error(
            503, "Structurally heavy chat request capacity is busy; retry shortly."
        )
        assert _is_connection_error(exc) is False

    def test_admission_not_model_not_found(self):
        exc = _openai_api_error(
            503, "Structurally heavy chat request capacity is busy; retry shortly."
        )
        assert _is_model_not_found_error(exc) is False


# ---------------------------------------------------------------------------
# Other error classifications unchanged (regression protection)
# ---------------------------------------------------------------------------

class TestOtherClassificationsUnchanged:
    """Ensure existing error detectors still work."""

    def test_402_is_payment(self):
        exc = _openai_api_error(402, "Payment required: insufficient credits")
        assert _is_payment_error(exc) is True

    def test_429_is_rate_limit(self):
        exc = _openai_api_error(429, "Rate limit exceeded")
        assert _is_rate_limit_error(exc) is True

    def test_connection_refused_is_connection_error(self):
        exc = _urllib3_error("Connection refused", status=None)
        assert _is_connection_error(exc) is True

    def test_400_bad_model_not_found(self):
        exc = _openai_api_error(400, "is not a valid model ID")
        assert _is_model_not_found_error(exc) is True

    def test_400_bad_request_not_model_not_found(self):
        exc = _openai_api_error(400, "Bad request: invalid parameter")
        assert _is_model_not_found_error(exc) is False


# ---------------------------------------------------------------------------
# _admission_retry_after header parsing
# ---------------------------------------------------------------------------

class TestAdmissionRetryAfter:
    """Retry-After parsing with clamping and defaults."""

    def test_retry_after_present_returns_float(self):
        exc = _openai_no_status("busy", headers={"Retry-After": "2"})
        assert _admission_retry_after(exc) == 2.0

    def test_retry_after_case_sensitive_header(self):
        """Current implementation uses exact 'Retry-After' key; lowercase not found."""
        exc = _openai_no_status("busy", headers={"retry-after": "3"})
        assert _admission_retry_after(exc) == 1.0  # falls back to default

    def test_retry_after_missing_uses_default(self):
        exc = _openai_no_status("busy", headers={})
        assert _admission_retry_after(exc) == 1.0

    def test_retry_after_garbage_uses_default(self):
        exc = _openai_no_status("busy", headers={"Retry-After": "not-a-number"})
        assert _admission_retry_after(exc) == 1.0

    def test_retry_after_9999_clamped_to_30(self):
        exc = _openai_no_status("busy", headers={"Retry-After": "9999"})
        assert _admission_retry_after(exc) == 30.0

    def test_retry_after_negative_clamped_to_0(self):
        exc = _openai_no_status("busy", headers={"Retry-After": "-5"})
        assert _admission_retry_after(exc) == 0.0

    def test_retry_after_zero_allowed(self):
        exc = _openai_no_status("busy", headers={"Retry-After": "0"})
        assert _admission_retry_after(exc) == 0.0

    def test_retry_after_float_parsed(self):
        exc = _openai_no_status("busy", headers={"Retry-After": "1.5"})
        assert _admission_retry_after(exc) == 1.5

    def test_retry_after_whitespace_stripped(self):
        exc = _openai_no_status("busy", headers={"Retry-After": "  2  "})
        assert _admission_retry_after(exc) == 2.0

    def test_no_response_attribute_uses_default(self):
        exc = Exception("busy")
        exc.status_code = 503
        assert _admission_retry_after(exc) == 1.0


class TestAdmissionRetryPublicCallPath:
    """Lock admission handling at the public API and Relay execution seam."""

    def test_sync_admission_precedes_generic_transient_retry(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _admission_error("2.5"),
            _successful_response(),
        ]
        sleeps = []

        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("custom", "model", "http://gateway.test", "key", None),
        ), patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "model"),
        ), patch("agent.auxiliary_client.time.sleep", side_effect=sleeps.append):
            result = call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "recovered"
        assert sleeps == [2.5]

    def test_sync_admission_retry_uses_relay_completion(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _admission_error("0"),
            _successful_response(),
        ]
        relay_attempts = []

        def execute_current(request, callback, **_kwargs):
            relay_attempts.append(request)
            return callback(request)

        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("custom", "model", "http://gateway.test", "key", None),
        ), patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "model"),
        ), patch(
            "agent.auxiliary_client._transient_retry_count", return_value=0,
        ), patch("agent.auxiliary_client.time.sleep"), patch(
            "agent.relay_llm.execute_current", side_effect=execute_current,
        ):
            result = call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "recovered"
        assert len(relay_attempts) == 2

    @pytest.mark.asyncio
    async def test_async_admission_precedes_transient_and_uses_relay_completion(self):
        client = MagicMock()
        client.chat.completions.create = MagicMock()
        responses = iter([
            _admission_error("2"),
            _admission_error("3"),
            _successful_response(),
        ])

        async def create(**_kwargs):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        client.chat.completions.create.side_effect = create
        relay_attempts = []
        sleeps = []

        async def execute_current_async(request, callback, **_kwargs):
            relay_attempts.append(request)
            return await callback(request)

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("custom", "model", "http://gateway.test", "key", None),
        ), patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "model"),
        ), patch(
            "agent.auxiliary_client.asyncio.sleep", side_effect=fake_sleep,
        ), patch(
            "agent.relay_llm.execute_current_async",
            side_effect=execute_current_async,
        ):
            result = await async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        assert result.choices[0].message.content == "recovered"
        assert sleeps == [2.0, 3.0]
        assert len(relay_attempts) == 3
