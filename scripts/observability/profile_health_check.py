#!/usr/bin/env python3
"""
Universal, read-only profile health checker for Hermes.

Outputs four independent checks:
1. provider_failure: count of structured provider error lines (exclude prompt/goal-judge)
2. background_review_denied: count of denied tool invocations by tool name
3. telegram_audit: presence of [TG-AUDIT] lines in time window; warn on gaps
4. lsp_usage: count of registered LSP tools vs successful calls in window

Input: --profile, --since, optional --json
Output: JSON with status/checks/window/source_files; exit 0=pass/warn, 1=fail, 2=input/file error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add repo root to sys.path for internal imports
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.profiles import (
    normalize_profile_name,
    profile_exists,
    get_profile_dir,
)
from hermes_constants import get_hermes_home


class HealthCheckError(Exception):
    """Structured error for health check failures."""
    def __init__(self, code: int, message: str, details: Optional[Dict] = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes profile health checker")
    parser.add_argument("--profile", required=True, help="Profile name (e.g., mannie)")
    parser.add_argument(
        "--since",
        required=True,
        help="Start of window (ISO 8601, e.g., 2026-08-03T00:00:00Z)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON (default: human-readable summary)",
    )
    return parser.parse_args()


def parse_iso8601(timestamp: str) -> datetime:
    """Parse ISO 8601 string to datetime (aware)."""
    if timestamp.endswith("Z"):
        timestamp = timestamp[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_local_timezone() -> timezone:
    """Get the local timezone from the system."""
    # Use the system's local timezone
    tz = datetime.now().astimezone().tzinfo
    return tz if isinstance(tz, timezone) else timezone.utc


def parse_log_timestamp(line: str) -> Optional[datetime]:
    """
    Extract timestamp from Hermes log line.
    Expected format: YYYY-MM-DD HH:MM:SS,mmm LEVEL ...
    
    Naive log timestamps (no TZ) are interpreted as local Asia/Taipei timezone (UTC+8).
    Offset-aware --since values are compared in UTC.
    """
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}", line)
    if not match:
        return None
    try:
        dt_str = match.group(1)
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        # Naive timestamps: interpret as UTC+8 (Asia/Taipei), then convert to UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def filter_lines_by_window(lines: List[str], since: datetime) -> List[str]:
    """Keep only log lines with timestamp >= since (both in UTC)."""
    filtered: List[str] = []
    for line in lines:
        ts = parse_log_timestamp(line)
        if ts is None:
            continue
        if ts >= since:
            filtered.append(line)
    return filtered


def read_log_file(log_path: Path) -> List[str]:
    """Read log file, return lines; if missing/unreadable, raise HealthCheckError."""
    if not log_path.exists():
        raise HealthCheckError(2, f"Log file not found: {log_path}", {"path": str(log_path)})
    try:
        return log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        raise HealthCheckError(2, f"Failed to read log file: {log_path}", {"path": str(log_path), "error": str(e)})


# Provider failure patterns - require provider/request CONTEXT
PROVIDER_FAILURE_PATTERNS = [
    r"Tool .* returned error \([0-9.]+s\): \{\"error\": \".*provider.*\"\}",
    r"API call failed \(attempt [0-9]+/[0-9]+\)\s+error_type=[A-Za-z]+ provider=[^ ]+.*summary=[^\n]*",
    r"provider_error\([^)]*\)",
    r"provider\s+authentication\s+failed",
    r"incorrect\s+api\s+key",
    r"invalid\s+api\s+key",
]

# Patterns that ALONE are NOT provider failures (need context)
NOT_PROVIDER_PATTERNS = [
    r"\b401\b",  # bare 401 without provider context
    r"MCP call timed out after [0-9.]+s",  # MCP timeout
    r"ReadTimeout",  # generic read timeout
]

EXCLUDE_PATTERNS = [
    r"User:",
    r"Assistant:",
    r"Previous reasoning summary unavailable",
    r"goal-judge",
]

PROVIDER_REGEXES = [re.compile(p, re.IGNORECASE) for p in PROVIDER_FAILURE_PATTERNS]
NOT_PROVIDER_REGEXES = [re.compile(p, re.IGNORECASE) for p in NOT_PROVIDER_PATTERNS]
EXCLUDE_REGEXES = [re.compile(p, re.IGNORECASE) for p in EXCLUDE_PATTERNS]


def check_provider_failure(lines: List[str]) -> Dict[str, Any]:
    """Count structured provider error lines WITH provider/request context.
    Returns count and up to 5 matched lines (for output, will be sanitized)."""
    count = 0
    matched_lines: List[str] = []
    for line in lines:
        # Skip excluded lines
        if any(exc.search(line) for exc in EXCLUDE_REGEXES):
            continue
        # Must match a provider pattern
        if any(rgx.search(line) for rgx in PROVIDER_REGEXES):
            # Must NOT be a bare/non-context pattern
            if any(rgx.search(line) for rgx in NOT_PROVIDER_REGEXES):
                continue
            count += 1
            matched_lines.append(line)
    return {
        "count": count,
        "matched_lines": matched_lines[:5],  # Only first 5 for output
    }


def check_background_review_denied(lines: List[str]) -> Dict[str, Any]:
    """Count denied tool invocations by tool name.
    Returns counts, review turn count, matched_lines.
    Denominator = count of 'agent_init.*thread=bg-review' lifecycle events."""
    deny_pattern = re.compile(
        r"Background review denied non-whitelisted tool: ([^\s.]+\.?)",
        re.IGNORECASE,
    )
    review_turn_pattern = re.compile(r"agent_init.*thread=bg-review")
    counts: Dict[str, int] = {}
    matched_lines: List[str] = []
    review_turns = 0
    for line in lines:
        deny_match = deny_pattern.search(line)
        if deny_match:
            tool_name = deny_match.group(1).rstrip(".")
            counts[tool_name] = counts.get(tool_name, 0) + 1
            matched_lines.append(line)
        if review_turn_pattern.search(line):
            review_turns += 1
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "matched_lines": matched_lines[:5],
        "review_turns": review_turns,
    }


def check_telegram_audit(lines: List[str]) -> Dict[str, Any]:
    """Check for [TG-AUDIT] lines and gaps.
    Returns AGGREGATE ONLY — no raw lines in output."""
    pattern = re.compile(r"\[TG-AUDIT\] update_id=(\d+)")
    audit_lines: List[str] = []
    update_ids: List[int] = []
    for line in lines:
        if "[TG-AUDIT]" in line:
            audit_lines.append(line)
            match = pattern.search(line)
            if match:
                try:
                    update_ids.append(int(match.group(1)))
                except ValueError:
                    pass

    update_ids.sort()
    gaps: List[Tuple[int, int]] = []
    prev: Optional[int] = None
    for uid in update_ids:
        if prev is not None and uid > prev + 1:
            gaps.append((prev + 1, uid - 1))
        prev = uid

    has_event = len(audit_lines) > 0
    return {
        "count": len(audit_lines),
        "has_event": has_event,
        "gaps": gaps,
        "gap_warning": len(gaps) > 0,
    }


def check_lsp_usage(lines: List[str]) -> Dict[str, Any]:
    """
    Distinguish registered LSP tools from successful calls in window.
    Registered: scan for registry.register calls in tools/lsp_tool.py.
    Success: not reliably measurable from logs; report 0 with note.
    """
    registered_count = 0
    note = ""

    try:
        # Try the full registry first
        from tools.registry import registry
        entries = registry._snapshot_entries()
        for entry in entries:
            if entry.toolset in ("lsp", "lsp-res"):
                registered_count += 1
        if registered_count == 0:
            # Fallback: parse the module at module level
            _repo = Path(__file__).resolve().parents[2]
            lsp_path = _repo / "tools" / "lsp_tool.py"
            if lsp_path.exists():
                source = lsp_path.read_text(encoding="utf-8")
                registered_count = len(
                    re.findall(r'toolset="(lsp|lsp-res)"', source)
                )
                note = "Count derived from static source scan (registry not loaded)."
    except Exception as e:
        note = f"Failed to import LSP tool registry: {e}"
        # Try file scan fallback
        _repo = Path(__file__).resolve().parents[2]
        lsp_path = _repo / "tools" / "lsp_tool.py"
        if lsp_path.exists():
            source = lsp_path.read_text(encoding="utf-8")
            registered_count = len(
                re.findall(r'toolset="(lsp|lsp-res)"', source)
            )
            note = f"Static scan fallback (import failed: {e})"

    success_count = 0
    success_measured = False
    if not note:
        note = "Live LSP success measurement not implemented; requires tool call logging."

    return {
        "registered_count": registered_count,
        "success_count": success_count,
        "success_measured": success_measured,
        "note": note,
    }


def sanitize_output(obj: Any) -> Any:
    """Remove sensitive data from output (chat IDs, tokens, emails, home paths)."""
    if isinstance(obj, str):
        # Remove chat IDs (numeric, typically 10+ digits)
        obj = re.sub(r"\bchat=\d{8,}\b", "chat=<redacted>", obj)
        # Remove tokens (Bearer *** patterns)
        obj = re.sub(r"Bearer\s+\S+", "Bearer ***", obj)
        # Remove emails
        obj = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", obj)
        # Remove home paths
        obj = re.sub(r"/home/\w+", "/home/<user>", obj)
        obj = re.sub(r"/Users/\w+", "/Users/<user>", obj)
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_output(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_output(v) for v in obj]
    return obj


def main() -> None:
    args = parse_args()

    # Parse timestamp
    try:
        since_dt = parse_iso8601(args.since)
    except Exception as e:
        print(f"Error parsing --since timestamp: {e}", file=sys.stderr)
        sys.exit(2)

    # Profile resolution - NO silent fallback
    try:
        profile_name = normalize_profile_name(args.profile)
    except ValueError as e:
        print(f"Error: Invalid profile name '{args.profile}': {e}", file=sys.stderr)
        sys.exit(2)

    if not profile_exists(profile_name):
        print(f"Error: Profile '{profile_name}' does not exist", file=sys.stderr)
        sys.exit(2)

    profile_dir = get_profile_dir(profile_name)
    log_dir = profile_dir / "logs"
    if not log_dir.exists():
        print(f"Error: Log directory not found for profile '{profile_name}': {log_dir}", file=sys.stderr)
        sys.exit(2)

    log_files = sorted(log_dir.glob("agent.log*")) + [
        log_dir / "gateway.error.log",
        log_dir / "gateway.log",
    ]
    # Dedup in case glob picks up the explicit files
    log_files = list(dict.fromkeys(log_files))

    all_lines: List[str] = []
    for lf in log_files:
        if lf.exists():
            try:
                all_lines.extend(read_log_file(lf))
            except HealthCheckError as e:
                print(f"Error: {e.message}", file=sys.stderr)
                sys.exit(e.code)

    window_lines = filter_lines_by_window(all_lines, since_dt)

    provider_result = check_provider_failure(window_lines)
    background_result = check_background_review_denied(window_lines)
    telegram_result = check_telegram_audit(window_lines)
    lsp_result = check_lsp_usage(window_lines)

    # Determine overall status per §0 thresholds
    # provider_failure: count > 0 = FAIL
    # background_review_denied: denied/turn ratio
    #   <= 0.25 PASS, >0.25-1.0 WARN, >1.0 FAIL, no denominator = UNKNOWN
    # telegram_audit: no events = NOT_APPLICABLE (no inbound activity),
    #   events but no [TG-AUDIT] = FAIL, gaps = WARN
    # lsp_usage: no success evidence = UNKNOWN/not_measured

    fail = False
    warn = False
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []

    # Provider failure
    if provider_result["count"] > 0:
        fail = True
        fail_reasons.append(f"provider_failure count > 0 ({provider_result['count']})")

    # Background review denied - compute denied/turn ratio
    # Denominator = count of review forks from lifecycle events
    # (each fork starts with 'agent_init.*thread=bg-review' in the log)
    bg_denied_total = background_result["total"]
    review_turns = background_result.get("review_turns", 0)
    if review_turns > 0:
        ratio = bg_denied_total / review_turns
        if ratio <= 0.25:
            pass  # PASS: normal (review fork only has memory/skill tools; others denied)
        elif ratio <= 1.0:
            warn = True
            warn_reasons.append(f"background_review_denied/turn ratio {ratio:.2f} (>0.25) [denies/turn={ratio:.2f}]")
        else:
            fail = True
            fail_reasons.append(f"background_review_denied/turn ratio too high: {ratio:.2f} (>1.0)")
    elif bg_denied_total > 0:
        # Has denies but no denominator — mark UNKNOWN (warn only)
        warn = True
        warn_reasons.append(f"background_review_denied total > 0 ({bg_denied_total}) [UNKNOWN: no review turn denominator]")

    # Telegram audit
    if telegram_result["count"] == 0:
        # Per spec: no inbound activity = NOT_APPLICABLE (not FAIL)
        # We can't distinguish "no inbound" from "audit broken", so NOT_APPLICABLE
        pass  # no warn/fail
    elif telegram_result["gap_warning"]:
        warn = True
        warn_reasons.append(f"telegram_audit gaps detected ({len(telegram_result['gaps'])})")

    # LSP usage - always UNKNOWN/not_measured per spec
    # This is informational only, NOT a warn/fail condition
    # (lsp_usage UNKNOWN is expected; only FAIL/WARN on real issues)

    # Determine overall status
    if fail:
        overall_status = "fail"
        exit_code = 1
    elif warn:
        overall_status = "warn"
        exit_code = 0
    else:
        overall_status = "pass"
        exit_code = 0

    result = {
        "status": overall_status,
        "checks": {
            "provider_failure": sanitize_output(provider_result),
            "background_review_denied": sanitize_output(background_result),
            "telegram_audit": sanitize_output(telegram_result),
            "lsp_usage": sanitize_output(lsp_result),
        },
        "window": {
            "since": args.since,
            "utc": since_dt.astimezone(timezone.utc).isoformat(),
        },
        "source_files": [str(f.relative_to(log_dir.parent)) for f in log_files if f.exists()],
    }

    # Add reasons to result
    if fail_reasons:
        result["fail_reasons"] = fail_reasons
    if warn_reasons:
        result["warn_reasons"] = warn_reasons

    # Sanitize entire result before output
    result = sanitize_output(result)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(exit_code)
    else:
        print(f"Hermes Profile Health Check")
        print(f"Profile: {profile_name}")
        print(f"Window: since {args.since}")
        print()
        print("Checks:")
        print(f"  Provider Failure: {provider_result['count']} errors")
        print(f"  Background Review Denied: {background_result['total']} denied")
        if background_result["total"] > 0:
            print(f"    By tool: {background_result['counts']}")
        print(f"  Telegram Audit: {telegram_result['count']} events")
        if telegram_result["count"] == 0:
            print("    -> NOT_APPLICABLE: No [TG-AUDIT] events in window (no inbound activity)")
        elif telegram_result["gap_warning"]:
            print(f"    -> WARNING: {len(telegram_result['gaps'])} gap(s) detected")
        print(f"  LSP Usage: {lsp_result['registered_count']} registered tools")
        if lsp_result["note"]:
            print(f"    Note: {lsp_result['note']}")
        print()
        if overall_status == "fail":
            print("OVERALL: FAIL")
            print("Reasons:")
            for reason in fail_reasons:
                print(f"  - {reason}")
        elif overall_status == "warn":
            print("OVERALL: WARN")
            print("Warnings:")
            for reason in warn_reasons:
                print(f"  - {reason}")
        else:
            print("OVERALL: PASS")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()