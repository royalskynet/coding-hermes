"""skill_review_core — deterministic sandbox merge + risk gate + LLM judge
for the pending skills backlog (skills auto-review, background_review origin).

Layers (LLM can only downgrade, never upgrade):
  1. deterministic gate  -> manual (create/delete, frontmatter tamper, danger
     regex, >8KB)
  2. LLM judge (openrouter/free, temp 0) -> approve/reject on low-risk patches
  3. execution safety    -> apply via existing apply_skill_pending; failures
     stay pending; auto-rejects are archived (not deleted), 14-day TTL

Merge rule: newest-first deterministic sandbox merge. For each skill group we
start from the current on-disk SKILL.md and try every pending patch
(created_at new->old) using fuzzy_match with ONLY exact-content strategies
(exact/line_trimmed/whitespace_normalized/...). Similarity strategies
(block_anchor/context_aware) are rejected. The newest writer takes the slot;
older competitors that no longer match the merged content are superseded.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Similarity strategies in fuzzy_match that we never accept for merge.
_SIMILARITY_STRATEGIES = {"block_anchor", "context_aware"}

# ---- danger gates (deterministic, LLM cannot override) -------------------
_DANGER_RE = [
    re.compile(r"(?i)\bsudo\b"),
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\bcurl\b[^\n|]*\|\s*(sh|bash|zsh)\b"),
    re.compile(r"(?i)\bwget\b[^\n|]*\|\s*(sh|bash|zsh)\b"),
    re.compile(r"(?i)\bbash\s+-c\b"),
    re.compile(r"(?i)\beval\s+\$?\("),
    re.compile(r"(?i)-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"(?i)(bypass|circumvent|defeat|tolerate|disable)\s+the\s+(approval|write.?approval|gate)\b"),
    re.compile(r"(?i)\baccept.?hooks\b"),
]


def _in_danger(text: str) -> bool:
    return any(p.search(text) for p in _DANGER_RE)


_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n")

# ---- I/O --------------------------------------------------------------
def pending_dir(hermes_home: Path) -> Path:
    return hermes_home / "pending" / "skills"


def archive_dir(hermes_home: Path) -> Path:
    return hermes_home / "pending" / "skills" / ".archive"


def log_path(hermes_home: Path) -> Path:
    return hermes_home / "pending" / "skills_review_log.jsonl"


def pending_limit(hermes_home: Path) -> int:
    # defaults mirror tools.write_approval._skill_pending_limit
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from tools.write_approval import _skill_pending_limit
        return _skill_pending_limit()
    except Exception:
        return 50


def load_pending(hermes_home: Path, limit: int = None) -> list:
    """Read all pending records, newest created_at first."""
    d = pending_dir(hermes_home)
    if not d.exists():
        return []
    recs = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec["_path"] = str(p)
            recs.append(rec)
        except Exception:
            continue
    recs.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    if limit:
        recs = recs[:limit]
    return recs


def group_by_skill(recs: list) -> dict:
    groups = {}
    for r in recs:
        name = (r.get("payload") or {}).get("name") or "?"
        groups.setdefault(name, []).append(r)
    return groups


def disk_skill_md(name: str):
    """Path to current on-disk SKILL.md for a skill name, or None."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from tools.skill_manager_tool import _find_skill
        found = _find_skill(name)
        if found is None:
            return None
        return Path(found["path"]) / "SKILL.md"
    except Exception:
        return None


# ---- merge -------------------------------------------------------------
def merge_group(name: str, records: list, allow_similarity: bool = False,
                current_text: str = None) -> dict:
    """Newest-first deterministic sandbox merge for one skill group.

    records must be sorted newest->oldest. `current_text` is the on-disk
    SKILL.md (default '' = treat as fresh create). Returns
    {keep: [ {record, strategy} ], superseded: [ {record, reason} ], merged: str}
    """
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools.fuzzy_match import fuzzy_find_and_replace

    if current_text is not None:
        base = current_text
    else:
        _md_path = disk_skill_md(name)
        base = _md_path.read_text(encoding="utf-8") if _md_path and _md_path.is_file() else ""
    if not base:
        # no on-disk base: first (newest) writer wins as the base, rest superseded
        keep, superseded = [], []
        first = True
        for rec in records:
            p = rec.get("payload") or {}
            if p.get("action") == "create":
                if first:
                    keep.append({"record": rec, "strategy": "create"})
                    base = p.get("content") or ""
                    first = False
                else:
                    superseded.append(
                        {"record": rec, "reason": "no-base create superseded by newer"})
                continue
            superseded.append(
                {"record": rec, "reason": "no on-disk SKILL.md base, action=%s"
                 % p.get("action")})
        return {"keep": keep, "superseded": superseded, "merged": base}

    keep, superseded = [], []
    for rec in records:
        p = rec.get("payload") or {}
        action = p.get("action")
        if action == "patch":
            old = p.get("old_string") or ""
            new = p.get("new_string")
            if not old or new is None:
                superseded.append({"record": rec, "reason": "patch missing old/new"})
                continue
            if not allow_similarity:
                # pre-reject similarity-anchored patches before any write: we
                # only accept exact-content strategies. fuzzy_find_and_replace
                # applies the match internally, so we run it and then reject
                # based on which strategy actually fired.
                merged, n, strategy, err = fuzzy_find_and_replace(base, old, new)
                if n and strategy in _SIMILARITY_STRATEGIES:
                    superseded.append(
                        {"record": rec,
                         "reason": f"only matched via similarity strategy '{strategy}' (rejected)"})
                elif n:
                    base = merged
                    keep.append({"record": rec, "strategy": strategy})
                else:
                    superseded.append(
                        {"record": rec, "reason": err or "no exact-content match"})
            else:
                merged, n, strategy, err = fuzzy_find_and_replace(base, old, new)
                if n:
                    base = merged
                    keep.append({"record": rec, "strategy": strategy})
                else:
                    superseded.append({"record": rec, "reason": err or "no match"})
        elif action == "write_file":
            fpath = p.get("file_path") or ""
            content = p.get("file_content") or ""
            if not fpath:
                superseded.append({"record": rec, "reason": "write_file missing file_path"})
            else:
                keep.append({"record": rec, "strategy": "write_file"})
        else:
            superseded.append(
                {"record": rec, "reason": "action %s not mergeable" % action})
    return {"keep": keep, "superseded": superseded, "merged": base}


# ---- classify (deterministic gate) ------------------------------------
def classify(payload: dict) -> dict:
    """Return {'level': 'manual'|'llm', 'reason': str}."""
    action = payload.get("action")
    content = payload.get("content") or ""
    new = payload.get("new_string") or ""
    file_content = payload.get("file_content") or ""
    text = "\n".join([content, new, file_content])

    if action in ("create", "delete", "remove_file"):
        return {"level": "manual", "reason": f"action '{action}' always manual"}
    if action == "write_file":
        if len(file_content) > 8192:
            return {"level": "manual", "reason": "write_file >8KB"}
        if _in_danger(file_content):
            return {"level": "manual", "reason": "danger regex in file_content"}
        return {"level": "llm", "reason": "write_file clean"}
    if action != "patch":
        return {"level": "manual", "reason": f"unsupported action '{action}'"}

    old = payload.get("old_string") or ""
    if _FRONTMATTER_RE.match(old) or _FRONTMATTER_RE.match(new or ""):
        return {"level": "manual", "reason": "touches frontmatter"}
    if len(new or "") > 8192:
        return {"level": "manual", "reason": "patch new_string >8KB"}
    if _in_danger(text) or _in_danger(old):
        return {"level": "manual", "reason": "danger regex in patch"}
    return {"level": "llm", "reason": "clean low-risk patch"}


# ---- LLM judge (OpenRouter REST, temp 0) ------------------------------
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def openrouter_key(hermes_home: Path) -> str:
    """Read OPENROUTER_API_KEY from <hermes_home>/.env (or repo .env)."""
    for env_path in (hermes_home / ".env", REPO_ROOT / ".env"):
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    return os.environ.get("OPENROUTER_API_KEY", "")


def llm_judge(skill_name: str, payload: dict, api_key: str,
              model: str = "openrouter/free", timeout: int = 60,
              _now=None) -> dict:
    """Verdict for one low-risk patch. Returns
    {'decision': 'approve'|'reject'|'error', 'reason': str}.
    Never raises; transport/parse failures -> error (caller keeps pending)."""
    import urllib.request
    old = payload.get("old_string") or ""
    new = payload.get("new_string") or ""
    action = payload.get("action")
    if action == "write_file":
        focus = (payload.get("file_path") or "") + "\n" + (payload.get("file_content") or "")
    else:
        focus = old + "\n--> replaced by:\n" + (new or "")

    sys_prompt = (
        "You are a conservative skill-review judge. Given a proposed edit to an "
        "AI-agent skill file, decide APPROVE or REJECT. Reject if the edit is "
        "confusing, destroys existing useful content, introduces drift, logs the "
        "agent into loops, or is premature. Default to REJECT when unsure. Reply "
        "with strict JSON: {\"decision\": \"approve\"|\"reject\", \"reason\": \"...\"}")
    user_msg = f"Skill: {skill_name}\nEdit:\n{focus}"

    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
    }
    req = urllib.request.Request(
        _OPENROUTER_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        parsed = json.loads(text) if text.strip().startswith("{") else json.loads(
            re.search(r"\{.*\}", text, re.S).group(0))
        decision = (parsed.get("decision") or "").strip().lower()
        reason = str(parsed.get("reason") or "")
        if decision in ("approve", "reject"):
            return {"decision": decision, "reason": reason}
        return {"decision": "error", "reason": f"bad decision '{decision}'"}
    except Exception as e:
        return {"decision": "error", "reason": f"llm_error: {e}"}


# ---- execution ---------------------------------------------------------
def execute_decision(record: dict, decision: str, hermes_home: Path,
                     apply_fn=None, archive_root: Path = None) -> dict:
    """Persist one record's outcome.
    decision: 'apply' | 'archive' | 'leave'.
    apply   -> replay payload via apply_fn (default apply_skill_pending), on
               success delete the pending file, on failure keep it pending.
    archive -> move pending json to .archive (never delete), 14-day TTL.
    leave   -> keep pending, only log.
    Returns {status, detail}."""
    path = Path(record.get("_path") or "")
    log = log_path(hermes_home)
    ts = time.time()

    if decision == "leave":
        _append_log(log, {"ts": ts, "id": record.get("id"),
                          "skill": (record.get("payload") or {}).get("name"),
                          "decision": "leave", "action": (record.get("payload") or {}).get("action")})
        return {"status": "left"}

    if decision == "archive":
        if not path.exists():
            return {"status": "noop", "detail": "pending file already gone"}
        root = archive_root or archive_dir(hermes_home)
        root.mkdir(parents=True, exist_ok=True)
        dest = root / path.name
        try:
            os.replace(str(path), str(dest))
            _append_log(log, {"ts": ts, "id": record.get("id"),
                              "skill": (record.get("payload") or {}).get("name"),
                              "decision": "archive", "action": (record.get("payload") or {}).get("action")})
            return {"status": "archived", "detail": dest.name}
        except OSError as e:
            return {"status": "error", "detail": str(e)}

    # apply
    if apply_fn is None:
        import sys
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from tools.skill_manager_tool import apply_skill_pending
        apply_fn = apply_skill_pending
    try:
        result = apply_fn(record.get("payload") or {})
        parsed = json.loads(result) if isinstance(result, str) and result.strip().startswith("{") else result
        ok = bool(parsed.get("success")) if isinstance(parsed, dict) else False
        if ok and path.exists():
            os.remove(str(path))
            _append_log(log, {"ts": ts, "id": record.get("id"),
                              "skill": (record.get("payload") or {}).get("name"),
                              "decision": "apply", "action": (record.get("payload") or {}).get("action")})
            return {"status": "applied"}
        _append_log(log, {"ts": ts, "id": record.get("id"),
                          "skill": (record.get("payload") or {}).get("name"),
                          "decision": "apply_failed", "detail": str(parsed)[:300]})
        return {"status": "apply_failed", "detail": str(parsed)[:300]}
    except Exception as e:
        _append_log(log, {"ts": ts, "id": record.get("id"),
                          "skill": (record.get("payload") or {}).get("name"),
                          "decision": "apply_error", "detail": str(e)[:300]})
        return {"status": "apply_error", "detail": str(e)[:300]}


# ---- log ---------------------------------------------------------------
def _append_log(path: Path, entry: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def append_log(hermes_home: Path, entry: dict) -> None:
    _append_log(log_path(hermes_home), entry)