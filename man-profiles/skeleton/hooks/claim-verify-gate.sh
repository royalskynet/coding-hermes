#!/bin/bash
# claim-verify-gate.sh — Mannie pre_verify hook
# Fires when agent edited code and is about to finish. If the final response
# makes a completion claim but shows NO evidence of actually running verify
# commands, block the stop and demand pasted command output.
set -euo pipefail
INPUT=$(cat 2>/dev/null || echo "")
[ -z "$INPUT" ] && exit 0

FINAL=$(printf '%s' "$INPUT" | python3 -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(d.get('final_response','') or d.get('extra',{}).get('final_response',''))
" 2>/dev/null || true)
[ -z "$FINAL" ] && exit 0

# 完成宣稱特徵
CLAIM=$(printf '%s' "$FINAL" | grep -icE '完成|已完成|全通過|通過基準|✅|\bPASS\b|done|搞定' || true)
# 實跑證據特徵：指令提示符 / 明顯輸出區塊
EVID=$(printf '%s' "$FINAL" | grep -cE '^\s*\$ |```|exit=|總數:|→' || true)

if [ "${CLAIM:-0}" -ge 1 ] && [ "${EVID:-0}" -eq 0 ]; then
  MSG='偵測到完成宣稱但無實跑證據。收尾前，對你剛才每個「完成/通過」宣稱，實跑對應的驗證指令，逐條貼「指令原文 + 完整輸出」。無 tool 輸出佐證的宣稱一律改標「未驗證」。不要只覆述計畫。'
  python3 -c "import json,sys; print(json.dumps({'action':'continue','message':sys.argv[1]}))" "$MSG"
fi
exit 0
