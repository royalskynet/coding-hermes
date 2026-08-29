"""Streaming path must redact credentials before they reach a chat surface.

Regression for the 2026-08-29 leak: a streamed turn sets ``already_sent=True``,
which makes ``gateway/run.py`` discard the copy that
``_sanitize_gateway_final_response`` scrubbed — so the streaming path had no
redaction at all. All tokens below are fabricated.
"""

from gateway.stream_consumer import GatewayStreamConsumer

FAKE_GH = "ghp_FAKEFAKEFAKE1234567890"
clean = GatewayStreamConsumer._clean_for_display


def test_plain_text_is_untouched():
    text = "一般回覆，沒有秘密。"
    assert clean(text) == text


def test_github_token_is_redacted():
    out = clean(f"here is the key: {FAKE_GH} ok")
    assert FAKE_GH not in out


def test_env_assignment_is_redacted():
    out = clean("API_KEY=abcdefghijklmnop123456")
    assert "abcdefghijklmnop123456" not in out


def test_token_split_across_deltas_is_still_redacted():
    """The real chunk-boundary case.

    Redacting a lone delta would match nothing on either half. These sites
    are fed *accumulated* text, so by the time a send happens the token is
    whole — this test pins that contract.
    """
    a, b = FAKE_GH[:9], FAKE_GH[9:]
    assert FAKE_GH not in clean(a)  # partial prefix alone carries no secret
    assert FAKE_GH not in clean(a + b)  # accumulated form must be scrubbed


def test_media_directive_stripping_still_works():
    assert "MEDIA:" not in clean("看這個 MEDIA:/tmp/x.png 圖")
