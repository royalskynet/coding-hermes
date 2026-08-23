"""Fail-open runtime health gate for long gateway workorders."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger("gateway.run")

DEFAULT_SAMPLE_INTERVAL_SECONDS = 60
DEFAULT_DEGRADED_FD = 160
DEFAULT_DEGRADED_CLOSE_WAIT = 10
DEFAULT_FALLBACK_FAILURES = 5
DEFAULT_CRITICAL_FD = 240
DEFAULT_CRITICAL_AFTER_SECONDS = 180


@dataclass(frozen=True)
class HealthSample:
    fd_total: int
    close_wait: int
    cpu_percent: Optional[float] = None
    telegram_state: str = "unavailable"
    fallback_failures: int = 0
    breaker_state: str = "unavailable"
    backoff_seconds: Optional[float] = None


@dataclass(frozen=True)
class HealthVerdict:
    state: str
    sample: Optional[HealthSample] = None


def _default_state_path() -> Path:
    return get_hermes_home() / "gateway" / "health_gate.json"


def sample_process(pid: Optional[int] = None) -> HealthSample:
    """Bounded, error-tolerant macOS process sample using lsof and ps."""
    process_id = os.getpid() if pid is None else pid
    all_fds = subprocess.run(
        ["lsof", "-n", "-P", "-p", str(process_id)],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if all_fds.returncode not in (0, 1):
        raise OSError(f"lsof failed with exit {all_fds.returncode}")
    lines = [line for line in all_fds.stdout.splitlines() if line.strip()]
    fd_total = max(0, len(lines) - 1)  # header is not a descriptor

    sockets = subprocess.run(
        ["lsof", "-n", "-P", "-p", str(process_id), "-a", "-i"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    close_wait = sockets.stdout.upper().count("CLOSE_WAIT")
    cpu_percent = None
    try:
        cpu = subprocess.run(
            ["ps", "-p", str(process_id), "-o", "%cpu="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        cpu_percent = float(cpu.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return HealthSample(fd_total=fd_total, close_wait=close_wait, cpu_percent=cpu_percent)


class HealthGate:
    """Evaluate samples, persist degraded duration, and own monitor lifecycle."""

    def __init__(
        self,
        config: Optional[Mapping[str, object]] = None,
        *,
        state_path: Optional[Path] = None,
        sampler: Callable[[], HealthSample] = sample_process,
        notice_callback: Optional[Callable[[str], object]] = None,
        restart_callback: Optional[Callable[[], object]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        cfg = config or {}
        self.sample_interval = self._number(cfg, "sample_interval_seconds", DEFAULT_SAMPLE_INTERVAL_SECONDS)
        self.degraded_fd = self._number(cfg, "degraded_fd", DEFAULT_DEGRADED_FD)
        self.degraded_close_wait = self._number(cfg, "degraded_close_wait", DEFAULT_DEGRADED_CLOSE_WAIT)
        self.fallback_failures = self._number(cfg, "consecutive_fallback_failures", DEFAULT_FALLBACK_FAILURES)
        self.critical_fd = self._number(cfg, "critical_fd", DEFAULT_CRITICAL_FD)
        self.critical_after = self._number(cfg, "critical_after_seconds", DEFAULT_CRITICAL_AFTER_SECONDS)
        self._config_valid = all(value is not None for value in (
            self.sample_interval, self.degraded_fd, self.degraded_close_wait,
            self.fallback_failures, self.critical_fd, self.critical_after,
        ))
        self._state_path = state_path or _default_state_path()
        self._sampler = sampler
        self._notice_callback = notice_callback
        self._restart_callback = restart_callback
        self._clock = clock
        self.state = "ok"
        self._degraded_since: Optional[float] = None
        self._notice_sent = False
        self._restart_sent = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._lock = threading.Lock()
        self._load_state()

    @staticmethod
    def _number(cfg: Mapping[str, object], key: str, default: int) -> Optional[float]:
        try:
            value = float(cfg.get(key, default))
            return value if value >= 0 else None
        except (TypeError, ValueError):
            return None

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if data.get("state") == "degraded":
                self.state = "degraded"
                self._degraded_since = float(data["degraded_since"])
                self._notice_sent = bool(data.get("notice_sent"))
        except (OSError, ValueError, TypeError, KeyError):
            # Persistence is advisory. Any corruption fails open.
            self.state = "ok"
            self._degraded_since = None

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({
                "state": self.state,
                "degraded_since": self._degraded_since,
                "notice_sent": self._notice_sent,
            }), encoding="utf-8")
        except OSError:
            pass

    def evaluate(self, sample: HealthSample) -> HealthVerdict:
        if not self._config_valid:
            self.state = "ok"
            return HealthVerdict("ok", sample)
        now = self._clock()
        unhealthy = (
            sample.fd_total > self.degraded_fd
            or sample.close_wait > self.degraded_close_wait
            or sample.fallback_failures >= self.fallback_failures
        )
        if sample.fd_total > self.critical_fd:
            next_state = "critical"
        elif unhealthy:
            if self._degraded_since is None:
                self._degraded_since = now
            next_state = "critical" if now - self._degraded_since > self.critical_after else "degraded"
        else:
            next_state = "ok"

        if next_state == "ok":
            self._degraded_since = None
            self._notice_sent = False
            self._restart_sent = False
        elif next_state == "degraded" and not self._notice_sent:
            self._notice_sent = True
            try:
                if self._notice_callback:
                    self._notice_callback(
                        "The network is degraded and backing off. New long workorders "
                        "are paused; Telegram remains available and running work continues."
                    )
            except Exception:
                pass
        elif next_state == "critical" and not self._restart_sent:
            self._restart_sent = True
            try:
                if self._restart_callback:
                    self._restart_callback()
            except Exception:
                pass

        self.state = next_state
        self._save_state()
        self._log(sample)
        return HealthVerdict(next_state, sample)

    def _log(self, sample: HealthSample) -> None:
        logger.info(
            "[HEALTH] fd=%d close_wait=%d cpu=%s telegram=%s fallback_failures=%d "
            "gate=%s breaker=%s backoff=%s",
            sample.fd_total, sample.close_wait,
            "unavailable" if sample.cpu_percent is None else f"{sample.cpu_percent:.1f}%",
            sample.telegram_state, sample.fallback_failures, self.state,
            sample.breaker_state,
            "unavailable" if sample.backoff_seconds is None else f"{sample.backoff_seconds:.0f}s",
        )

    def sample_once(self) -> HealthVerdict:
        try:
            return self.evaluate(self._sampler())
        except Exception as exc:
            logger.warning("[HEALTH] sample unavailable; gate fails open: %s", exc)
            self.state = "ok"
            return HealthVerdict("ok")

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not self._config_valid:
                return False
            self._stop_event = threading.Event()
            self.sample_once()
            self._thread = threading.Thread(
                target=self._monitor_loop, name="gateway-health-monitor", daemon=True,
            )
            self._thread.start()
            return True

    def _monitor_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.wait(self.sample_interval):
            self.sample_once()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._stop_event is None or self._thread is None:
                return
            self._stop_event.set()
            thread = self._thread
            self._stop_event = None
            self._thread = None
        thread.join(timeout=timeout)


def health_gate_allows_long_work(gate: Optional[HealthGate]) -> bool:
    """Fail-open dispatch seam used by the existing kanban scheduler."""
    try:
        return gate is None or gate.state == "ok"
    except Exception:
        return True
