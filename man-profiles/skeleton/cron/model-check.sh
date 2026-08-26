#!/bin/bash
# Check OmniRoute models hourly
set -e
OMNIURL="http://127.0.0.1:20128/v1/models"
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

MODELS=$(curl -s "$OMNIURL" 2>/dev/null | python3 -c "
import json,sys
data=json.load(sys.stdin)
ids=[m['id'] for m in data.get('data',[])]
print('|'.join(ids))
" 2>/dev/null)

if echo "$MODELS" | grep -q "free-tools-heavy"; then
    echo "[$TIMESTAMP] model-check PASS: free-tools-heavy found"
    exit 0
else
    echo "[$TIMESTAMP] model-check FAIL: free-tools-heavy missing"
    hermes --profile mannie send --to telegram "⚠️ Mannie model-check FAIL: free-tools-heavy missing at $TIMESTAMP" 2>/dev/null
    exit 1
fi
