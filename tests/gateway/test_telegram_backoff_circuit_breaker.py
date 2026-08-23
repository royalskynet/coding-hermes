"""Deterministic recovery tests for Telegram's fallback transport."""

import asyncio

import httpx
import pytest

import plugins.platforms.telegram.telegram_network as tnet


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, behavior, instances):
        self.behavior = behavior
        self.closed = False
        instances.append(self)

    async def handle_async_request(self, request):
        action = self.behavior.get(request.url.host, "ok")
        if callable(action):
            action = action()
        if action == "fail":
            raise httpx.ConnectError("offline")
        return httpx.Response(200, request=request)

    async def aclose(self):
        self.closed = True


def request():
    return httpx.Request("GET", "https://api.telegram.org/botTOKEN/getMe")


def build(monkeypatch, *, threshold=5, cooldown=60, maximum=60):
    clock = FakeClock()
    behavior = {
        "api.telegram.org": "fail",
        "149.154.167.220": "fail",
    }
    instances = []
    monkeypatch.setattr(
        tnet.httpx,
        "AsyncHTTPTransport",
        lambda **kwargs: FakeTransport(behavior, instances),
    )
    transport = tnet.TelegramFallbackTransport(
        ["149.154.167.220"],
        backoff_initial=1,
        backoff_multiplier=2,
        backoff_maximum=maximum,
        breaker_failure_threshold=threshold,
        breaker_cooldown=cooldown,
        clock=clock,
        sleep=clock.sleep,
    )
    return transport, behavior, instances, clock


async def fail(transport):
    with pytest.raises(httpx.ConnectError, match="offline"):
        await transport.handle_async_request(request())


@pytest.mark.asyncio
async def test_all_path_failures_release_fallback_transports_between_attempts(monkeypatch):
    transport, _, instances, _ = build(monkeypatch, threshold=100)
    for _ in range(4):
        await fail(transport)
        assert transport._fallbacks == {}
        assert sum(not item.closed for item in instances) == 1  # primary only


@pytest.mark.asyncio
async def test_backoff_grows_and_caps(monkeypatch):
    transport, _, _, clock = build(monkeypatch, threshold=100)
    for _ in range(8):
        await fail(transport)
    assert clock.sleeps == [1, 2, 4, 8, 16, 32, 60]
    assert transport.health_backoff_seconds == 60


@pytest.mark.asyncio
async def test_open_breaker_blocks_without_constructing_transport(monkeypatch):
    transport, _, instances, _ = build(monkeypatch, threshold=2)
    await fail(transport)
    await fail(transport)
    assert transport.health_breaker_state == "open"
    count = len(instances)

    with pytest.raises(tnet.TelegramCircuitOpenError):
        await transport.handle_async_request(request())

    assert len(instances) == count  # negative assertion: clearly-open request did no I/O


@pytest.mark.asyncio
async def test_cooldown_allows_only_one_half_open_probe(monkeypatch):
    transport, behavior, instances, clock = build(monkeypatch, threshold=1)
    await fail(transport)
    clock.now += 60
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_primary(_request):
        entered.set()
        await release.wait()
        raise httpx.ConnectError("offline")

    transport._primary.handle_async_request = blocked_primary
    probe = asyncio.create_task(transport.handle_async_request(request()))
    await entered.wait()
    assert transport.health_breaker_state == "half_open"
    count = len(instances)
    with pytest.raises(tnet.TelegramCircuitOpenError):
        await transport.handle_async_request(request())
    assert len(instances) == count
    release.set()
    with pytest.raises(httpx.ConnectError):
        await probe


@pytest.mark.asyncio
async def test_successful_half_open_probe_resets_and_normal_traffic_resumes(monkeypatch):
    transport, behavior, _, clock = build(monkeypatch, threshold=1)
    await fail(transport)
    clock.now += 60
    behavior["api.telegram.org"] = "ok"

    assert (await transport.handle_async_request(request())).status_code == 200
    assert transport.health_breaker_state == "closed"
    assert transport.consecutive_fallback_failures == 0
    assert transport.health_backoff_seconds == 0
    assert (await transport.handle_async_request(request())).status_code == 200


@pytest.mark.asyncio
async def test_bookkeeping_failure_fails_open(monkeypatch):
    transport, behavior, instances, _ = build(monkeypatch, threshold=1)
    behavior["api.telegram.org"] = "ok"
    monkeypatch.setattr(transport, "_admit_request", lambda: (_ for _ in ()).throw(RuntimeError("state broken")))

    assert (await transport.handle_async_request(request())).status_code == 200
    assert len(instances) == 1


def test_health_properties_have_stable_defaults(monkeypatch):
    transport, _, _, _ = build(monkeypatch)
    assert transport.consecutive_fallback_failures == 0
    assert transport.health_breaker_state == "closed"
    assert transport.health_backoff_seconds == 0


def test_config_overrides_are_translated_and_invalid_values_fail_open():
    kwargs = tnet.telegram_recovery_kwargs({
        "telegram_backoff_initial_seconds": 3,
        "telegram_backoff_multiplier": "4",
        "telegram_backoff_maximum_seconds": 30,
        "telegram_breaker_failure_threshold": 7,
        "telegram_breaker_cooldown_seconds": "bad",
    })

    assert kwargs == {
        "backoff_initial": 3.0,
        "backoff_multiplier": 4.0,
        "backoff_maximum": 30.0,
        "breaker_failure_threshold": 7,
        "breaker_cooldown": tnet.DEFAULT_BREAKER_COOLDOWN,
    }
