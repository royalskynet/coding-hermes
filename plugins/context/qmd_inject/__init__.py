"""Koko qmd vault context injection plugin."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("qmd_inject")

QMD_BIN = "/Users/51mini/.bun/bin/qmd"
META_PREFIX = "qmd_inject"
MAX_HITS = 3
SNIPPET_LIMIT = 300
CONTEXT_LIMIT = 1200
VSEARCH_MIN_SCORE = 0.4
TOGGLE_ON_CONTEXT = (
    "[qmd] 持續注入模式已開啟。請只向使用者簡短確認 qmd 模式已開啟，不要做其他事。"
)
TOGGLE_OFF_CONTEXT = (
    "[qmd] 持續注入模式已關閉。請只向使用者簡短確認 qmd 模式已關閉，不要做其他事。"
)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)


def pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    started = time.perf_counter()
    if not _is_koko_profile():
        return None

    message = str(kwargs.get("user_message") or "")
    stripped = message.strip()
    lowered = stripped.lower()
    platform = str(kwargs.get("platform") or "unknown").strip() or "unknown"
    sender_id = str(kwargs.get("sender_id") or "unknown").strip() or "unknown"
    meta_key = _meta_key(platform, sender_id)

    if lowered == "qmd on":
        _set_meta(meta_key, "on")
        _log("toggle", "", 0, started)
        return {"context": TOGGLE_ON_CONTEXT}

    if lowered == "qmd off":
        _set_meta(meta_key, "off")
        _log("toggle", "", 0, started)
        return {"context": TOGGLE_OFF_CONTEXT}

    mode = ""
    query = ""
    if lowered.startswith("qmd "):
        mode = "single"
        query = stripped[4:].strip()
    elif _get_meta(meta_key) == "on":
        mode = "persistent"
        query = stripped
    else:
        return None

    if not query:
        _log(mode, query, 0, started)
        return None

    hits = _retrieve(query)
    _log(mode, query, len(hits), started)
    if not hits:
        return None
    return {"context": _format_context(hits)}


def _is_koko_profile() -> bool:
    for name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        if os.environ.get(name, "").strip().lower() == "koko":
            return True

    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home).expanduser().name.lower() == "koko"

    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name().lower() == "koko"
    except Exception:
        return False


def _meta_key(platform: str, sender_id: str) -> str:
    return f"{META_PREFIX}:{platform}:{sender_id}"


def _get_db() -> Any:
    from hermes_state import SessionDB

    return SessionDB()


def _get_meta(key: str) -> str | None:
    try:
        db = _get_db()
        try:
            return db.get_meta(key)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        logger.info("qmd_inject meta read failed: %s", exc)
        return None


def _set_meta(key: str, value: str) -> None:
    try:
        db = _get_db()
        try:
            db.set_meta(key, value)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        logger.info("qmd_inject meta write failed: %s", exc)


def _retrieve(query: str) -> list[dict[str, Any]]:
    hits = _run_qmd(["search", query, "--json", "-n", str(MAX_HITS)], timeout=5)
    if hits:
        return hits[:MAX_HITS]

    hits = _run_qmd(["vsearch", query, "--json", "-n", str(MAX_HITS)], timeout=15)
    return [
        hit for hit in hits
        if _score(hit) is None or _score(hit) >= VSEARCH_MIN_SCORE
    ][:MAX_HITS]


def _run_qmd(args: list[str], timeout: int) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            [QMD_BIN, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        logger.info("qmd_inject qmd command failed: %s", exc)
        return []

    if proc.returncode != 0:
        return []
    data = _parse_json_array(proc.stdout)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _parse_json_array(output: str) -> Any:
    text = (output or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("\n[")
        if start >= 0:
            text = text[start + 1 :]
        elif "[" in text:
            text = text[text.find("[") :]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def _score(hit: dict[str, Any]) -> float | None:
    try:
        return float(hit.get("score"))
    except (TypeError, ValueError):
        return None


def _format_context(hits: list[dict[str, Any]]) -> str:
    parts = ["以下為 vault 檢索參考，與使用者問題無關就忽略："]
    for idx, hit in enumerate(hits[:MAX_HITS], start=1):
        title = _clean(str(hit.get("title") or _page_name(str(hit.get("file") or ""))))
        file_name = _page_name(str(hit.get("file") or ""))
        source = title if title == file_name or not file_name else f"{title} ({file_name})"
        snippet = _clean(str(hit.get("snippet") or ""))[:SNIPPET_LIMIT]
        if snippet:
            parts.append(f"{idx}. 出處：{source}\n{snippet}")
        else:
            parts.append(f"{idx}. 出處：{source}")
    return "\n\n".join(parts)[:CONTEXT_LIMIT]


def _page_name(path: str) -> str:
    if not path:
        return "unknown"
    return Path(path.removeprefix("qmd://")).name or path


def _clean(text: str) -> str:
    return " ".join(text.replace("\r", " ").split())


def _log(mode: str, query: str, hits: int, started: float) -> None:
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "mode=%s query=%r hits=%d elapsed_ms=%d",
        mode,
        query[:30],
        hits,
        elapsed_ms,
    )
