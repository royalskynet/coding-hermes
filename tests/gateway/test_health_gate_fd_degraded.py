from gateway.health_gate import HealthGate, HealthSample, health_gate_allows_long_work


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
