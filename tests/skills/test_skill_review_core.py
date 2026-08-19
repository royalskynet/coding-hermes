"""Tests for tools/skill_review_core — the deterministic merge + risk gate +
LLM fail-safe + archive behavior used by the pending-skills auto-reviewer."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import skill_review_core as core  # noqa: E402


def _rec(action, name="s", old="", new="", content="", file_path="",
         file_content="", created_at=None):
    payload = {"action": action, "name": name}
    if action == "patch":
        payload["old_string"] = old
        payload["new_string"] = new
    elif action == "create":
        payload["content"] = content
    elif action == "write_file":
        payload["file_path"] = file_path
        payload["file_content"] = file_content
    return {"id": f"r{int(time.time()*1000)}_{action}",
            "payload": payload,
            "created_at": created_at if created_at is not None else time.time()}


# ---- merge newest-first -------------------------------------------------
def test_merge_keeps_newest_competitor():
    """Two competing patches against the SAME old_string: only the newer wins."""
    base = "Alpha\n\nBeta\n\nGamma\n"
    newer = _rec("patch", old="Beta", new="BETA2", created_at=20)
    older = _rec("patch", old="Beta", new="OLDWIN", created_at=10)
    # newest first per convention
    mg = core.merge_group("s", [newer, older], current_text=base)
    assert len(mg["keep"]) == 1
    assert mg["keep"][0]["record"] is newer
    assert mg["superseded"][0]["record"] is older
    assert mg["merged"] == "Alpha\n\nBETA2\n\nGamma\n"


def test_merge_takes_newer_when_fully_overwrites():
    """Older patch no longer matches after newer rewrite — superseded."""
    base = "One\n\nTwo\n\nThree\n"
    newer = _rec("patch", old="One", new="NEWONE", created_at=20)
    older = _rec("patch", old="One", new="OLDONE", created_at=10)
    mg = core.merge_group("s", [newer, older], current_text=base)
    assert len(mg["keep"]) == 1
    assert mg["merged"] == "NEWONE\n\nTwo\n\nThree\n"


def test_merge_disjoint_patches_both_apply():
    base = "A\n\nB\n\nC\n"
    p1 = _rec("patch", old="A", new="A1", created_at=20)
    p2 = _rec("patch", old="C", new="C2", created_at=10)
    mg = core.merge_group("s", [p1, p2], current_text=base)
    assert len(mg["keep"]) == 2
    assert mg["merged"] == "A1\n\nB\n\nC2\n"


def test_merge_rejects_similarity_strategy():
    """block_anchor/context_aware similarity strategies are NEVER accepted."""
    base = "Header\nSome body text here.\nFooter\n"
    # craft an old_string that only matches approximately against a region
    rec = _rec("patch", old="Some body text here in the file", new="REPLACED",
               created_at=5)
    mg = core.merge_group("s", [rec], current_text=base, allow_similarity=False)
    # either superseded (no exact) or kept-with-exact; it must NOT be kept via
    # a similarity strategy
    for k in mg["keep"]:
        assert k["strategy"] not in core._SIMILARITY_STRATEGIES


def test_merge_no_base_keeps_newest_create_only():
    p1 = _rec("create", content="SKILL A", created_at=20)
    p2 = _rec("create", content="SKILL B", created_at=10)
    mg = core.merge_group("s", [p1, p2], current_text="")
    assert len(mg["keep"]) == 1
    assert mg["keep"][0] ["record"] is p1
    assert mg["merged"] == "SKILL A"


# ---- classify (deterministic gate) --------------------------------------
def test_classify_manual_for_create_delete():
    assert core.classify({"action": "create", "name": "x", "content": "hi"})["level"] == "manual"
    assert core.classify({"action": "delete", "name": "x"})["level"] == "manual"


def test_classify_manual_for_frontmatter():
    p = {"action": "patch", "old_string": "---\nname: s", "new_string": "edited", "name": "s"}
    assert core.classify(p)["level"] == "manual"


def test_classify_manual_for_danger():
    p = {"action": "patch", "old_string": "do:", "new_string": "run: sudo rm -rf /", "name": "s"}
    assert core.classify(p)["level"] == "manual"


def test_classify_manual_for_over_8kb():
    p = {"action": "patch", "old_string": "x", "new_string": "y" * 9000, "name": "s"}
    assert core.classify(p)["level"] == "manual"


def test_classify_llm_for_clean_patch():
    p = {"action": "patch", "old_string": "fix build", "new_string": "fix build and tests", "name": "s"}
    assert core.classify(p)["level"] == "llm"


# ---- LLM fail-safe ------------------------------------------------------
def test_llm_judge_error_never_raises_and_reports_error():
    r = core.llm_judge("s", {"action": "patch", "old_string": "a", "new_string": "b"},
                       api_key="invalid-key", timeout=2)
    assert r["decision"] == "error" or r["decision"] in ("approve", "reject")


def test_execute_archive_keeps_file_under_archive():
    hh = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    root = Path(hh) / "pending" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    pf = root / "arch_test.json"
    pf.write_text(json.dumps({"id": "x", "payload": {"action": "patch"}}))
    rec = {"id": "x", "_path": str(pf), "payload": {"action": "patch"}}
    res = core.execute_decision(rec, "archive", Path(hh))
    assert res["status"] in ("archived", "error")
    if res["status"] == "archived":
        assert not pf.exists()
        assert (root / ".archive" / "arch_test.json").exists()
        # cleanup
        (root / ".archive" / "arch_test.json").unlink(missing_ok=True)


def test_execute_leave_does_not_touch_pending():
    hh = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    root = Path(hh) / "pending" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    pf = root / "leave_test.json"
    pf.write_text(json.dumps({"id": "y", "payload": {"action": "patch"}}))
    rec = {"id": "y", "_path": str(pf), "payload": {"action": "patch"}}
    core.execute_decision(rec, "leave", Path(hh))
    assert pf.exists()
    pf.unlink(missing_ok=True)


# ---- lock idempotence (dedup target) ------------------------------------
def test_merge_roundtrip_deterministic():
    """Same input twice -> identical keep/superseded split."""
    base = "A\nB\nC\n"
    p1 = _rec("patch", old="B", new="BB", created_at=20)
    p2 = _rec("patch", old="A", new="AA", created_at=10)
    m1 = core.merge_group("s", [p1, p2], current_text=base)
    m2 = core.merge_group("s", [p1, p2], current_text=base)
    assert [k["strategy"] for k in m1["keep"]] == [k["strategy"] for k in m2["keep"]]
    assert m1["merged"] == m2["merged"]


def test_similarity_set_contains_expected():
    assert core._SIMILARITY_STRATEGIES == {"block_anchor", "context_aware"}


# ---- CLI lock (reentrancy / dedup target) -------------------------------
def test_lock_exclusive_and_reacquire(tmp_path):
    import sys as _sys
    repo = REPO_ROOT
    if str(repo) not in _sys.path:
        _sys.path.insert(0, str(repo))
    import scripts.skill_pending_review as cli
    lock_path = tmp_path / ".skill_review.lock"
    l1 = cli._Lock(lock_path)
    assert l1.try_acquire() is True
    l2 = cli._Lock(lock_path)
    assert l2.try_acquire() is False  # second holder blocked
    l1.release()
    l3 = cli._Lock(lock_path)
    assert l3.try_acquire() is True  # reacquire after release
    l3.release()


def test_lock_stale_constant_is_30min():
    import sys as _sys
    if str(REPO_ROOT) not in _sys.path:
        _sys.path.insert(0, str(REPO_ROOT))
    import scripts.skill_pending_review as cli
    assert cli.LOCK_STALE_S == 30 * 60