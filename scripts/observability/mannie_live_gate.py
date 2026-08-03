#!/usr/bin/env python3
"""
Mannie Max-Autonomy 計畫 — 外部 Live 驗收腳本

用途：外部工程師（M1-M4）在 live 環境執行，產出 X1-X7 證據
輸出：`mannie-live-gate-report-<timestamp>.json` + `mannie-live-gate-checklist.md`
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ──────────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path("/Users/51mini/.hermes/hermes-agent")  # fixed repo root
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
PROFILE = os.environ.get("HERMES_PROFILE", "mannie")

LIVE_CHECKS = [
    {
        "id": "X1",
        "name": "strip-proxy SSE & health probe",
        "required": True,
        "commands": [
            "curl -sf http://localhost:8080/health",
            "curl -sf http://localhost:8080/health | jq -e '.status==\"ok\" and .sse==true'",
        ],
        "evidence": "health endpoint returns SSE=true + status=ok",
        "owner": "external"
    },
    {
        "id": "X2",
        "name": "LSP live availability (real local server)",
        "required": True,
        "commands": [
            "hermes lsp status --profile mannie",
            "hermes lsp definition --file agent/auxiliary_client.py --symbol _is_admission_busy_error --profile mannie",
        ],
        "evidence": "lsp status active + definition returns location",
        "owner": "external"
    },
    {
        "id": "X3",
        "name": "Background review denied trend (7-day window)",
        "required": True,
        "commands": [
            "python scripts/observability/profile_health_check.py --profile mannie --since 2026-07-27T00:00:00Z --json",
        ],
        "evidence": "denied trend decreasing or stable; review turn denominator available",
        "owner": "external"
    },
    {
        "id": "X4",
        "name": "Cron job version/patch history schema (if implemented)",
        "required": False,
        "commands": [
            "cat ~/.hermes/profiles/mannie/cron/jobs.json | jq '.jobs[].patch_history'",
        ],
        "evidence": "patch_history array exists on jobs",
        "owner": "external"
    },
    {
        "id": "X5",
        "name": "Secret scan (content-silent) on live profile",
        "required": True,
        "commands": [
            "bash /Users/51mini/.claude/plans/evidence/mannie-x0-20260803-113416/run-secret-scan.sh",
        ],
        "evidence": "All 7 secret classes = 0; aggregate = 0",
        "owner": "external"
    },
    {
        "id": "X6",
        "name": "Full regression suite (no flaky)",
        "required": True,
        "commands": [
            "python -m pytest tests/agent/test_admission_busy.py tests/agent/test_background_review_whitelist.py tests/agent/test_invalid_aux_response_and_payment_fallback.py tests/agent/test_model_incompatible_fallback.py tests/hermes_cli/test_inherit_notify_subs.py tests/plugins/memory/test_holographic_prefetch.py tests/scripts/test_profile_health_check.py tests/tools/test_lsp_availability.py -v --tb=short",
        ],
        "evidence": "134 tests passed",
        "owner": "external"
    },
    {
        "id": "X7",
        "name": "Health checker live profile run",
        "required": True,
        "commands": [
            "python scripts/observability/profile_health_check.py --profile mannie --since 2026-08-03T00:00:00Z --json",
        ],
        "evidence": "Four checks output; status=fail (expected bg-review denied); exit=0",
        "owner": "external"
    },
]

CHECKLIST_TEMPLATE = """# Mannie Max-Autonomy Live Gate Checklist

> Generated: {timestamp}
> Profile: {profile}
> Operator: {operator}
> Repo: {repo}
> Commit: {commit}

---

## Gate Matrix

| Check | Required | Status | Evidence |
|-------|----------|--------|----------|
{rows}

---

## Run Instructions

```bash
# 1. Clone & setup (external engineer)
git clone git@github.com:royalskynet/hermes-agent-local.git
cd hermes-agent-local
git remote add upstream https://github.com/NousResearch/hermes-agent.git

# 2. Run this script
python mannie_live_gate.py --profile mannie --since 2026-08-03T00:00:00Z --output-dir /tmp/mannie-gate

# 3. Review generated files
cat /tmp/mannie-gate/mannie-live-gate-checklist.md
cat /tmp/mannie-gate/mannie-live-gate-report-*.json
```

---

## Acceptance Criteria

- **All required checks = PASS** → Gate PASSED
- **Any required check = FAIL** → Gate BLOCKED (do not promote)
- **Optional checks** → Record status; do not block
- **Flaky tests** (test_auxiliary_client timeout, test_file_safety cache) → Expected to fail occasionally; re-run once; if persistent → BLOCKED

---

## Evidence Files (auto-linked)

{evidence_links}

---

## Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| External Engineer (X1-X7) | | | |
| Profile Owner (mannie) | | | |

---

*This checklist is generated by `mannie_live_gate.py` — do not edit manually.*
"""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def run_shell_cmd(cmd_str: str, cwd: Optional[Path] = None, timeout: int = 60) -> Dict[str, Any]:
    """Run shell command string, return structured result."""
    try:
        result = subprocess.run(
            cmd_str,
            shell=True,
            cwd=cwd or REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "command": cmd_str,
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "success": result.returncode == 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except subprocess.TimeoutExpired:
        return {
            "command": cmd_str,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Timeout after {timeout}s",
            "success": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "command": cmd_str,
            "exit_code": -2,
            "stdout": "",
            "stderr": str(e),
            "success": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def get_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()[:12] if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def get_operator() -> str:
    return os.environ.get("USER", "unknown")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Mannie Max-Autonomy Live Gate")
    parser.add_argument("--profile", default=PROFILE, help="Hermes profile name")
    parser.add_argument("--since", default="2026-08-03T00:00:00Z", help="ISO 8601 window start")
    parser.add_argument("--output-dir", default="/tmp/mannie-gate", help="Output directory")
    parser.add_argument("--skip", nargs="*", default=[], help="Skip check IDs (e.g., X4)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    commit = get_git_commit()
    operator = get_operator()

    print(f"=== Mannie Live Gate [{timestamp}] ===")
    print(f"Profile: {args.profile}")
    print(f"Commit: {commit}")
    print(f"Output: {output_dir}")

    results = []

    for check in LIVE_CHECKS:
        check_id = check["id"]
        if check_id in args.skip:
            print(f"  [{check_id}] SKIPPED (user request)")
            results.append({
                "id": check_id,
                "name": check["name"],
                "required": check["required"],
                "skipped": True,
                "status": "skipped",
                "evidence": check["evidence"],
                "commands": [],
            })
            continue

        print(f"  [{check_id}] {check['name']}...")
        cmd_results = []
        all_pass = True

        for cmd_str in check["commands"]:
            res = run_shell_cmd(cmd_str)
            cmd_results.append(res)
            if not res["success"]:
                all_pass = False
            status = "✅" if res["success"] else "❌"
            print(f"    {status} {cmd_str} (exit={res['exit_code']})")

        check_result = {
            "id": check_id,
            "name": check["name"],
            "required": check["required"],
            "skipped": False,
            "status": "pass" if all_pass else "fail",
            "evidence": check["evidence"],
            "commands": cmd_results,
        }
        results.append(check_result)

    # Generate report JSON
    report = {
        "timestamp": timestamp,
        "profile": args.profile,
        "commit": commit,
        "operator": operator,
        "repo": str(REPO_ROOT),
        "checks": results,
        "summary": {
            "total": len([r for r in results if not r["skipped"]]),
            "passed": len([r for r in results if r["status"] == "pass"]),
            "failed": len([r for r in results if r["status"] == "fail"]),
            "skipped": len([r for r in results if r["skipped"]]),
            "gate_status": "PASSED" if all(r["status"] == "pass" for r in results if r["required"] and not r["skipped"]) else "BLOCKED",
        },
    }

    report_path = output_dir / f"mannie-live-gate-report-{timestamp}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport: {report_path}")

    # Generate checklist MD
    rows = []
    evidence_links = []
    for r in results:
        status_emoji = {"pass": "✅", "fail": "❌", "skipped": "⏭️"}.get(r["status"], "❓")
        req = "Yes" if r["required"] else "No"
        evidence = "—"
        if r.get("commands") and len(r["commands"]) > 0:
            evidence = r["commands"][0].get("stdout", "")[:80]
        rows.append(f"| {r['id']} | {req} | {status_emoji} {r['status'].upper()} | {evidence} |")
        for cmd_result in r.get("commands", []):
            if cmd_result.get("stdout"):
                evidence_links.append(f"- `{r['id']}` cmd: `{cmd_result.get('command', '')}` stdout: {cmd_result['stdout'][:120]}")

    checklist = CHECKLIST_TEMPLATE.format(
        timestamp=timestamp,
        profile=args.profile,
        operator=operator,
        repo=str(REPO_ROOT),
        commit=commit,
        rows="\n".join(rows),
        evidence_links="\n".join(evidence_links) or "None",
    )

    checklist_path = output_dir / f"mannie-live-gate-checklist-{timestamp}.md"
    with open(checklist_path, "w") as f:
        f.write(checklist)
    print(f"Checklist: {checklist_path}")

    # Summary
    print(f"\n=== SUMMARY ===")
    print(f"Total: {report['summary']['total']}")
    print(f"Passed: {report['summary']['passed']}")
    print(f"Failed: {report['summary']['failed']}")
    print(f"Skipped: {report['summary']['skipped']}")
    print(f"GATE: {report['summary']['gate_status']}")

    if report["summary"]["gate_status"] == "BLOCKED":
        sys.exit(1)


if __name__ == "__main__":
    main()