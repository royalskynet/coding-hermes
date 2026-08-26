#!/bin/bash
# Mannie healthcheck — ping Agnes every 15min, alert TG on failure
set -e
PROFILE="mannie"
LOG="/tmp/mannie-healthcheck.log"
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

if hermes --profile "$PROFILE" -z "ping" 2>&1 | grep -qiE "pong|繁|zh|中文|好的|可以|OK"; then
    echo "[$TIMESTAMP] OK" >> "$LOG"
    exit 0
else
    echo "[$TIMESTAMP] FAIL" >> "$LOG"
    hermes --profile "$PROFILE" send --to telegram "⚠️ Mannie healthcheck FAILED at $TIMESTAMP" 2>/dev/null
    exit 1
fi
