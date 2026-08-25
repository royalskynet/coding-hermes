"""Tests for gateway.lifecycle_ledger — unclean-shutdown detection (NS-608).

The ledger is a tiny sentinel state machine:
``record_startup`` claims ``state/gateway.lifecycle.json`` as
``phase=running``; every exit path calls ``mark_exited``; the next boot's
``record_startup``/``detect_unclean_exit`` reports a still-``running``
sentinel from a dead process as an unclean death (SIGKILL / OOM / VM loss)
and enriches the report with the last heartbeat's memory sample.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from gateway.lifecycle_ledger import (
    detect_unclean_exit,
    get_lifecycle_sentinel_path,
    mark_exited,
    read_prior_exit_label,
    record_startup,
    sample_memory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max on Linux; never alive


def _write_sentinel(home: Path, payload: dict) -> Path:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_sentinel(home: Path) -> dict:
    return json.loads(get_lifecycle_sentinel_path(home).read_text(encoding="utf-8"))


def _write_heartbeat(home: Path, payload: dict) -> Path:
    path = home / "state" / "gateway.heartbeat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _exit_diag_records(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# sample_memory
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="memory sampling is implemented for Linux (/proc) and macOS (psutil)",
)
def test_sample_memory_has_expected_keys_on_native_platform() -> None:
    sample = sample_memory()
    assert sample.get("rss_kib", 0) > 0
    assert sample.get("mem_total_kib", 0) > 0
    assert "mem_available_kib" in sample
    assert sample["mem_available_kib"] > 0
    assert "swap_used_kib" in sample


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS psutil sample")
def test_sample_memory_darwin_returns_real_values() -> None:
    sample = sample_memory()
    # psutil kiB conversions must be sane on darwin — 256 MiB machine floor.
    assert sample.get("rss_kib", 0) >= 0
    assert sample.get("mem_total_kib", 0) > 256 * 1024
    assert sample.get("mem_available_kib", 0) > 0
    assert sample.get("swap_used_kib", 0) >= 0


# ---------------------------------------------------------------------------
# First boot / clean lifecycle
# ---------------------------------------------------------------------------


def test_first_boot_reports_nothing_and_claims_sentinel(tmp_path: Path) -> None:
    assert record_startup(home=tmp_path) is None
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] == os.getpid()
    assert "start_time" in sentinel


def test_clean_exit_then_boot_reports_nothing(tmp_path: Path) -> None:
    record_startup(home=tmp_path)
    mark_exited(0, reason="graceful_shutdown", home=tmp_path)

    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "exited"
    assert sentinel["exit_code"] == 0
    assert sentinel["exit_reason"] == "graceful_shutdown"

    assert record_startup(home=tmp_path) is None
    assert _exit_diag_records(tmp_path) == []


# ---------------------------------------------------------------------------
# Unclean-death detection
# ---------------------------------------------------------------------------


def test_running_sentinel_from_dead_pid_is_unclean(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })

    evidence = detect_unclean_exit(home=tmp_path)
    assert evidence is not None
    assert evidence["prior_pid"] == _DEAD_PID
    assert evidence["prior_started_at"] == "2026-07-11T04:30:00+00:00"


def test_record_startup_persists_unclean_report_and_reclaims(tmp_path: Path) -> None:
    _write_sentinel(tmp_path, {
        "phase": "running",
        "pid": _DEAD_PID,
        "start_time": 1000.0,
        "started_at": "2026-07-11T04:30:00+00:00",
    })

    evidence = record_startup(home=tmp_path)
    assert evidence is not None

    records = _exit_diag_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tag"] == "gateway.previous_unclean_exit"
    assert records[0]["prior_pid"] == _DEAD_PID
    assert records[0]["pid"] == os.getpid()

    # Sentinel reclaimed for the new life.
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Takeover ownership guard on mark_exited
# ---------------------------------------------------------------------------


def test_mark_exited_leaves_pid_none_sentinel_alone(tmp_path: Path) -> None:
    """A sentinel with pid=None has unknown ownership — mark_exited must not
    clobber it with a clean-exit claim it cannot prove is its own."""
    _write_sentinel(tmp_path, {"phase": "running", "pid": None, "start_time": 2000.0})
    mark_exited(0, reason="graceful_shutdown", home=tmp_path)
    sentinel = _read_sentinel(tmp_path)
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] is None


# ---------------------------------------------------------------------------
# read_prior_exit_label (container-boot annotation)
# ---------------------------------------------------------------------------


def test_prior_exit_label_survives_corrupt_sentinel(tmp_path: Path) -> None:
    path = get_lifecycle_sentinel_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage", encoding="utf-8")
    assert read_prior_exit_label(tmp_path) == "unknown"


# ---------------------------------------------------------------------------
# B1 regression: graceful SIGTERM records a clean exit (NS-608 / B1)
#
# The gateway wires SIGTERM to the same graceful funnel that ends in
# ``mark_exited``.  A clean TERM must leave the sentinel ``phase=exited`` so
# the next boot does NOT report UNCLEANLY.  This guards the precise contract
# that distinguishes OOM/SIGKILL (unclean) from a normal managed restart.
# ---------------------------------------------------------------------------


def test_sigterm_writes_clean_exit_records(tmp_path: Path) -> None:
    home = tmp_path
    agent_repo = Path(__file__).resolve().parents[2]  # repo root
    stub = r"""
import json, os, signal, sys, time
from pathlib import Path
sys.path.insert(0, os.environ['__syspath__'])
from gateway.lifecycle_ledger import record_startup, mark_exited

home = Path(os.environ['ISO_HOME'])
record_startup(home=home)
print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
signal.signal(signal.SIGTERM,
              lambda s, f: (mark_exited(0, reason="graceful_shutdown", home=home),
                            sys.exit(0)))
deadline = time.time() + 60
while time.time() < deadline:
    time.sleep(0.1)
print(json.dumps({"timeout": True}), flush=True)
sys.exit(2)
"""
    iso_home = tmp_path  # reuse tmp_path; ledger writes go under state/
    proc = subprocess.Popen(
        [sys.executable, "-c", stub],
        env={**os.environ, "ISO_HOME": str(home), "__syspath__": str(agent_repo)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait for stub to claim the sentinel.
    deadline = time.time() + 30
    ready = False
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout is not None else ""
        if line:
            print("[stub] " + line.rstrip())
        if '"ready"' in line:
            ready = True
            break
        if proc.poll() is not None:
            break
    assert ready, f"stub never became ready (exit={proc.poll()})"

    os.kill(proc.pid, signal.SIGTERM)
    proc.wait(timeout=20)
    assert proc.returncode == 0, f"stub exit={proc.returncode}"

    sentinel = _read_sentinel(home)
    assert sentinel["phase"] == "exited"
    assert sentinel["pid"] == proc.pid
    assert sentinel["exit_code"] == 0
    assert sentinel["exit_reason"] == "graceful_shutdown"

    # A subsequent boot must report nothing (clean), not an unclean death.
    assert record_startup(home=home) is None
    assert _exit_diag_records(home) == []