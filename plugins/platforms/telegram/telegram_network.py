"""Telegram-specific network helpers.

Provides a hostname-preserving fallback transport for networks where
api.telegram.org resolves to an endpoint that is unreachable from the current
host. The transport keeps the logical request host and TLS SNI as
api.telegram.org while retrying the TCP connection against one or more fallback
IPv4 addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API_HOST = "api.telegram.org"

DEFAULT_BACKOFF_INITIAL = 1.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BACKOFF_MAXIMUM = 60.0
DEFAULT_BREAKER_FAILURE_THRESHOLD = 5
DEFAULT_BREAKER_COOLDOWN = 60.0


def telegram_recovery_kwargs(config: Mapping[str, Any] | None) -> dict[str, float | int]:
    """Translate gateway health config into safe transport constructor args.

    Invalid or unavailable config fails open to built-in defaults so a typo in
    health telemetry cannot prevent Telegram from connecting.
    """
    source = config if isinstance(config, Mapping) else {}

    def _number(key: str, default: float) -> float:
        try:
            return float(source.get(key, default))
        except (TypeError, ValueError):
            return default

    def _integer(key: str, default: int) -> int:
        try:
            return int(source.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "backoff_initial": _number(
            "telegram_backoff_initial_seconds", DEFAULT_BACKOFF_INITIAL),
        "backoff_multiplier": _number(
            "telegram_backoff_multiplier", DEFAULT_BACKOFF_MULTIPLIER),
        "backoff_maximum": _number(
            "telegram_backoff_maximum_seconds", DEFAULT_BACKOFF_MAXIMUM),
        "breaker_failure_threshold": _integer(
            "telegram_breaker_failure_threshold", DEFAULT_BREAKER_FAILURE_THRESHOLD),
        "breaker_cooldown": _number(
            "telegram_breaker_cooldown_seconds", DEFAULT_BREAKER_COOLDOWN),
    }


class TelegramCircuitOpenError(httpx.ConnectError):
    """Raised without network I/O while Telegram's recovery circuit is open."""

# DNS-over-HTTPS providers used to discover Telegram API IPs that may differ
# from the (potentially unreachable) IP returned by the local system resolver.
_DOH_TIMEOUT = 4.0  # seconds — bounded so connect() isn't noticeably delayed

_DOH_PROVIDERS: list[dict] = [
    {
        "url": "https://dns.google/resolve",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {},
    },
    {
        "url": "https://cloudflare-dns.com/dns-query",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {"Accept": "application/dns-json"},
    },
]

# Last-resort IPs when DoH is also blocked.  These are stable Telegram Bot API
# endpoints in the 149.154.160.0/20 block (same seed used by OpenClaw).
_SEED_FALLBACK_IPS: list[str] = ["149.154.166.110", "149.154.167.220"]


def _resolve_proxy_url(target_hosts=None) -> str | None:
    # Delegate to shared implementation (env vars + macOS system proxy detection)
    from gateway.platforms.base import resolve_proxy_url
    return resolve_proxy_url("TELEGRAM_PROXY", target_hosts=target_hosts)


class TelegramFallbackTransport(httpx.AsyncBaseTransport):
    """Retry Telegram Bot API requests via fallback IPs while preserving TLS/SNI.

    Requests continue to target https://api.telegram.org/... logically, but on
    connect failures the underlying TCP connection is retried against a known
    reachable IP. This is effectively the programmatic equivalent of
    ``curl --resolve api.telegram.org:443:<ip>``.
    """

    # Bound every pool. httpx defaults to 100 connections per pool, so a wedged
    # endpoint plus the seed IPs can outgrow the process file-descriptor limit
    # on its own (#63311).
    _POOL_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)

    def __init__(
        self,
        fallback_ips: Iterable[str],
        *,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        backoff_maximum: float = DEFAULT_BACKOFF_MAXIMUM,
        breaker_failure_threshold: int = DEFAULT_BREAKER_FAILURE_THRESHOLD,
        breaker_cooldown: float = DEFAULT_BREAKER_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        **transport_kwargs,
    ):
        self._fallback_ips = list(dict.fromkeys(_normalize_fallback_ips(fallback_ips)))
        proxy_url = _resolve_proxy_url(target_hosts=[_TELEGRAM_API_HOST, *self._fallback_ips])
        if proxy_url and "proxy" not in transport_kwargs:
            transport_kwargs["proxy"] = proxy_url
        transport_kwargs.setdefault("limits", self._POOL_LIMITS)
        self._transport_kwargs = transport_kwargs
        self._primary = httpx.AsyncHTTPTransport(**transport_kwargs)
        # Built on demand and discarded on failure — see _reset_fallback.
        self._fallbacks: dict[str, httpx.AsyncHTTPTransport] = {}
        self._fallback_lock = asyncio.Lock()
        self._sticky_ip: Optional[str] = None
        self._sticky_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._recovery_lock = asyncio.Lock()
        self._consecutive_fallback_failures = 0
        self._breaker_state = "closed"
        self._breaker_opened_at: Optional[float] = None
        self._backoff_seconds = 0.0
        self._next_attempt_at = 0.0
        self._backoff_initial = max(0.0, float(backoff_initial))
        self._backoff_multiplier = max(1.0, float(backoff_multiplier))
        self._backoff_maximum = max(self._backoff_initial, float(backoff_maximum))
        self._breaker_failure_threshold = max(1, int(breaker_failure_threshold))
        self._breaker_cooldown = max(0.0, float(breaker_cooldown))
        self._clock = clock
        self._sleep = sleep

    @property
    def consecutive_fallback_failures(self) -> int:
        """Consecutive requests for which primary and every fallback failed."""
        with self._state_lock:
            return self._consecutive_fallback_failures

    @property
    def health_breaker_state(self) -> str:
        with self._state_lock:
            return self._breaker_state

    @property
    def health_backoff_seconds(self) -> float:
        with self._state_lock:
            return self._backoff_seconds

    def _admit_request(self) -> float:
        """Reserve a half-open probe or return remaining closed-state backoff."""
        now = self._clock()
        with self._state_lock:
            if self._breaker_state == "open":
                opened_at = self._breaker_opened_at
                if opened_at is None or now - opened_at < self._breaker_cooldown:
                    raise TelegramCircuitOpenError("Telegram recovery circuit is open")
                self._breaker_state = "half_open"
                return 0.0
            if self._breaker_state == "half_open":
                raise TelegramCircuitOpenError("Telegram half-open probe already in progress")
            return max(0.0, self._next_attempt_at - now)

    async def _wait_for_recovery_window(self, delay: float) -> None:
        if delay <= 0:
            return
        async with self._recovery_lock:
            # Another waiter may have completed recovery while this task queued.
            remaining = self._admit_request()
            if remaining > 0:
                await self._sleep(remaining)

    def _record_success(self) -> None:
        with self._state_lock:
            self._consecutive_fallback_failures = 0
            self._breaker_state = "closed"
            self._breaker_opened_at = None
            self._backoff_seconds = 0.0
            self._next_attempt_at = 0.0

    def _record_all_path_failure(self) -> None:
        now = self._clock()
        with self._state_lock:
            self._consecutive_fallback_failures += 1
            next_backoff = (
                self._backoff_initial
                if self._backoff_seconds <= 0
                else self._backoff_seconds * self._backoff_multiplier
            )
            self._backoff_seconds = min(self._backoff_maximum, next_backoff)
            self._next_attempt_at = now + self._backoff_seconds
            if self._consecutive_fallback_failures >= self._breaker_failure_threshold:
                self._breaker_state = "open"
                self._breaker_opened_at = now

    async def _get_fallback(self, ip: str) -> httpx.AsyncHTTPTransport:
        async with self._fallback_lock:
            transport = self._fallbacks.get(ip)
            if transport is None:
                transport = httpx.AsyncHTTPTransport(**self._transport_kwargs)
                self._fallbacks[ip] = transport
            return transport

    async def _reset_fallback(self, ip: str) -> None:
        """Discard a failed fallback pool so its dead sockets are released.

        A connect that reaches ESTABLISHED and is then closed by the peer leaves
        its socket in CLOSE_WAIT inside the pool. Retaining the poisoned pool
        leaks one descriptor per retry until the process hits its file limit and
        can no longer accept connections or resolve DNS (#63311).
        """
        async with self._fallback_lock:
            transport = self._fallbacks.pop(ip, None)
        if transport is None:
            return
        try:
            await transport.aclose()
        except Exception as exc:  # closing a broken pool must never mask the real error
            logger.debug("[Telegram] Error closing fallback transport %s: %s", ip, exc)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != _TELEGRAM_API_HOST or not self._fallback_ips:
            return await self._primary.handle_async_request(request)

        # Breaker bookkeeping is advisory: any internal error fails open and
        # lets the real Telegram request proceed rather than wedging the bot.
        try:
            delay = self._admit_request()
            await self._wait_for_recovery_window(delay)
        except TelegramCircuitOpenError:
            raise
        except Exception as exc:
            logger.warning("[Telegram] Recovery state unavailable; failing open: %s", exc)

        sticky_ip = self._sticky_ip
        attempt_order: list[Optional[str]] = [sticky_ip] if sticky_ip else [None]
        if sticky_ip:
            attempt_order.append(None)  # retry primary DNS after sticky failure
        for ip in self._fallback_ips:
            if ip != sticky_ip:
                attempt_order.append(ip)

        last_error: Exception | None = None
        for ip in attempt_order:
            candidate = request if ip is None else _rewrite_request_for_ip(request, ip)
            transport = self._primary if ip is None else await self._get_fallback(ip)
            try:
                response = await transport.handle_async_request(candidate)
                try:
                    self._record_success()
                except Exception as exc:
                    logger.warning("[Telegram] Could not reset recovery state: %s", exc)
                if ip is not None and self._sticky_ip != ip:
                    async with self._sticky_lock:
                        if self._sticky_ip != ip:
                            self._sticky_ip = ip
                            logger.warning(
                                "[Telegram] Primary api.telegram.org path unreachable; using sticky fallback IP %s",
                                ip,
                            )
                return response
            except Exception as exc:
                last_error = exc
                # A peer can accept TCP and then close during TLS/HTTP.  httpx
                # surfaces that as ReadError/ProtocolError rather than a
                # ConnectError, but retaining its fallback pool leaves the
                # peer-closed socket in CLOSE_WAIT across later reconnects.
                # Discard any failed fallback pool before deciding whether the
                # error itself is retryable.
                if ip is not None:
                    await self._reset_fallback(ip)
                if not _is_retryable_connect_error(exc):
                    raise
                if ip is not None and ip == self._sticky_ip:
                    async with self._sticky_lock:
                        if self._sticky_ip == ip:
                            self._sticky_ip = None
                            logger.warning(
                                "[Telegram] Sticky fallback IP %s failed; resetting to primary DNS path",
                                ip,
                            )
                if ip is None:
                    logger.warning(
                        "[Telegram] Primary api.telegram.org connection failed (%s); trying fallback IPs %s",
                        exc,
                        ", ".join(self._fallback_ips),
                    )
                    continue
                logger.warning("[Telegram] Fallback IP %s failed: %s", ip, exc)
                continue

        if last_error is None:
            raise RuntimeError("All Telegram fallback IPs exhausted but no error was recorded")
        try:
            self._record_all_path_failure()
        except Exception as exc:
            logger.warning("[Telegram] Could not record recovery failure; failing open: %s", exc)
        raise last_error

    async def aclose(self) -> None:
        await self._primary.aclose()
        async with self._fallback_lock:
            transports = list(self._fallbacks.values())
            self._fallbacks.clear()
        for transport in transports:
            await transport.aclose()


def _normalize_fallback_ips(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        raw = str(value).strip()
        if not raw:
            continue
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            logger.warning("Ignoring invalid Telegram fallback IP: %r", raw)
            continue
        if addr.version != 4:
            logger.warning("Ignoring non-IPv4 Telegram fallback IP: %s", raw)
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            logger.warning("Ignoring private/internal Telegram fallback IP: %s", raw)
            continue
        normalized.append(str(addr))
    return normalized


def parse_fallback_ip_env(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [part.strip() for part in value.split(",")]
    return _normalize_fallback_ips(parts)


def _resolve_system_dns() -> set[str]:
    """Return the IPv4 addresses that the OS resolver gives for api.telegram.org."""
    try:
        results = socket.getaddrinfo(_TELEGRAM_API_HOST, 443, socket.AF_INET)
        return {addr[4][0] for addr in results}
    except Exception:
        return set()


async def _query_doh_provider(
    client: httpx.AsyncClient, provider: dict
) -> list[str]:
    """Query one DoH provider and return A-record IPs."""
    try:
        resp = await client.get(
            provider["url"], params=provider["params"], headers=provider["headers"]
        )
        resp.raise_for_status()
        data = resp.json()
        ips: list[str] = []
        for answer in data.get("Answer", []):
            if answer.get("type") != 1:  # A record
                continue
            raw = answer.get("data", "").strip()
            try:
                ipaddress.ip_address(raw)
                ips.append(raw)
            except ValueError:
                continue
        return ips
    except Exception as exc:
        logger.debug("DoH query to %s failed: %s", provider["url"], exc)
        return []


async def discover_fallback_ips() -> list[str]:
    """Auto-discover Telegram API IPs via DNS-over-HTTPS.

    Resolves api.telegram.org through Google and Cloudflare DoH and returns all
    unique A records.  IPs that match the local system resolver are kept rather
    than excluded: in many networks the system-DNS IP is the most reliable path
    to api.telegram.org and a transient primary-path failure should be retried
    against the same address via the IP-rewrite path before the seed list is
    consulted (#14520).  Falls back to a hardcoded seed list only when DoH
    yields no usable answers.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(_DOH_TIMEOUT)) as client:
        doh_tasks = [_query_doh_provider(client, p) for p in _DOH_PROVIDERS]
        system_dns_task = asyncio.ensure_future(asyncio.to_thread(_resolve_system_dns))
        results = await asyncio.gather(*doh_tasks, return_exceptions=True)

    # The system-resolver leg runs socket.getaddrinfo in a worker thread with
    # no timeout of its own — a wedged OS resolver (broken VPN/DNS) can sit for
    # minutes. Its result only feeds the no-usable-answers log line below, so
    # it must never gate discovery: bound it and move on (#63309). The DoH legs
    # are already bounded by the client timeout above.
    system_ips: set[str] = set()
    try:
        system_result = await asyncio.wait_for(system_dns_task, timeout=_DOH_TIMEOUT)
        if isinstance(system_result, set):
            system_ips = system_result
    except Exception:
        logger.debug("System-DNS resolution for %s did not complete in time", _TELEGRAM_API_HOST)

    doh_ips: list[str] = []
    for r in results:
        if isinstance(r, list):
            doh_ips.extend(r)

    # Deduplicate preserving order
    seen: set[str] = set()
    candidates: list[str] = []
    for ip in doh_ips:
        if ip not in seen:
            seen.add(ip)
            candidates.append(ip)

    # Validate through existing normalization
    validated = _normalize_fallback_ips(candidates)

    if validated:
        logger.debug("Discovered Telegram fallback IPs via DoH: %s", ", ".join(validated))
        return validated

    logger.info(
        "DoH discovery yielded no usable IPs (system DNS: %s); using seed fallback IPs %s",
        ", ".join(system_ips) or "unknown",
        ", ".join(_SEED_FALLBACK_IPS),
    )
    return list(_SEED_FALLBACK_IPS)


def _rewrite_request_for_ip(request: httpx.Request, ip: str) -> httpx.Request:
    original_host = request.url.host or _TELEGRAM_API_HOST
    url = request.url.copy_with(host=ip)
    headers = request.headers.copy()
    headers["host"] = original_host
    extensions = dict(request.extensions)
    extensions["sni_hostname"] = original_host
    return httpx.Request(
        method=request.method,
        url=url,
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


def _is_retryable_connect_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))
