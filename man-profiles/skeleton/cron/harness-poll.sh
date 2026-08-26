#!/bin/bash
# Harness poll cron job — runs harness-poll-fts.mjs every 2 min.
# Replaces the disabled launchd plist.
# Requires: node, harness-poll-fts.mjs, strip-proxy on port 20129
# 脫敏：本機絕對路徑改由 $OMNIRUTE_HOME 環境變數注入。
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
OMNIRUTE_HOME="${OMNIRUTE_HOME:-${HOME}/omniroute-free-tools}"
cd "$OMNIRUTE_HOME"
/opt/homebrew/bin/node scripts/harness-poll-fts.mjs --json --doctor >> "$OMNIRUTE_HOME/strip-proxy/logs/harness-poll-cron.log" 2>&1
