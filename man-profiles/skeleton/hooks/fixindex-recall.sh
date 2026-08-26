#!/bin/bash
# fixindex-recall.sh v2 — Mannie pre_llm_call hook (subprocess-based)
# Reads stdin JSON → searches fixindex via fxsearch.py → injects context if hits found
set -euo pipefail
export FIXINDEX_DIR="${FIXINDEX_DIR:-${HOME}/.claude/projects/<REDACTED>/memory/fixes}"
CACHE="${PROFILE_DIR:-${HOME}/.hermes/profiles/mannie}/cache/fxrecall"
FXSEARCH="${FIXINDEX_REPO:-${HOME}/dev/fixindex}/fxsearch.py"

INPUT=$(python3 -c "import sys; print(sys.stdin.read())" 2>/dev/null || echo "")
[ -z "$INPUT" ] && exit 0

# Parse hook payload
USER_MSG=$(echo "$INPUT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
msg = d.get('extra',{}).get('user_message','') or d.get('user_message','')
print(msg)
" 2>/dev/null || true)

[ -z "$USER_MSG" ] || [ ${#USER_MSG} -lt 4 ] && exit 0

# Throttling
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || true)
if [ -n "$SESSION_ID" ]; then
    mkdir -p "$CACHE"
    CACHE_KEY=$(echo -n "$USER_MSG" | python3 -c "import sys,hashlib; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" 2>/dev/null || true)
    FLAG="$CACHE/$SESSION_ID-$CACHE_KEY"
    [ -f "$FLAG" ] && exit 0
    touch "$FLAG" 2>/dev/null || true
fi

# Run fxsearch
RESULT=$(python3 "$FXSEARCH" --json --limit 3 "$USER_MSG" 2>/dev/null || true)
[ -z "$RESULT" ] && exit 0

TOP=$(echo "$RESULT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
hits = d.get('hits', [])
if not hits:
    sys.exit(0)
lines = []
for h in hits[:3]:
    k = h.get('key','')
    hd = h.get('heading','')[:60]
    lines.append(f'    {k}  {hd}')
msg = '[fixindex] 3 條可能相關的舊帳（用 fixindex show NNNN 展開）：'
print(msg + '\n' + '\n'.join(lines))
" 2>/dev/null || true)

if [ -n "$TOP" ]; then
    TOP_ESC=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$TOP" 2>/dev/null || true)
    echo "{\"context\": $TOP_ESC}"
fi
exit 0
