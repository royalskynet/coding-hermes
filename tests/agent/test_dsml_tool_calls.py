"""Tests for the DeepSeek DSML inline tool-call parser.

Grammar ground truth: profiles/koko/sessions/request_dump_20260816_2218*.json
(doubled fullwidth U+FF5C sentinel). No real credentials appear here — all
values below are fabricated.
"""

from agent.agent_runtime_helpers import strip_think_blocks
from agent.dsml_tool_calls import looks_like_dsml_head, parse_dsml_tool_calls

OPEN = "<｜｜DSML｜｜"
CLOSE = "</｜｜DSML｜｜"

SIMPLE = (
    f"{OPEN}tool_calls>\n"
    f'{OPEN}invoke name="terminal">\n'
    f'{OPEN}parameter name="command" string="true">echo hi\n'
    f"{CLOSE}parameter>\n"
    f"{CLOSE}invoke>\n"
    f"{CLOSE}tool_calls>"
)

# The merged closer/opener shape the model actually emits between params.
MERGED = (
    f"{OPEN}tool_calls>\n"
    f'{OPEN}invoke name="write_file">\n'
    f'{OPEN}parameter name="path" string="true">/tmp/x.json'
    f'{CLOSE}parameter name="content" string="true">{{"a": 1}}\n'
    f"{CLOSE}parameter>\n"
    f"{CLOSE}invoke>\n"
    f"{CLOSE}tool_calls>"
)


def test_no_dsml_is_passthrough():
    text = "plain answer with <tool_call> lookalike prose"
    assert parse_dsml_tool_calls(text) == (text, [])


def test_single_invoke_single_param():
    remaining, calls = parse_dsml_tool_calls(SIMPLE)
    assert calls == [{"name": "terminal", "arguments": {"command": "echo hi\n"}}]
    assert "DSML" not in remaining


def test_merged_close_open_param_tag():
    _, calls = parse_dsml_tool_calls(MERGED)
    assert len(calls) == 1
    args = calls[0]["arguments"]
    assert args["path"] == "/tmp/x.json"
    # string="true" means raw, so the JSON-looking value stays a string.
    assert args["content"].strip() == '{"a": 1}'


def test_two_invokes_in_one_block():
    body = SIMPLE.replace(f"{CLOSE}tool_calls>", "")
    block = (
        body
        + f'{OPEN}invoke name="read_file">\n'
        + f'{OPEN}parameter name="path" string="true">/tmp/y\n'
        + f"{CLOSE}parameter>\n{CLOSE}invoke>\n{CLOSE}tool_calls>"
    )
    _, calls = parse_dsml_tool_calls(block)
    assert [c["name"] for c in calls] == ["terminal", "read_file"]


def test_prose_around_block_is_preserved():
    remaining, calls = parse_dsml_tool_calls(f"before\n{SIMPLE}\nafter")
    assert calls
    assert "before" in remaining and "after" in remaining
    assert "DSML" not in remaining


def test_unterminated_block_is_not_parsed_but_left_for_stripper():
    truncated = SIMPLE.rsplit(f"{CLOSE}tool_calls>", 1)[0]
    remaining, calls = parse_dsml_tool_calls(truncated)
    assert calls == []
    assert "DSML" in remaining
    # Safety net must still scrub it before it can reach a user.
    assert "DSML" not in strip_think_blocks(None, truncated)


def test_malformed_invoke_is_dropped_not_raised():
    junk = f"{OPEN}tool_calls>{OPEN}invoke>garbage{CLOSE}tool_calls>"
    remaining, calls = parse_dsml_tool_calls(junk)
    assert calls == []
    assert "DSML" not in remaining


def test_closed_block_is_scrubbed_by_stripper_too():
    assert "DSML" not in strip_think_blocks(None, f"hi {SIMPLE} bye")


def test_looks_like_dsml_head():
    assert looks_like_dsml_head(f"{OPEN}tool_calls>")
    # Prose before the block must not disable streaming suppression.
    assert looks_like_dsml_head(f"let me check.\n{OPEN}tool_calls>")
    assert not looks_like_dsml_head("regular text")
