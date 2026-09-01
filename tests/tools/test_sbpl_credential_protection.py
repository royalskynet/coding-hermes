"""Regression test for execute_code_confine.sbpl credential protection.

Ensures that the sbpl deny list is committed to git and contains
expected credential protection rules.
"""

import subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SBPL_PATH = REPO_ROOT / "tools" / "execute_code_confine.sbpl"


def test_sbpl_file_exists_and_committed():
    """Test that execute_code_confine.sbpl exists and is tracked by git."""
    # File should exist
    assert SBPL_PATH.exists(), f"{SBPL_PATH} should exist"

    # File should be tracked by git
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "tools/execute_code_confine.sbpl"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"{SBPL_PATH} should be tracked by git"


def test_sbpl_contains_expected_deny_rules():
    """Test that sbpl contains the expected credential deny rules from WO-01d and WO-01e."""
    content = SBPL_PATH.read_text()

    # Check for the four deny rules added in WO-01d
    assert '.config/gh' in content, "Should deny .config/gh"
    assert '.netrc' in content, "Should deny .netrc (literal)"
    assert '.npmrc' in content, "Should deny .npmrc (literal)"
    assert '.docker' in content, "Should deny .docker"

    # Check for .anthropic_oauth.json (was already there)
    assert '.anthropic_oauth.json' in content, "Should deny .anthropic_oauth.json"

    # Check that openclaw/credentials is denied (added in WO-01e)
    assert '.openclaw/credentials' in content, "Should deny .openclaw/credentials"

    # WO-01e removes the exception that exposed the live gateway token.
    assert 'gateway.token' not in content, "Must not allow access to gateway.token"


def test_sbpl_not_modified_since_last_commit():
    """Test that sbpl hasn't been modified since last commit (regression check)."""
    # Check git status for the file
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "tools/execute_code_confine.sbpl"],
        capture_output=True,
        cwd=REPO_ROOT,
    )
    # If returncode is 0, no differences (what we want)
    # If returncode is 1, there are differences (regression)
    assert result.returncode == 0, (
        f"{SBPL_PATH} has uncommitted changes. "
        "This violates the requirement that credential restrictions be committed."
    )


if __name__ == "__main__":
    # Allow running directly for quick verification
    test_sbpl_file_exists_and_committed()
    test_sbpl_contains_expected_deny_rules()
    test_sbpl_not_modified_since_last_commit()
    print("All sbpl credential protection tests passed!")
