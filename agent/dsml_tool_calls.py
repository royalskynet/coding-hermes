"""Parser for DeepSeek's DSML-sentinel inline tool-call format.

DeepSeek v4 (observed via OpenRouter, ``api_mode: chat_completions``)
sometimes fails to emit the structured OpenAI ``tool_calls`` field and
instead inlines a pseudo-XML block into ``content`` using a doubled
fullwidth-pipe sentinel:

    <｜｜DSML｜｜tool_calls>
    <｜｜DSML｜｜invoke name="write_file">
    <｜｜DSML｜｜parameter name="path" string="true">/tmp/x</｜｜DSML｜｜parameter name="content" string="true">hello
    </｜｜DSML｜｜parameter>
    </｜｜DSML｜｜invoke>
    </｜｜DSML｜｜tool_calls>

Ground truth for this grammar was extracted from real, non-Telegram-rendered
request dumps (WO-2026-08-29-hermes-dsml-toolcall-parser.md step 1):
``profiles/koko/sessions/request_dump_20260816_221044_*.json`` and
``profiles/koko/sessions/request_dump_20260816_221847_*.json``, verified
byte-for-byte with a Python codepoint dump. This supersedes the single-pipe
example that first surfaced via Telegram HTML rendering (that example was
already corrupted by ``ParseMode.HTML`` swallowing unknown-tag attributes).

Confirmed grammar facts:

  * The sentinel pipe is a DOUBLED fullwidth U+FF5C on each side of the
    literal ``DSML``: ``｜｜DSML｜｜`` (not a single U+FF5C, and not ASCII
    ``|``).
  * A ``tool_calls`` block holds one or more ``invoke`` blocks.
  * Parameters are delimited by tags carrying ``name="..."``. Between two
    parameters the model frequently collapses "close previous / open next"
    into a single tag, e.g. ``</｜｜DSML｜｜parameter name="content"
    string="true">`` — there is no separate opener for that second
    parameter. Only the *last* parameter in an invoke gets a clean,
    attribute-less closer: ``</｜｜DSML｜｜parameter>``.
  * ``string="true"`` means the value is a raw string; anything else is
    attempted as JSON with a raw-string fallback.
"""

from __future__ import annotations

import json
import re

# Cheap ASCII pre-check so callers can skip the unicode regexes entirely on
# the overwhelmingly common case of content with no DSML in it at all.
SENTINEL_HINT = "DSML"

_DSML_OPEN_PREFIX = "<｜｜DSML｜｜"

_TOOL_CALLS_BLOCK_RE = re.compile(
    r"<｜｜DSML｜｜tool_calls>(.*?)</｜｜DSML｜｜tool_calls>", re.DOTALL
)
_INVOKE_RE = re.compile(
    r'<｜｜DSML｜｜invoke\s+name="([^"]*)">(.*?)</｜｜DSML｜｜invoke>', re.DOTALL
)
_PARAM_TAG_RE = re.compile(
    r'<\/?｜｜DSML｜｜parameter(?:\s+name="([^"]*)")?(?:\s+string="([^"]*)")?\s*>'
)


def looks_like_dsml_head(content_so_far: str) -> bool:
    """True once the accumulated stream content contains the DSML sentinel.

    Used by the streaming path to decide when to stop forwarding raw content
    deltas to the user (before the block — and any secrets it might carry,
    e.g. file contents being written — has streamed out). Deliberately a
    substring test, not ``startswith``: the model often emits a sentence of
    prose before opening the block, and that prose must not disable the
    suppression. Takes the *accumulated* content so a sentinel split across
    chunk boundaries still matches.
    """
    return _DSML_OPEN_PREFIX in content_so_far


def _parse_invoke_body(body: str) -> dict:
    """Split an invoke body into ``{param_name: value}`` using tag
    boundaries.

    Every parameter tag that carries a ``name="..."`` attribute — whether
    it's a clean opener ``<...parameter name=X>`` or a merged
    closer/opener ``</...parameter name=X>`` — starts a new value; the
    value runs until the next tag. A trailing attribute-less closer
    ``</...parameter>`` just ends the last value without starting a new one.
    """
    args: dict = {}
    tags = list(_PARAM_TAG_RE.finditer(body))
    for idx, tag in enumerate(tags):
        name = tag.group(1)
        if not name:
            continue
        is_raw_string = tag.group(2) == "true"
        value_start = tag.end()
        value_end = tags[idx + 1].start() if idx + 1 < len(tags) else len(body)
        raw_value = body[value_start:value_end]
        if is_raw_string:
            args[name] = raw_value
        else:
            try:
                args[name] = json.loads(raw_value.strip())
            except (json.JSONDecodeError, ValueError):
                args[name] = raw_value
    return args


def parse_dsml_tool_calls(content: str) -> tuple[str, list[dict]]:
    """Extract DeepSeek DSML-sentinel tool calls out of ``content``.

    Returns ``(remaining_content, tool_calls)``. Each tool call looks like
    ``{"name": str, "arguments": dict}``.

    Only fully closed ``<...tool_calls>...</...tool_calls>`` blocks are
    parsed and removed from the returned content. A block whose closing
    sentinel never arrived (truncated stream, model cut off mid-emission)
    is left untouched in ``remaining_content`` so the tag-stripper safety
    net (``agent.agent_runtime_helpers.strip_think_blocks``) can still
    scrub the raw sentinel before it reaches a user.

    This function never raises. Malformed/unparseable invokes are simply
    dropped (no ``name`` attribute, or nested inside a block that itself
    never closed).
    """
    if SENTINEL_HINT not in content:
        return content, []

    tool_calls: list[dict] = []

    def _consume_block(match: "re.Match[str]") -> str:
        body = match.group(1)
        for invoke_match in _INVOKE_RE.finditer(body):
            name = invoke_match.group(1)
            if not name:
                continue
            arguments = _parse_invoke_body(invoke_match.group(2))
            tool_calls.append({"name": name, "arguments": arguments})
        return ""

    remaining = _TOOL_CALLS_BLOCK_RE.sub(_consume_block, content)
    return remaining, tool_calls
