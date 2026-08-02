#!/usr/bin/env python3
"""LSP Symbol Tools — code navigation via Language Server Protocol.

Exposes four tools that let the agent query symbol trees, definitions,
references, and workspace-wide symbol searches without reading entire files.
All tools route through the singleton :class:`agent.lsp.manager.LSPService`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from tools.registry import registry
from tools.tool_output_limits import get_max_bytes, get_max_lines

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _trim_to_limits(text: str) -> str:
    """Trim *text* to tool_output limits (bytes + lines), appending a hint."""
    max_bytes = get_max_bytes()
    max_lines = get_max_lines()
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        kept = lines[:max_lines]
        return "\n".join(kept) + f"\n... [truncated: {len(lines)} lines → {max_lines}]"
    # Byte-truncate and re-decode
    truncated = raw[:max_bytes]
    text_trunc = truncated.decode("utf-8", errors="replace")
    lines = text_trunc.splitlines()
    if len(lines) > max_lines:
        text_trunc = "\n".join(lines[:max_lines])
    return text_trunc + f"\n... [truncated: output exceeds {max_bytes} bytes]"


def _format_symbols(symbols: List[Dict[str, Any]], indent: int = 0) -> str:
    """Render DocumentSymbol tree in flat Markdown list."""
    lines: List[str] = []
    prefix = "  " * indent
    kind_map: Dict[int, str] = {
        1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
        6: "Method", 7: "Property", 8: "Field", 9: "Constructor",
        10: "Enum", 11: "Interface", 12: "Function", 13: "Variable",
        14: "Constant", 15: "String", 16: "Number", 17: "Boolean",
        18: "Array", 19: "Object", 20: "Key", 21: "Null",
        22: "EnumMember", 23: "Struct", 24: "Event", 25: "Operator",
        26: "TypeParameter",
    }
    for s in symbols:
        kind = s.get("kind", 0)
        kind_str = ""
        if isinstance(kind, int) and kind in kind_map:
            kind_str = kind_map[kind]
        name = s.get("name", "?")
        detail = s.get("detail", "")
        rng = s.get("range", {})
        start = rng.get("start", {})
        line = start.get("line", -1) + 1 if isinstance(start, dict) else "?"
        entry = f"{prefix}- `{name}` ({kind_str}, L{line})"
        if detail:
            entry += f" — {detail}"
        lines.append(entry)
        children = s.get("children")
        if isinstance(children, list) and children:
            lines.append(_format_symbols(children, indent + 1))
    return "\n".join(lines)


def _get_service():
    """Return the singleton LSPService or None."""
    try:
        from agent.lsp import get_service
        svc = get_service()
        if svc is not None and svc.is_active():
            return svc
    except Exception as e:
        logger.debug("LSP service unavailable: %s", e)
    return None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def tool_lsp_document_symbols(args: dict) -> dict:
    """Return the symbol tree for a single file.

    Use this INSTEAD of read_file when you want to understand the
    structure of a file — class/method/function names with line numbers.
    REPLACES the need to read an entire file to grasp its layout.
    """
    path = str(args.get("path") or "")
    if not path:
        return {"error": "path is required"}
    svc = _get_service()
    if svc is None:
        return {"error": "LSP service is not active"}
    symbols = svc.document_symbols_sync(path)
    if not symbols:
        return {"content": f"(no symbols returned for {path})", "file": path, "symbol_count": 0}
    text = _format_symbols(symbols)
    text = _trim_to_limits(text)
    return {
        "content": text,
        "file": path,
        "symbol_count": len(symbols),
    }


def tool_lsp_definition(args: dict) -> dict:
    """Jump to the definition of the symbol at (line, character) in a file.

    Line is 0-based (first line = 0). Use this when you need to find where a
    symbol is defined — the result is a compact file:line list, not entire files.
    """
    path = args.get("path", "")
    line = args.get("line", 0)
    character = args.get("character", 0)
    if not path:
        return {"error": "path is required"}
    svc = _get_service()
    if svc is None:
        return {"error": "LSP service is not active"}
    results = svc.definition_sync(path, line, character)
    if not results:
        return {"content": f"(no definition found at {path}:{line})", "locations": []}
    lines: List[str] = []
    for loc in results:
        f = loc.get("file", "")
        rng = loc.get("range", {})
        start = rng.get("start", {})
        l0 = start.get("line", -1) if isinstance(start, dict) else "?"
        if isinstance(l0, int):
            l0 = f"L{l0 + 1}"
        lines.append(f"{f}:{l0}")
    text = "\n".join(lines)
    text = _trim_to_limits(text)
    return {"content": text, "locations": results}


def tool_lsp_references(args: dict) -> dict:
    """Find all references to the symbol at (line, character) in a file.

    line is 0-indexed. Include include_declaration=true to include the
    declaration site. Returns a compact file:line list with one-line context.
    """
    path = args.get("path", "")
    line = args.get("line", 0)
    character = args.get("character", 0)
    include_declaration = args.get("include_declaration", False)
    if not path:
        return {"error": "path is required"}
    svc = _get_service()
    if svc is None:
        return {"error": "LSP service is not active"}
    results = svc.references_sync(path, line, character, include_declaration=include_declaration)
    if not results:
        return {"content": f"(no references found for {path}:{line})", "locations": []}
    lines_out: List[str] = []
    for loc in results:
        f = loc.get("file", "")
        rng = loc.get("range", {})
        start = rng.get("start", {})
        l0 = start.get("line", -1) if isinstance(start, dict) else "?"
        if isinstance(l0, int):
            l0 = f"L{l0 + 1}"
        lines_out.append(f"{f}:{l0}")
    text = "\n".join(lines_out)
    text = _trim_to_limits(text)
    return {"content": text, "locations": results, "count": len(results)}


def tool_lsp_workspace_symbols(args: dict) -> dict:
    """Search the entire workspace for symbols matching a query string.

    Use this INSTEAD of search_files when looking for code symbols
    (function names, class names, etc.) — it returns precise symbol info
    with line locations, not raw text matches.
    """
    query = args.get("query", "")
    if not query:
        return {"error": "query is required"}
    svc = _get_service()
    if svc is None:
        return {"error": "LSP service is not active"}
    results = svc.workspace_symbols_sync(query)
    if not results:
        return {"content": f"(no workspace symbols match '{query}')", "locations": [], "count": 0}
    lines_out: List[str] = []
    for r in results:
        f = r.get("file", "")
        name = r.get("name", "?")
        rng = r.get("range", {})
        start = rng.get("start", {})
        l0 = start.get("line", -1) if isinstance(start, dict) else "?"
        if isinstance(l0, int):
            l0 = f"L{l0 + 1}"
        lines_out.append(f"`{name}` — {f}:{l0}")
    text = "\n".join(lines_out)
    text = _trim_to_limits(text)
    return {"content": text, "count": len(results)}


# ---------------------------------------------------------------------------
# Check fn: LSP must be active
# ---------------------------------------------------------------------------


def _check_lsp_active() -> bool:
    try:
        from agent.lsp import get_service
        svc = get_service()
        return svc is not None and svc.is_active()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

LSP_DOCUMENT_SYMBOLS_SCHEMA = {
    "name": "lsp_document_symbols",
    "description": (
        "Read the symbol tree of a single source file via the Language Server Protocol. "
        "Returns class/function/method NAME + KIND + LINE — use this INSTEAD of read_file "
        "when you want to understand a file's **structure** without reading all its lines. "
        "Use it whenever that's all you need, because the output is much smaller than the "
        "full file. Returns hierarchical DocumentSymbol nodes or flat SymbolInformation "
        "if the server doesn't support hierarchy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the source file to inspect.",
            },
        },
        "required": ["path"],
    },
}

LSP_DEFINITION_SCHEMA = {
    "name": "lsp_definition",
    "description": (
        "Find the definition of the symbol at (line, character) in a source file. "
        "line is 0-indexed. Returns a compact list of file:line locations — "
        "NOT whole files. Use this when you're reading code and need to jump "
        "to where a symbol is defined."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file containing the symbol.",
            },
            "line": {
                "type": "integer",
                "description": "0-indexed line number of the symbol.",
            },
            "character": {
                "type": "integer",
                "description": "0-indexed character offset within the line.",
            },
        },
        "required": ["path", "line", "character"],
    },
}

LSP_REFERENCES_SCHEMA = {
    "name": "lsp_references",
    "description": (
        "Find all references to the symbol at (line, character) in a source file. "
        "line is 0-indexed. Returns a compact file:line list with a per-location "
        "one-line context. Set include_declaration=true to include the symbol's own declaration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file containing the symbol.",
            },
            "line": {
                "type": "integer",
                "description": "0-indexed line number of the symbol.",
            },
            "character": {
                "type": "integer",
                "description": "0-indexed character offset within the line.",
            },
            "include_declaration": {
                "type": "boolean",
                "description": "Include the declaration site in results (default false).",
            },
        },
        "required": ["path", "line", "character"],
    },
}

LSP_WORKSPACE_SYMBOLS_SCHEMA = {
    "name": "lsp_workspace_symbols",
    "description": (
        "Search the workspace for code symbols matching a query. "
        "Use this INSTEAD of searching for when you need to find code-level "
        "symbols (function names, class names, etc.) — it returns structured "
        "symbol information with filename and line location, not raw text matches. "
        "The query is case-sensitive by most LSP servers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Symbol name or partial name to search for.",
            },
        },
        "required": ["query"],
    },
}

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# [local-patch] LSP symbol tools — see fixindex 0030-lsp-symbol-tools-local-patch-reapply.
# Re-apply after hermes upgrade.

registry.register(
    name="lsp_document_symbols",
    toolset="lsp",
    schema=LSP_DOCUMENT_SYMBOLS_SCHEMA,
    handler=tool_lsp_document_symbols,
    check_fn=_check_lsp_active,
    emoji="🧭",
)

registry.register(
    name="lsp_definition",
    toolset="lsp",
    schema=LSP_DEFINITION_SCHEMA,
    handler=tool_lsp_definition,
    check_fn=_check_lsp_active,
    emoji="📍",
)

registry.register(
    name="lsp_references",
    toolset="lsp-res",
    schema=LSP_REFERENCES_SCHEMA,
    handler=tool_lsp_references,
    check_fn=_check_lsp_active,
    emoji="🔗",
)

registry.register(
    name="lsp_workspace_symbols",
    toolset="lsp",
    schema=LSP_WORKSPACE_SYMBOLS_SCHEMA,
    handler=tool_lsp_workspace_symbols,
    check_fn=_check_lsp_active,
    emoji="🔎",
)