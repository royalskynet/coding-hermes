"""True SOUL core guard tests (no self-referential mocks).

Covers the landed PUT /soul guard in hermes_cli/web_routers/profiles.py:
  1. Cross-implementation byte-identity vs the koko weekly_retro authority.
  2. Real handler behavior via TestClient against web_server.app.
"""
import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Production implementation under test.
from hermes_cli.web_routers import profiles as p

# ── Authority (weekly_retro) dynamic loader ─────────────────────────
_KOKO_CRON = Path("/Users/51mini/.hermes/profiles/koko/cron")
_WEEKLY_RETRO = _KOKO_CRON / "weekly_retro.py"


@pytest.fixture(scope="module")
def weekly_retro():
    if not _WEEKLY_RETRO.exists():
        pytest.skip("koko weekly_retro.py authority not available")
    sys.path.insert(0, str(_KOKO_CRON))
    try:
        spec = importlib.util.spec_from_file_location("weekly_retro_soul_test", _WEEKLY_RETRO)
        assert spec and spec.loader, "failed to load weekly_retro authority"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(_KOKO_CRON))


# ── TestClient infrastructure ───────────────────────────────────────
@pytest.fixture()
def client():
    from starlette.testclient import TestClient

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    app.state.auth_required = False
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    yield c
    app.state.auth_required = False


# ── Step 1 tests: cross-impl byte identity ──────────────────────────


def test_cross_impl_byte_identical_on_koko_soul(weekly_retro):
    koko_soul = Path("/Users/51mini/.hermes/profiles/koko/SOUL.md")
    if not koko_soul.exists():
        pytest.skip("koko SOUL.md not available")
    lines = koko_soul.read_text(encoding="utf-8").splitlines(keepends=True)
    a = p._extract_core_block(lines)
    b = weekly_retro.extract_core_block(lines)
    assert a == b and a is not None, "CROSS-IMPL MISMATCH on koko SOUL"

    # Three-way sha256: production == authority == awk subprocess.
    awk = subprocess.run(
        ["awk", "/CORE:BEGIN/,/CORE:END/"],
        input=koko_soul.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        check=True,
    )
    awk_hash = hashlib.sha256(awk.stdout.encode()).hexdigest()
    assert p._core_hash_of(a) == awk_hash == weekly_retro.core_hash_of(b)


@pytest.mark.parametrize(
    "content",
    [
        "no markers at all\n",
        "",
        "CORE:BEGIN\nfirst\nCORE:END\n",
        "CORE:BEGIN\nCORE:END\n",
        "CORE:END\nbefore\nCORE:BEGIN\n",
        "x\nCORE:BEGIN\nblock1\nCORE:END\nmid\nCORE:BEGIN\nblock2\nCORE:END\n",
        "CORE:BEGIN\nno trailing newline\nCORE:END",
        "line CORE:BEGIN inline\nbody\nline CORE:END inline\n",
        "CORE:BEGIN only\n",
        "CORE:END only\n",
    ],
)
def test_cross_impl_byte_identical_edge_cases(weekly_retro, content):
    lines = content.splitlines(keepends=True)
    a = p._extract_core_block(lines)
    b = weekly_retro.extract_core_block(lines)
    assert a == b


def test_prod_hash_matches_authority_core_hash_of(weekly_retro):
    koko_soul = Path("/Users/51mini/.hermes/profiles/koko/SOUL.md")
    if not koko_soul.exists():
        pytest.skip("koko SOUL.md not available")
    lines = koko_soul.read_text(encoding="utf-8").splitlines(keepends=True)
    core = p._extract_core_block(lines)
    assert core is not None
    assert p._core_hash_of(core) == weekly_retro.core_hash_of(core)


# ── Step 2 tests: real handler behavior via TestClient ──────────────


def _soul_with_core(body="ignored"):
    return f"""# SOUL

CORE:BEGIN
_system: you are koko, steadfast.
{body}
CORE:END

## Growth
"""


def _setup_governed(tmp_path):
    """SOUL.md with CORE + matching soul_core.sha256."""
    core = _soul_with_core()
    (tmp_path / "SOUL.md").write_text(core, encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    hash_val = p._core_hash_of(p._extract_core_block(core.splitlines(keepends=True)))
    (mem / "soul_core.sha256").write_text(hash_val + "\n", encoding="utf-8")


def _setup_plain(tmp_path):
    """SOUL.md with no CORE, no hash file."""
    (tmp_path / "SOUL.md").write_text("# SOUL\nno core here\n", encoding="utf-8")


def _put(client, monkeypatch, tmp_path, content):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_resolve_profile_dir", lambda _n: tmp_path)
    return client.put("/api/profiles/test/soul", json={"content": content})


def test_put_soul_core_modified_403_no_garbage_backup(client, monkeypatch, tmp_path):
    _setup_governed(tmp_path)
    resp = _put(client, monkeypatch, tmp_path, _soul_with_core("TAMPERED"))
    assert resp.status_code == 403
    # SOUL unchanged, hash unchanged, no backup garbage.
    assert "TAMPERED" not in (tmp_path / "SOUL.md").read_text(encoding="utf-8")
    assert (tmp_path / "memory" / "soul_core.sha256").exists()
    assert list(tmp_path.glob("SOUL.md.bak*")) == []


def test_put_soul_core_removed_403(client, monkeypatch, tmp_path):
    _setup_governed(tmp_path)
    resp = _put(client, monkeypatch, tmp_path, "# SOUL\nno core at all\n")
    assert resp.status_code == 403
    assert list(tmp_path.glob("SOUL.md.bak*")) == []


def test_put_soul_hash_file_mismatch_403(client, monkeypatch, tmp_path):
    # hash file is authoritative: even a "correct" unchanged CORE must match the
    # on-disk hash. Corrupt the hash file to force a mismatch.
    _setup_governed(tmp_path)
    (tmp_path / "memory" / "soul_core.sha256").write_text("deadbeef\n", encoding="utf-8")
    resp = _put(client, monkeypatch, tmp_path, _soul_with_core())
    assert resp.status_code == 403
    assert "hash mismatch" in resp.json()["detail"].lower() or "hash" in resp.json()["detail"].lower()


def test_put_soul_growth_edit_success_backup_and_hash(client, monkeypatch, tmp_path):
    _setup_governed(tmp_path)
    new = _soul_with_core() + "\n\n## Added growth\n"
    resp = _put(client, monkeypatch, tmp_path, new)
    assert resp.status_code == 200
    assert resp.json()["core_guard"] is True
    # Exactly one backup, content == old text.
    baks = list(tmp_path.glob("SOUL.md.bak*"))
    assert len(baks) == 1
    assert baks[0].read_text(encoding="utf-8") == _soul_with_core()
    # Hash file recomputed == new CORE hash.
    new_core = p._extract_core_block(new.splitlines(keepends=True))
    stored = (tmp_path / "memory" / "soul_core.sha256").read_text(encoding="utf-8").strip()
    assert stored == p._core_hash_of(new_core)


def test_put_soul_ungoverned_profile_passthrough(client, monkeypatch, tmp_path):
    _setup_plain(tmp_path)
    resp = _put(client, monkeypatch, tmp_path, "# SOUL\nplain overwrite\n")
    assert resp.status_code == 200
    assert resp.json()["core_guard"] is False
    # No hash file created for ungoverned profile.
    assert not (tmp_path / "memory" / "soul_core.sha256").exists()


def test_put_soul_core_present_but_no_hash_file(client, monkeypatch, tmp_path):
    # SOUL has CORE but no hash file → guarded by CORE presence; growth-edit ok
    # and hash file gets created.
    (tmp_path / "SOUL.md").write_text(_soul_with_core(), encoding="utf-8")
    resp = _put(client, monkeypatch, tmp_path, _soul_with_core() + "\n\n## Growth\n")
    assert resp.status_code == 200
    assert resp.json()["core_guard"] is True
    assert (tmp_path / "memory" / "soul_core.sha256").exists()
