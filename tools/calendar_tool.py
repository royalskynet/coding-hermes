"""Google Calendar tool — wraps the `gws` CLI for event CRUD.

Registers three tools under the ``calendar`` toolset:
- ``calendar_agenda`` — list this week's events
- ``calendar_add`` — create a new event
- ``calendar_delete`` — remove an event

Update flow: delete + re-add (CLI has no `events patch` subcommand).
"""

import json
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

_GWS_BIN = shutil.which("gworkspace") or "gworkspace"
_TIMEZONE = "Asia/Taipei"


def _check_calendar() -> bool:
    return shutil.which("gworkspace") is not None


def _run_gws(args: list, timeout: int = 30) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            [_GWS_BIN] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "gws command timed out"}
    except FileNotFoundError:
        return {"error": "gws CLI not found"}

    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        if "invalid_grant" in stderr or "REFRESH_FAILED" in stderr:
            return {
                "error": "google_oauth_expired",
                "instruction": "Google OAuth refresh token 失效。請告訴使用者：在終端跑 `gworkspace --auth-url` 取得授權 URL，於瀏覽器登入同意，再把 redirect URL 中的 code 餵回 `gworkspace --auth-code <CODE>`。",
            }
        return {"error": f"gws failed (exit {result.returncode}): {stderr}"}

    raw = result.stdout.strip()
    if not raw:
        return {"ok": True}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _format_agenda(data: Any) -> str:
    if isinstance(data, dict) and "error" in data:
        return json.dumps(data, ensure_ascii=False)

    events = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    if not events:
        return "本週沒有行程。"

    lines = []
    for ev in events:
        eid = ev.get("id", "?")
        summary = ev.get("summary", "(no title)")
        start = ev.get("start", "?")
        # gworkspace returns flat ISO string; raw API returns {"dateTime": ...}
        if isinstance(start, dict):
            start_time = start.get("dateTime", start.get("date", "?"))
        else:
            start_time = start
        end = ev.get("end", "")
        if isinstance(end, dict):
            end_time = end.get("dateTime", end.get("date", ""))
        else:
            end_time = end
        lines.append(f"[{eid}] {summary} | {start_time} → {end_time}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_agenda(args: Dict[str, Any], **kwargs) -> str:
    data = _run_gws(["calendar", "list"])
    return _format_agenda(data)


def _handle_add(args: Dict[str, Any], **kwargs) -> str:
    summary = args.get("summary", "").strip()
    start = args.get("start", "").strip()
    end = args.get("end", "").strip()

    if not summary or not start or not end:
        return json.dumps({"error": "summary, start, end are all required"})

    cmd = [
        "calendar", "create",
        "--summary", summary,
        "--start", start,
        "--end", end,
    ]

    location = args.get("location", "").strip()
    if location:
        cmd.extend(["--location", location])

    data = _run_gws(cmd)
    if isinstance(data, dict) and "error" in data:
        return json.dumps(data, ensure_ascii=False)

    eid = data.get("id", "?")
    link = data.get("htmlLink", "")
    return f"已建立行程: [{eid}] {summary}\n{link}"


def _handle_delete(args: Dict[str, Any], **kwargs) -> str:
    event_id = args.get("event_id", "").strip()
    if not event_id:
        return json.dumps({"error": "event_id is required"})

    data = _run_gws(["calendar", "delete", event_id])

    if isinstance(data, dict) and "error" in data:
        return json.dumps(data, ensure_ascii=False)

    return f"已刪除行程 [{event_id}]"


# ---------------------------------------------------------------------------
# Schemas (kept minimal for qwen2.5:14b)
# ---------------------------------------------------------------------------

AGENDA_SCHEMA = {
    "name": "calendar_agenda",
    "description": "列出本週行事曆行程",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

ADD_SCHEMA = {
    "name": "calendar_add",
    "description": "新增行事曆行程",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "行程標題",
            },
            "start": {
                "type": "string",
                "description": "開始時間 (ISO 8601, 例: 2026-04-17T14:00:00+08:00)",
            },
            "end": {
                "type": "string",
                "description": "結束時間 (ISO 8601, 例: 2026-04-17T15:00:00+08:00)",
            },
        },
        "required": ["summary", "start", "end"],
    },
}

DELETE_SCHEMA = {
    "name": "calendar_delete",
    "description": "刪除行事曆行程",
    "parameters": {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "行程 ID (從 calendar_agenda 取得)",
            },
        },
        "required": ["event_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

from tools.registry import registry

registry.register(
    name="calendar_agenda",
    toolset="calendar",
    schema=AGENDA_SCHEMA,
    handler=_handle_agenda,
    check_fn=_check_calendar,
    emoji="📅",
)

registry.register(
    name="calendar_add",
    toolset="calendar",
    schema=ADD_SCHEMA,
    handler=_handle_add,
    check_fn=_check_calendar,
    emoji="📅",
)

registry.register(
    name="calendar_delete",
    toolset="calendar",
    schema=DELETE_SCHEMA,
    handler=_handle_delete,
    check_fn=_check_calendar,
    emoji="📅",
)
