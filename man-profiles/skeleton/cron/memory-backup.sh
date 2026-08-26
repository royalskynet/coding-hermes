#!/bin/bash
# Backup memory_store.db daily at 03:00, keep 7 days
set -e
DB_PATH="$HOME/.hermes/profiles/mannie/memory_store.db"
BACKUP_DIR="$HOME/.hermes/profiles/mannie/backups"
MAX_BACKUPS=7
TIMESTAMP=$(date +"%Y%m%d")
BACKUP_FILE="$BACKUP_DIR/memory_store_$TIMESTAMP.db"

mkdir -p "$BACKUP_DIR"
cp "$DB_PATH" "$BACKUP_FILE" 2>/dev/null && echo "[$(date)] BACKUP OK: $BACKUP_FILE"

# Cleanup old (>7)
find "$BACKUP_DIR" -name "memory_store_*.db" -mtime +$MAX_BACKUPS -delete 2>/dev/null
