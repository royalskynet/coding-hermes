import logging

from gateway.health_gate import (
    HealthGate,
    HealthSample,
    collect_telegram_health,
    health_gate_allows_long_work,
    schedule_graceful_restart,
)


def _sample(fd=20, close_wait=0, failures=0):
    return HealthSample(
        fd_total=fd,
        close_wait=close_wait,
        cpu_percent=1.5,
        telegram_state="connected",
        fallback_failures=failures,
        breaker_state="closed",
        backoff_seconds=0,
    )


def test_degraded_blocks_new_long_work_and_notices_once(tmp_path):
    notices = []
    restarts = []
    gate = HealthGate(
        state_path=tmp_path / "health_gate.json",
        notice_callback=notices.append,
        restart_callback=lambda: restarts.append(True),
        clock=lambda: 1000.0,
    )

    assert gate.evaluate(_sample(fd=161, close_wait=11)).state == "degraded"
    assert health_gate_allows_long_work(gate) is False
    gate.evaluate(_sample(fd=170, close_wait=12))

    assert len(notices) == 1
    assert "network" in notices[0].lower()
    assert restarts == []


def test_recovery_accepts_long_work_again(tmp_path):
    gate = HealthGate(state_path=tmp_path / "health_gate.json")
    gate.evaluate(_sample(fd=161))

    assert gate.evaluate(_sample()).state == "ok"
    assert health_gate_allows_long_work(gate) is True


def test_critical_requests_graceful_restart_without_killing_process(tmp_path):
    restarts = []
    gate = HealthGate(
        state_path=tmp_path / "health_gate.json",
        restart_callback=lambda: restarts.append("graceful"),
    )

    assert gate.evaluate(_sample(fd=241)).state == "critical"
    assert restarts == ["graceful"]


def test_sampling_state_and_config_failures_fail_open(tmp_path):
    sampling_gate = HealthGate(
        state_path=tmp_path / "sampling.json",
        sampler=lambda: (_ for _ in ()).throw(OSError("lsof unavailable")),
    )
    config_gate = HealthGate(
        config={"degraded_fd": "broken"},
        state_path=tmp_path / "config.json",
    )
    state_gate = HealthGate(state_path=tmp_path)  # directory cannot be read as JSON

    assert sampling_gate.sample_once().state == "ok"
    assert config_gate.evaluate(_sample(fd=999)).state == "ok"
    assert state_gate.state == "ok"
    assert health_gate_allows_long_work(None) is True


def test_clearly_unhealthy_sample_is_never_ok(tmp_path):
    gate = HealthGate(state_path=tmp_path / "health_gate.json")

    assert gate.evaluate(_sample(fd=999, close_wait=999, failures=99)).state != "ok"


def test_invalid_intervals_fall_back_to_safe_default(tmp_path):
    for value in (0, -1, float("inf"), float("nan"), "invalid"):
        gate = HealthGate(
            config={"sample_interval_seconds": value},
            state_path=tmp_path / f"state-{str(value)}.json",
        )
        assert gate.sample_interval == 60


def test_telegram_pause_is_open_but_gateway_restart_latch_is_irrelevant(caplog, tmp_path):
    class Adapter:
        def is_connected(self):
            return False

    sample = collect_telegram_health(
        Adapter(),
        {"telegram": {"attempts": 7, "paused": True, "next_retry": float("inf")}},
        now=100.0,
    )
    assert sample == ("disconnected", 7, "open", None)

    gate = HealthGate(state_path=tmp_path / "health.json")
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        gate._log(_sample().__class__(20, 0, 1.0, sample[0], sample[1], sample[2], sample[3]))
    assert "breaker=open" in caplog.text
    # No restart-latch parameter exists: whole-gateway restart cannot be
    # mislabeled as the Telegram breaker.


def test_production_restart_scheduler_calls_injected_request_restart():
    scheduled = []

    class Loop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, callback):
            scheduled.append(callback)

    calls = []
    schedule_graceful_restart(Loop(), lambda: calls.append("request_restart"))
    scheduled[0]()
    assert calls == ["request_restart"]
