"""hermes-memory-store — holographic memory plugin using MemoryProvider interface.

Registers as a MemoryProvider plugin, giving the agent structured fact storage
with entity resolution, trust scoring, and HRR-based compositional retrieval.

Original plugin by dusterbloom (PR #2351), adapted to the MemoryProvider ABC.

Config in $HERMES_HOME/config.yaml (profile-scoped):
  plugins:
    hermes-memory-store:
      db_path: $HERMES_HOME/memory_store.db   # omit to use the default
      auto_extract: false
      default_trust: 0.5
      min_trust_threshold: 0.3
      temporal_decay_half_life: 0
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from utils import is_truthy_value
from .store import MemoryStore
from .retrieval import FactRetriever
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas (unchanged from original PR)
# ---------------------------------------------------------------------------

FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory with algebraic reasoning. "
        "Use alongside the memory tool — memory for always-on context, "
        "fact_store for deep recall and compositional queries.\n\n"
        "ACTIONS (simple → powerful):\n"
        "• add — Store a fact the user would expect you to remember.\n"
        "• search — Keyword lookup ('editor config', 'deploy process').\n"
        "• probe — Entity recall: ALL facts about a person/thing.\n"
        "• related — What connects to an entity? Structural adjacency.\n"
        "• reason — Compositional: facts connected to MULTIPLE entities simultaneously.\n"
        "• contradict — Memory hygiene: find facts making conflicting claims.\n"
        "• update/remove/list — CRUD operations.\n\n"
        "IMPORTANT: Before answering questions about the user, ALWAYS probe or reason first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "search", "probe", "related", "reason", "contradict", "update", "remove", "list"],
            },
            "content": {"type": "string", "description": "Fact content (required for 'add')."},
            "query": {"type": "string", "description": "Search query (required for 'search')."},
            "entity": {"type": "string", "description": "Entity name for 'probe'/'related'."},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names for 'reason'."},
            "fact_id": {"type": "integer", "description": "Fact ID for 'update'/'remove'."},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]},
            "tags": {"type": "string", "description": "Comma-separated tags."},
            "trust_delta": {"type": "number", "description": "Trust adjustment for 'update'."},
            "min_trust": {"type": "number", "description": "Minimum trust filter (default: 0.3)."},
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["action"],
    },
}

FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": (
        "Rate a fact after using it. Mark 'helpful' if accurate, 'unhelpful' if outdated. "
        "This trains the memory — good facts rise, bad facts sink."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["helpful", "unhelpful"]},
            "fact_id": {"type": "integer", "description": "The fact ID to rate."},
        },
        "required": ["action", "fact_id"],
    },
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_plugin_config() -> dict:
    try:
        # Canonical loader: behavioral read now honors the managed-scope
        # overlay + ${VAR} expansion (e.g. an api key template) too.
        from hermes_cli.config import load_config_readonly
        all_config = load_config_readonly()
        return cfg_get(all_config, "plugins", "hermes-memory-store", default={}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HolographicMemoryProvider(MemoryProvider):
    """Holographic memory with structured facts, entity resolution, and HRR retrieval."""

    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._store = None
        self._retriever = None
        self._min_trust = float(self._config.get("min_trust_threshold", 0.3))
        # Prefetch raw-turn injection caps (config: plugins.hermes-memory-store)
        self._prefetch_raw_max = int(self._config.get("prefetch_raw_max", 1))
        self._prefetch_raw_max_age_days = int(self._config.get("prefetch_raw_max_age_days", 30))

    @property
    def name(self) -> str:
        return "holographic"

    def is_available(self) -> bool:
        return True  # SQLite is always available, numpy is optional

    def save_config(self, values, hermes_home):
        """Write config to config.yaml under plugins.hermes-memory-store."""
        from pathlib import Path
        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml
            # Write-back round-trip: raw read is correct (merged defaults
            # must not be persisted back into the user's file).
            from hermes_cli.config import read_user_config_raw
            existing = read_user_config_raw(config_path)
            existing.setdefault("plugins", {})
            existing["plugins"]["hermes-memory-store"] = values
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False)
        except Exception:
            pass

    def get_config_schema(self):
        from hermes_constants import display_hermes_home
        _default_db = f"{display_hermes_home()}/memory_store.db"
        return [
            {"key": "db_path", "description": "SQLite database path", "default": _default_db},
            {"key": "auto_extract", "description": "Auto-extract facts at session end", "default": "false", "choices": ["true", "false"]},
            {"key": "default_trust", "description": "Default trust score for new facts", "default": "0.5"},
            {"key": "hrr_dim", "description": "HRR vector dimensions", "default": "1024"},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        _hermes_home = str(get_hermes_home())
        _default_db = _hermes_home + "/memory_store.db"
        db_path = self._config.get("db_path", _default_db)
        # Expand $HERMES_HOME in user-supplied paths so config values like
        # "$HERMES_HOME/memory_store.db" or "~/.hermes/memory_store.db" both
        # resolve to the active profile's directory.
        if isinstance(db_path, str):
            db_path = db_path.replace("$HERMES_HOME", _hermes_home)
            db_path = db_path.replace("${HERMES_HOME}", _hermes_home)
        default_trust = float(self._config.get("default_trust", 0.5))
        hrr_dim = int(self._config.get("hrr_dim", 1024))
        hrr_weight = float(self._config.get("hrr_weight", 0.3))
        temporal_decay = int(self._config.get("temporal_decay_half_life", 0))

        self._store = MemoryStore(db_path=db_path, default_trust=default_trust, hrr_dim=hrr_dim)
        self._db_path = str(db_path)
        self._retriever = FactRetriever(
            store=self._store,
            temporal_decay_half_life=temporal_decay,
            hrr_weight=hrr_weight,
            hrr_dim=hrr_dim,
        )
        self._session_id = session_id

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            total = self._store._conn.execute(
                "SELECT COUNT(*) FROM facts"
            ).fetchone()[0]
        except Exception:
            total = 0
        if total == 0:
            return (
                "# Holographic Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, preferences, decisions.\n"
                "Use fact_feedback to rate facts after using them (trains trust scores)."
            )
        return (
            f"# Holographic Memory\n"
            f"Active. {total} facts stored with entity resolution and trust scoring.\n"
            f"Use fact_store to search, probe entities, reason across entities, or add facts.\n"
            f"Use fact_feedback to rate facts after using them (trains trust scores)."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Surface memory to the LLM with explicit temporal + source framing.

        Distilled facts and raw conversation turns are presented in separate
        sections with different framing. Raw turns get age-annotated ("3 天前
        Ether 說過") so the LLM never mistakes a quoted utterance for current
        truth — which is how the "你約她週二/週三" subject-flip happened.
        """
        if not self._retriever or not query:
            return ""
        try:
            results = self._retriever.search(query, min_trust=self._min_trust, limit=5)
            distilled, raw = [], []
            for r in results or []:
                if r.get("kind") == "raw_turn" or r.get("category") == "imported_episodic":
                    raw.append(r)
                else:
                    distilled.append(r)

            blocks: list[str] = []

            # Koko-only dream recall: when query mentions dreams, prepend
            # narrative summaries from the dream_log so Koko can answer
            # "你昨晚做了什麼夢" verbatim, without fabrication.
            dream_block = self._koko_dream_block(query)
            if dream_block:
                blocks.append(dream_block)

            # Koko-only milestone recall: surface relationship milestones
            # to create "she remembers me" feel without explicit trigger.
            milestone_block = self._koko_milestone_block(query)
            if milestone_block:
                blocks.append(milestone_block)

            if not results:
                return "\n\n".join(blocks)

            if distilled:
                lines = ["## 蒸餾記憶 (當前可用作真實的人物/關係/狀態)"]
                for r in distilled:
                    trust = r.get("trust_score", r.get("trust", 0))
                    lines.append(f"- [{trust:.1f}] {r.get('content', '')}")
                blocks.append("\n".join(lines))

            if raw:
                # Apply raw-turn injection caps:
                # 1. Skip raw turns older than _prefetch_raw_max_age_days (default 30d)
                # 2. Limit to at most _prefetch_raw_max (default 1) highest-trust entries
                from datetime import datetime
                now = datetime.now()
                max_age = self._prefetch_raw_max_age_days
                aged_raw = []
                for r in raw:
                    created = r.get("created_at")
                    if created:
                        if isinstance(created, str):
                            ts = created.replace("T", " ").split(".")[0]
                            try:
                                dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                dt = None
                        else:
                            dt = created
                        if dt is not None and max_age >= 0:
                            if (now - dt).days > max_age:
                                continue
                    aged_raw.append(r)
                # Sort by trust descending, then limit
                aged_raw.sort(key=lambda r: r.get("trust_score", r.get("trust", 0)), reverse=True)
                aged_raw = aged_raw[: self._prefetch_raw_max]
                if aged_raw:
                    lines = [
                        "## 過往對話片段 (歷史脈絡)",
                        "讀法: 此處 Ether/Iris 的引述屬於當時當下說過的話。主詞照原樣保留,",
                        "時間以括號中「(YYYY-MM-DD)」為準。引用時明說「之前你說過/我說過」,",
                        "讓 Ether 知道你在回憶,而當前要回應的話題以他現在這句為主。",
                    ]
                    for r in aged_raw:
                        trust = r.get("trust_score", r.get("trust", 0))
                        age = self._format_age(r.get("created_at"))
                        lines.append(f"- [{trust:.1f}{age}] {r.get('content', '')}")
                    blocks.append("\n".join(lines))

            return "\n\n".join(blocks)
        except Exception as e:
            logger.debug("Holographic prefetch failed: %s", e)
            return ""

    _DREAM_KEYWORDS = ("夢", "昨晚", "夜裡", "做夢", "夢到", "夢見", "夢境")

    def _koko_dream_block(self, query: str) -> str:
        """Koko-only: surface latest dream narratives when query asks about dreams."""
        db_path = getattr(self, "_db_path", "") or ""
        if not db_path:
            return ""
        # Extract profile name: .../profiles/<name>/memory_store.db
        try:
            from pathlib import Path as _Path
            parts = _Path(db_path).parts
            profile_name = parts[-2] if len(parts) >= 2 else ""
        except Exception:
            return ""
        if profile_name != "koko":
            return ""
        if not any(k in query for k in self._DREAM_KEYWORDS):
            return ""
        try:
            import sqlite3 as _sqlite
            conn = _sqlite.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT started_at, narrative FROM dream_log "
                    "WHERE stage='dream' AND status='ok' AND narrative IS NOT NULL "
                    "ORDER BY started_at DESC LIMIT 3"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return ""
        if not rows:
            return ""
        lines = [
            "## 我的夢境（給你講夢用）",
            "以下是我的夢：夢是我夜裡的自由聯想，幫我連接記憶，不是事實紀錄。我引用它時會說明來源是夢。",
            "讀法: 這是我做的夢，第一人稱。Ether 問「昨晚做什麼夢／你夢到什麼」時直接用這段內容講出來，不要捏造額外細節。",
            "",
        ]
        for started_at, narrative in rows:
            date = (started_at or "")[:10]
            lines.append(f"- ({date}) {narrative}")
        return "\n".join(lines)

    _EMOTION_TRIGGERS = ("累", "想", "還記得", "好久沒", "最近", "謝謝", "辛苦", "難過", "開心", "終於", "好久了", "一直")

    def _koko_milestone_block(self, query: str) -> str:
        """Koko-only: surface relationship milestones on emotion triggers or 5% random."""
        import random
        db_path = getattr(self, "_db_path", "") or ""
        if not db_path:
            return ""
        try:
            from pathlib import Path as _Path
            parts = _Path(db_path).parts
            profile_name = parts[-2] if len(parts) >= 2 else ""
        except Exception:
            return ""
        if profile_name != "koko":
            return ""

        emotion_hit = any(t in query for t in self._EMOTION_TRIGGERS)
        random_hit = random.random() < 0.05  # 5% baseline
        if not emotion_hit and not random_hit:
            return ""

        try:
            from pathlib import Path as _Path
            profile_dir = _Path(db_path).parent
            milestones_path = profile_dir / "memory" / "milestones.jsonl"
            if not milestones_path.exists():
                return ""
            import json as _json
            items = []
            for line in milestones_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(_json.loads(line))
                except Exception:
                    continue
            if not items:
                return ""
            if emotion_hit:
                # Pick most relevant by tag/fact keyword overlap
                q_words = set(query)
                scored = []
                for item in items:
                    score = sum(1 for t in item.get("tags", []) if any(c in query for c in t))
                    score += item.get("weight", 0.5)
                    scored.append((score, item))
                scored.sort(key=lambda x: -x[0])
                chosen = scored[0][1]
            else:
                chosen = random.choice(items)
            fact = chosen.get("fact", "")
            date = chosen.get("date", "")
            if not fact:
                return ""
            return f"（背景記憶：{date} {fact}）"
        except Exception as e:
            logger.debug("milestone block failed: %s", e)
            return ""

    @staticmethod
    def _format_age(created_at) -> str:
        """Return ' · N天前' for a timestamp, or empty string if unparseable."""
        if not created_at:
            return ""
        try:
            from datetime import datetime
            if isinstance(created_at, str):
                # Handle both ISO and SQLite default formats
                ts = created_at.replace("T", " ").split(".")[0]
                dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            else:
                dt = created_at
            days = (datetime.now() - dt).days
            if days <= 0:
                return " · 今天"
            if days == 1:
                return " · 昨天"
            return f" · {days}天前"
        except Exception:
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Holographic memory stores explicit facts via tools, not auto-sync.
        # The on_session_end hook handles auto-extraction if configured.
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "fact_store":
            return self._handle_fact_store(args)
        elif tool_name == "fact_feedback":
            return self._handle_fact_feedback(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # is_truthy_value: the config schema declares auto_extract as a string
        # enum ("false"/"true"), and a plain truthiness check treats the string
        # "false" as enabled (#57682).
        if not is_truthy_value(self._config.get("auto_extract", False)):
            return
        if not self._store or not messages:
            return
        self._auto_extract_facts(messages)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes as facts."""
        if action == "add" and self._store and content:
            try:
                category = "user_pref" if target == "user" else "general"
                self._store.add_fact(content, category=category)
            except Exception as e:
                logger.debug("Holographic memory_write mirror failed: %s", e)

    def shutdown(self) -> None:
        # Release the shared SQLite connection deterministically on the
        # caller's thread. Dropping the reference alone leaves fd finalization
        # to GC, which keeps the connection (and its write lock) alive on a
        # long-running gateway and prolongs the "database is locked" contention
        # this store's shared-connection refcounting is meant to eliminate.
        # close() is idempotent and refcount-guarded, so siblings stay safe.
        if self._store is not None:
            try:
                self._store.close()
            except Exception as e:
                logger.debug("Holographic shutdown close() failed: %s", e)
        self._store = None
        self._retriever = None

    # -- Tool handlers -------------------------------------------------------

    def _handle_fact_store(self, args: dict) -> str:
        try:
            action = args["action"]
            store = self._store
            retriever = self._retriever

            if action == "add":
                fact_id = store.add_fact(
                    args["content"],
                    category=args.get("category", "general"),
                    tags=args.get("tags", ""),
                )
                return json.dumps({"fact_id": fact_id, "status": "added"})

            elif action == "search":
                results = retriever.search(
                    args["query"],
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", self._min_trust)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "probe":
                results = retriever.probe(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "related":
                results = retriever.related(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "reason":
                entities = args.get("entities", [])
                if not entities:
                    return tool_error("reason requires 'entities' list")
                results = retriever.reason(
                    entities,
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "contradict":
                results = retriever.contradict(
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "update":
                updated = store.update_fact(
                    int(args["fact_id"]),
                    content=args.get("content"),
                    trust_delta=float(args["trust_delta"]) if "trust_delta" in args else None,
                    tags=args.get("tags"),
                    category=args.get("category"),
                )
                return json.dumps({"updated": updated})

            elif action == "remove":
                removed = store.remove_fact(int(args["fact_id"]))
                return json.dumps({"removed": removed})

            elif action == "list":
                facts = store.list_facts(
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", 0.0)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"facts": facts, "count": len(facts)})

            else:
                return tool_error(f"Unknown action: {action}")

        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_fact_feedback(self, args: dict) -> str:
        try:
            fact_id = int(args["fact_id"])
            helpful = args["action"] == "helpful"
            result = self._store.record_feedback(fact_id, helpful=helpful)
            return json.dumps(result)
        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return tool_error(str(exc))

    # -- Auto-extraction (on_session_end) ------------------------------------

    def _auto_extract_facts(self, messages: list) -> None:
        # Local import (pattern used in initialize()): the compressor module is
        # heavier than this plugin and is only needed when auto_extract is on.
        from agent.context_compressor import (
            _MERGED_PRIOR_CONTEXT_HEADER,
            _MERGED_SUMMARY_DELIMITER,
            is_compaction_summary_message,
        )

        def _pre_delimiter_user_segment(msg: dict):
            """Return the genuine user text preceding a merged-into-tail
            compaction summary, or None when the whole message is a summary.

            Merge-into-tail messages (agent/context_compressor.py ~3163-3190)
            wrap real prior tail content BEFORE ``_MERGED_SUMMARY_DELIMITER``,
            prefixed with ``_MERGED_PRIOR_CONTEXT_HEADER``, then append the
            generated handoff summary AFTER the delimiter. Dropping the whole
            row (as ``is_compaction_summary_message`` alone would suggest)
            discards that genuine pre-delimiter content too (#57690 review).
            Only the summary suffix must be excluded from harvesting.
            """
            content = msg.get("content", "")
            if not isinstance(content, str) or _MERGED_SUMMARY_DELIMITER not in content:
                return None
            pre = content.split(_MERGED_SUMMARY_DELIMITER, 1)[0]
            if pre.startswith(_MERGED_PRIOR_CONTEXT_HEADER):
                pre = pre[len(_MERGED_PRIOR_CONTEXT_HEADER):]
            pre = pre.strip()
            return pre or None

        _PREF_PATTERNS = [
            re.compile(r'\bI\s+(?:prefer|like|love|use|want|need)\s+(.+)', re.IGNORECASE),
            re.compile(r'\bmy\s+(?:favorite|preferred|default)\s+\w+\s+is\s+(.+)', re.IGNORECASE),
            re.compile(r'\bI\s+(?:always|never|usually)\s+(.+)', re.IGNORECASE),
        ]
        _DECISION_PATTERNS = [
            re.compile(r'\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)', re.IGNORECASE),
            re.compile(r'\bthe\s+project\s+(?:uses|needs|requires)\s+(.+)', re.IGNORECASE),
        ]

        extracted = 0
        for msg in messages:
            if msg.get("role") != "user":
                continue
            # Compaction handoff summaries can be inserted as role="user"
            # messages; their prose reliably matches the decision patterns, so
            # without this guard the compactor's own output is stored as a
            # durable "fact" on every rollover (#57682). A merge-into-tail
            # summary also carries genuine pre-delimiter user content in the
            # SAME row; harvest that segment instead of dropping the whole
            # message (#57690 review).
            pre_delimiter_segment = _pre_delimiter_user_segment(msg)
            if pre_delimiter_segment is not None:
                content = pre_delimiter_segment
            elif is_compaction_summary_message(msg):
                continue
            else:
                content = msg.get("content", "")
            if not isinstance(content, str) or len(content) < 10:
                continue

            for pattern in _PREF_PATTERNS:
                if pattern.search(content):
                    try:
                        self._store.add_fact(content[:400], category="user_pref")
                        extracted += 1
                    except Exception:
                        pass
                    break

            for pattern in _DECISION_PATTERNS:
                if pattern.search(content):
                    try:
                        self._store.add_fact(content[:400], category="project")
                        extracted += 1
                    except Exception:
                        pass
                    break

        if extracted:
            logger.info("Auto-extracted %d facts from conversation", extracted)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the holographic memory provider with the plugin system."""
    config = _load_plugin_config()
    provider = HolographicMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
