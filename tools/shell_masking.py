"""Shell heredoc-body masking shared by the gateway-lifecycle and hardline guards.

Both ``cron/lifecycle_guard.py`` and ``tools/approval.py`` scan raw shell
command text for dangerous keywords (``hermes gateway restart``, ``rm -rf /``,
etc.).  A heredoc body is *data*, not code: the shell feeds every line between
``<<DELIM`` and a line equal to ``DELIM`` to the command's stdin, without ever
interpreting it as commands.  Yet both guards historically scanned the body as
if it were executable text, so a perfectly benign

    cat <<'EOF' > /tmp/notes.txt
    hermes gateway restart got blocked
    EOF

was rejected as if the user had actually asked to restart the gateway.

This module provides ``_mask_heredoc_bodies()``, a detection-only rewrite that
replaces every heredoc body with equal-length spaces (preserving line counts
and total length so downstream length-sensitive logic is unaffected).  The two
guards import it from here so they share one copy of the scan logic instead of
maintaining divergent duplicates.

Quote-awareness
---------------
The scan is a char-level *state machine*, not a regex, so it can tell whether a
``<<`` sits inside a quoted string (``echo "use <<EOF here"``) where it is
literal data and must NOT start a heredoc.  Quote rules match the two existing
guards' tokenizers (``approval._mask_quoted_newlines`` and
``approval._iter_shell_command_starts``): single quotes are literal until the
closing quote, and inside double quotes a backslash escapes the next character.

Safe-by-default
---------------
The bias is toward *not* masking.  If the input is malformed (unclosed quote,
no terminating delimiter, an opaque ``$(``/backtick region) the heredoc body is
left intact rather than guessed at — failing closed keeps the original scan
authority in force.
"""

from __future__ import annotations

__all__ = ["_mask_heredoc_bodies"]


def _mask_heredoc_bodies(command: str) -> str:
    """Blank the bodies of unquoted here-documents in *command*.

    A here-document start is ``<<`` (optionally ``<<-`` tab-strip or ``<<~``)
    appearing outside any quote, followed by a delimiter word.  Everything
    from the line after the ``<<DELIM`` line up to (and including) a line
    whose first token equals the delimiter is the body — pure stdin data.  Body
    characters (including newlines) are replaced 1:1 with spaces so the
    command's layout, line count and byte length are unchanged.

    The delimiter word may be bare, single-quoted or double-quoted; only its
    unquoted content matters for matching the terminator (the shell strips the
    quotes before comparing).  ``<<-`` additionally lets the terminator be
    indented with leading tabs (stripped before comparison); ``<<~`` (bash)
    behaves like ``<<-`` for matching purposes here.

    Anything that cannot be decided reliably is left unmasked.
    """
    if not command or "<<" not in command:
        return command

    chars = list(command)
    n = len(chars)
    i = 0
    quote: str | None = None
    # Opaque zones: command substitution ``$( ... )`` / ``<(...)`` and
    # backticks `` `...` ``.  Inside them a ``<<`` is ambiguous shell, so we
    # skip to the matching close without masking — failing closed.
    opaque_close: str | None = None
    opaque_start: int | None = None
    to_blank: set[int] = set()

    while i < n:
        ch = chars[i]

        if opaque_start is not None:
            if opaque_close in ("$(", "<("):
                if ch == ")":
                    opaque_start = None
                    opaque_close = None
            elif ch == "`":
                opaque_start = None
                opaque_close = None
            i += 1
            continue

        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            opaque_start = i
            opaque_close = "`"
            i += 1
            continue
        if command.startswith("$(", i) or command.startswith("<(", i):
            opaque_start = i
            opaque_close = "$(" if command.startswith("$(", i) else "<("
            i += 1
            continue

        # Outside quotes: only unquoted `<<` can start a here-document.
        if not (ch == "<" and i + 1 < n and chars[i + 1] == "<"):
            i += 1
            continue

        # Two-phase handling: first collect EVERY `<<` redirect in this
        # simple-command header (so `cat <<A <<'B'` gathers both delimiters in
        # header order), then blank the bodies one FIFO at a time starting
        # after the header's terminating newline.  The old single-shot path
        # blanked the first body and jumped past the header, silently dropping
        # any second `<<` — that let a second heredoc body leak keywords.
        header_delims, body_start = _collect_header_delims(chars, i)
        if not header_delims:
            i += 1  # unparseable — fail closed, keep scanning onward
            continue
        if body_start is None:
            break  # unterminated header — fail closed
        ok = True
        end: int | None = None
        for delim, tab_strip in header_delims:
            end = _find_heredoc_end(chars, body_start, delim, tab_strip)
            if end is None:
                ok = False  # unterminated body — fail closed
                break
            for p in range(body_start, end + 1):
                if p < n and chars[p] != "\n":
                    to_blank.add(p)  # newline preserved, length unchanged
            body_start = end + 1
        if ok:
            assert end is not None
            i = body_start
        else:
            break

    if not to_blank:
        return command
    for p in to_blank:
        if p < n:
            chars[p] = " "
    return "".join(chars)


def _collect_header_delims(
    chars: list[str],
    start: int,
) -> tuple[list[tuple[str, bool]], int | None]:
    """Collect every here-doc redirection in one simple-command header.

    A single header line may redirect stdin twice (``cat <<A <<'B'``).  The
    shell reads the header left->right, so the delimiter order here must match
    the order the bodies are terminated.  We scan from *start* (an unquoted
    ``<<``) to the end of its header line, honouring quotes/opaque zones so a
    quoted ``<<`` inside the same line is not collected, returning the list of
    ``(delimiter, tab_strip)`` pairs and the index just past the header's
    terminating newline (the start of the first body).
    """
    n = len(chars)
    delims: list[tuple[str, bool]] = []
    p = start
    quote: str | None = None
    opaque_close: str | None = None

    while p < n:
        c = chars[p]

        if opaque_close is not None:
            if opaque_close in ("$(", "<("):
                if c == ")":
                    opaque_close = None
            elif c == "`":
                opaque_close = None
            p += 1
            continue
        if quote == "'":
            if c == "'":
                quote = None
            p += 1
            continue
        if quote == '"':
            if c == "\\" and p + 1 < n:
                p += 2
                continue
            if c == '"':
                quote = None
            p += 1
            continue
        if c in ("'", '"'):
            quote = c
            p += 1
            continue
        if c == "\\" and p + 1 < n:
            p += 2
            continue
        if c == "`":
            opaque_close = "`"
            p += 1
            continue
        if c == "$" and p + 1 < n and chars[p + 1] == "(":
            opaque_close = "$("
            p += 1
            continue
        if c == "<" and p + 1 < n and chars[p + 1] == "(":
            opaque_close = "<("
            p += 1
            continue

        if c == "<" and p + 1 < n and chars[p + 1] == "<":
            op_len = 2
            tab_strip = False
            if p + 2 < n and chars[p + 2] in ("-", "~"):
                op_len = 3
                tab_strip = True
            j = p + op_len
            while j < n and chars[j] != "\n" and chars[j].isspace():
                j += 1
            if j >= n:
                return delims, None
            if chars[j] == "\n":
                j += 1  # delimiter on the following line
                while j < n and (chars[j].isspace() or chars[j] == "\n"):
                    j += 1
                if j >= n:
                    return delims, None
            k = j
            while k < n and not chars[k].isspace():
                k += 1
            delim = _unquote_delimiter("".join(chars[j:k]))
            if delim:
                delims.append((delim, tab_strip))
            p = k
            continue

        if c == "\n":
            return delims, p + 1
        p += 1
    return delims, None


def _unquote_delimiter(raw: str) -> str:
    """Return the comparison form of a heredoc delimiter word.

    The shell strips the quotes from a quoted delimiter before using it both as
    the expansion switch and as the literal terminator string, so
    ``<<'EOF'``, ``<<"EOF"`` and ``<<EOF`` all terminate on a line ``EOF``.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _find_heredoc_end(
    chars: list[str],
    body_start: int,
    delim: str,
    tab_strip: bool,
) -> int | None:
    """Return the index of the final char of the terminator line, or None.

    Scans forward line by line from *body_start*.  A line terminates the
    heredoc if, after stripping leading tabs (when *tab_strip*) and trailing
    whitespace, its first word equals *delim*.
    """
    n = len(chars)
    pos = body_start
    while pos < n:
        line_end = pos
        while line_end < n and chars[line_end] != "\n":
            line_end += 1
        start = pos
        if tab_strip:
            while start < line_end and chars[start] == "\t":
                start += 1
        word_end = start
        while word_end < line_end and not chars[word_end].isspace():
            word_end += 1
        word = "".join(chars[start:word_end])
        if word == delim:
            return line_end  # include the terminating newline
        pos = line_end + 1
    return None
