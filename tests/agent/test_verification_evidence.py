import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.verification_evidence import (
    classify_verification_command,
    mark_workspace_edited,
    record_terminal_result,
    verification_status,
)


def _node_project(root: Path) -> None:
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "lint": "eslint .", "dev": "vite"}})
    )
    (root / "pnpm-lock.yaml").write_text("")
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "run_tests.sh").write_text("#!/bin/sh\n")


def _python_project(root: Path) -> None:
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")








def test_lint_and_typecheck_are_not_reported_as_full_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _node_project(tmp_path)

    lint = classify_verification_command(
        "pnpm run lint",
        cwd=tmp_path,
        session_id="s1",
        exit_code=0,
    )
    test = classify_verification_command(
        "pnpm run test -- tests/button.test.tsx",
        cwd=tmp_path,
        session_id="s1",
        exit_code=0,
    )

    assert lint is not None
    assert lint.kind == "lint"
    assert lint.scope == "full"
    assert test is not None
    assert test.kind == "test"
    assert test.scope == "targeted"




def test_shell_wrappers_match_but_echo_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _node_project(tmp_path)

    wrapped = classify_verification_command(
        "env CI=1 bash scripts/run_tests.sh tests/test_widget.py",
        cwd=tmp_path,
        session_id="s1",
        exit_code=0,
    )
    echoed = classify_verification_command(
        "echo scripts/run_tests.sh tests/test_widget.py",
        cwd=tmp_path,
        session_id="s1",
        exit_code=0,
    )

    assert wrapped is not None
    assert wrapped.canonical_command == "scripts/run_tests.sh"
    assert wrapped.scope == "targeted"
    assert echoed is None




def test_temp_script_records_ad_hoc_evidence_without_canonical_suite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    script = Path(tempfile.gettempdir()) / f"hermes-ad-hoc-{tmp_path.name}.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    try:
        evidence = classify_verification_command(
            f"python {script}",
            cwd=tmp_path,
            session_id="s1",
            exit_code=0,
            output="ok",
        )
    finally:
        script.unlink(missing_ok=True)

    assert evidence is not None
    assert evidence.canonical_command == "ad-hoc verification script"
    assert evidence.kind == "ad_hoc"
    assert evidence.scope == "targeted"
    assert evidence.status == "passed"












def test_file_tool_stales_evidence_by_session_id_for_absolute_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _node_project(tmp_path)
    target = tmp_path / "src" / "app.ts"
    target.parent.mkdir()

    record_terminal_result(
        command="pnpm test",
        cwd=tmp_path,
        session_id="conversation",
        exit_code=0,
        output="green",
    )

    from tools.file_tools import write_file_tool

    result = json.loads(
        write_file_tool(
            str(target),
            "export const ok = true\n",
            task_id="turn",
            session_id="conversation",
        )
    )

    assert result["files_modified"] == [str(target.resolve())]
    assert verification_status(session_id="conversation", cwd=tmp_path)["status"] == "stale"
    assert verification_status(session_id="turn", cwd=tmp_path)["status"] == "unverified"






def test_recording_expires_old_edit_only_state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _node_project(tmp_path)

    mark_workspace_edited(
        session_id="old-session",
        cwd=tmp_path,
        paths=[str(tmp_path / "src" / "app.ts")],
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with sqlite3.connect(home / "verification_evidence.db") as conn:
        conn.execute("UPDATE verification_state SET last_edit_at = ?", (cutoff,))
        conn.commit()

    record_terminal_result(
        command="pnpm test",
        cwd=tmp_path,
        session_id="new-session",
        exit_code=0,
        output="new green",
    )

    status = verification_status(session_id="old-session", cwd=tmp_path)
    assert status["status"] == "unverified"
    assert status["changed_paths"] == []


def test_windows_backslash_ad_hoc_script_path_is_matched(tmp_path, monkeypatch):
    """Ad-hoc verification scripts with Windows backslash paths must be
    matched by ``_find_ad_hoc_match`` trying ``posix=False`` in addition to
    the default ``posix=True``. (#53553 / #65919)

    On Linux, ``Path`` doesn't parse Windows backslash paths, so we mock
    ``_is_temp_script_path`` to simulate the Windows environment where the
    path resolves correctly. The test verifies the posix=False splitting
    fallback — the actual fix from #53553.
    """
    from agent.verification_evidence import _find_ad_hoc_match

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    # On Windows, shlex.split(posix=True) eats backslashes as escape chars;
    # posix=False preserves them. Mock _is_temp_script_path so the test
    # focuses on the splitting fallback without needing a real Windows FS.
    def mock_is_temp_script(token, root):
        return "hermes-ad-hoc" in token and ".py" in token

    monkeypatch.setattr(
        "agent.verification_evidence._is_temp_script_path",
        mock_is_temp_script,
    )

    win_script = r"C:\Users\test\AppData\Local\Temp\hermes-ad-hoc-check.py"
    result = _find_ad_hoc_match(f"python {win_script}", tmp_path)
    assert result is not None, (
        "Windows backslash path should be matched via posix=False fallback"
    )


def _git_root_project(root: Path) -> None:
    """Empty .git dir makes ``project_facts_for`` treat ``root`` as an
    independent git workspace with no canonical verify commands."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    (root / "code.py").write_text("x = 1\n", encoding="utf-8")


def test_ad_hoc_session_evidence_covers_sibling_no_canonical_root(tmp_path, monkeypatch):
    """A single turn editing two roots is fully verified once one of them runs
    a passing ad-hoc ``hermes-verify-*`` script. Both roots have no canonical
    verify command, so the ad-hoc pass covers the sibling root too (#ad-hoc)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    root_a = tmp_path / "rootA"
    root_b = tmp_path / "rootB"
    for r in (root_a, root_b):
        _git_root_project(r)

    script = tmp_path / "hermes-verify-x.py"
    script.write_text("assert True\n", encoding="utf-8")

    sid = "sess-multiroot"
    mark_workspace_edited(session_id=sid, cwd=root_a, paths=[str(root_a / "code.py")])
    mark_workspace_edited(session_id=sid, cwd=root_b, paths=[str(root_b / "code.py")])

    rec = record_terminal_result(
        command=f"python3 {script}",
        cwd=root_a,
        session_id=sid,
        exit_code=0,
        output="PASS",
    )
    assert rec is not None and rec["kind"] == "ad_hoc" and rec["status"] == "passed"

    assert verification_status(session_id=sid, cwd=root_b)["status"] == "passed"


def test_ad_hoc_does_not_relax_canonical_root(tmp_path, monkeypatch):
    """A canonical-command repo must NOT be relaxed to passed by a passing
    ad-hoc script in the same session — it must prove a real canonical run."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    root_a = tmp_path / "rootA"
    _git_root_project(root_a)
    root_c = tmp_path / "rootC"  # canonical repo (has verifyCommands)
    root_c.mkdir()
    (root_c / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    script = tmp_path / "hermes-verify-x.py"
    script.write_text("assert True\n", encoding="utf-8")

    sid = "sess-canonical"
    mark_workspace_edited(session_id=sid, cwd=root_a, paths=[str(root_a / "code.py")])
    mark_workspace_edited(session_id=sid, cwd=root_c, paths=[str(root_c / "pyproject.toml")])

    rec = record_terminal_result(
        command=f"python3 {script}",
        cwd=root_a,
        session_id=sid,
        exit_code=0,
        output="PASS",
    )
    assert rec is not None and rec["kind"] == "ad_hoc" and rec["status"] == "passed"

    # canonical root must stay unverified — ad-hoc does not relax it
    status = verification_status(session_id=sid, cwd=root_c)
    assert status["status"] != "passed"
