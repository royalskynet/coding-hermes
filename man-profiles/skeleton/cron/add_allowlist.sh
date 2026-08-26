#!/bin/bash
# Add chat_id to mannie allowed_chats
set -e
CHAT_ID="$1"
CONFIG="$HOME/.hermes/profiles/mannie/config.yaml"

if [ -z "$CHAT_ID" ]; then
    echo "Usage: add_allowlist.sh <chat_id>"
    exit 1
fi

if grep -q "$CHAT_ID" "$CONFIG"; then
    echo "Chat ID $CHAT_ID already in allowed_chats"
    exit 0
fi

python3 -c "
import sys
target = '$CHAT_ID'
cfg = '$CONFIG'
with open(cfg) as f: lines = f.readlines()
for i, line in enumerate(lines):
    if line.strip().startswith('allowed_chats:'):
        lines.insert(i+1, '  - ' + target + '\n')
        break
with open(cfg, 'w') as f: f.writelines(lines)
print('ADDED ' + target)
"
echo "Added $CHAT_ID to allowed_chats. Restart gateway: hermes --profile mannie gateway restart"
