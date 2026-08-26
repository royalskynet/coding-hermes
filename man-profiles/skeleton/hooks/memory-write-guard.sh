#!/bin/bash
# memory-write-guard.sh — H4 第四層防線
# pre_tool_call hook: blocks writes to the memory repository directory.
# Read operations on that path are allowed.
# Fail-closed: if JSON parse fails but raw stdin contains the path, block anyway.
# 脫敏：MEMORY_DIR 改由環境變數注入，預設為 ${HOME}/.claude/projects/<REDACTED>/memory。
set -euo pipefail

MEMORY_DIR="${MEMORY_DIR:-${HOME}/.claude/projects/<REDACTED>/memory}"

python3 -u -c "
import sys, json, re, os

mem = sys.argv[1]
raw = sys.stdin.read()

if not raw.strip():
    sys.exit(0)

# --- JSON parse with fail-closed fallback ---
try:
    d = json.loads(raw)
except json.JSONDecodeError:
    # Fail-closed: raw input contains memory dir path -> block anyway
    if mem in raw:
        print(json.dumps({
            'action': 'block',
            'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
        }))
        sys.exit(1)
    sys.exit(0)

tool = d.get('tool_name', '')
# tool_input = real Hermes protocol; arguments = test-compat / alternative shape
args = d.get('tool_input') or d.get('arguments') or {}

# --- Recursive path check: does any string arg contain the memory dir? ---
def has_mem_path(v):
    if isinstance(v, str) and mem in v:
        return True
    if isinstance(v, dict):
        return any(has_mem_path(x) for x in v.values())
    if isinstance(v, list):
        return any(has_mem_path(x) for x in v)
    return False

if not has_mem_path(args):
    sys.exit(0)

# --- Memory dir is referenced — determine if write or read ---

# File-level write tools
WRITE_TOOLS = {
    'write_file', 'patch', 'edit', 'append', 'overwrite',
    'create_or_update_file', 'delete_file', 'remove_file',
    'upload',
}

# 目標欄位名稱（依工具而異）
def _target_path(args, tool):
    for k in ('path', 'file_path', 'filePath', 'remote_path'):
        v = args.get(k)
        if v:
            return os.path.expanduser(str(v))
    return None

# 只有「寫入/刪除的目標」落在記憶庫才算寫入記憶庫。
# 內容（content）提到記憶庫但目標在外（如寫到 plans/、/tmp）→ 純參考，放行。
# B2: 目標欄位缺失或相對路徑 → fail-closed 擋（介面已含 memory 路徑，目標落點不明應保守拒絕）。
if tool in WRITE_TOOLS:
    tgt = _target_path(args, tool)
    if tgt and os.path.isabs(tgt) and mem in tgt:
        print(json.dumps({
            'action': 'block',
            'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
        }))
        sys.exit(1)
    if tgt and not os.path.isabs(tgt):
        print(json.dumps({
            'action': 'block',
            'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
        }))
        sys.exit(1)
    if not tgt:
        # 無目標欄位：write/delete 無明確落點，無法保證不觸及 memory → fail-closed 擋
        print(json.dumps({
            'action': 'block',
            'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
        }))
        sys.exit(1)
    # 目標絕對路徑且在 memory 外：放行
    # （write tool 目標在外 → 視為讀/外部，不誤擋）

# Terminal — check command string for write-inducing patterns (memory-dir-aware)
if tool == 'terminal':
    cmd = args.get('command', '')

    # 真正會改檔案的指令（無歧義，維持攔截）
    WRITE_PATTERNS = [
        r'\btee\b',                       # tee to file
        r'\brm\b',                        # remove
        r'\btouch\b',                     # touch (creates / updates)
        r'sed\s+-i',                      # sed in-place
        r'\binstall\b',                   # install to
        r'\bdd\s+of=',                    # dd write target
        # 破壞性 git（B3）
        r'git\s+reset\s+--hard',
        r'git\s+clean\b',
        r'git\s+checkout\s+--',
        r'git\s+restore\b',
        r'git\s+push\s+.*(-f\b|--force)',
        # truncate 等（B4）
        r'\btruncate\b',
        r'\bshred\b',
        r'perl\s+-i',
        r'>\|',                           # clobber redirect (noclobber bypass)
    ]
    for p in WRITE_PATTERNS:
        if re.search(p, cmd):
            print(json.dumps({
                'action': 'block',
                'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
            }))
            sys.exit(1)

    # redirect (> / >> / 2> / &>)：只有「目標路徑展開後落在 memory dir」才算寫入 memory。
    # 2>&1、> /dev/null、> /tmp/... 等 fd 或 memory 外目標 → 純 stdout 處理，放行（避免誤擋 git push 常見的 2>&1）。
    # B1: 相對路徑 redirect 目標 → fail-closed 擋（介面已含 memory 路徑，相對落點可能是 memory 內）。
    for m in re.finditer(r'(?:>>|>|2>|&>)\s*(\S+)', cmd):
        tgt = m.group(1).strip()
        if not tgt:
            continue
        if re.fullmatch(r'&\d+', tgt):          # >&2 / >&1 fd redirect
            continue
        if tgt in ('/dev/null', '/dev/stdout', '/dev/stderr', '/dev/fd/1', '/dev/fd/2'):
            continue
        tgt_exp = os.path.expanduser(tgt)
        if not os.path.isabs(tgt_exp):
            # 相對目標 + 已確認命令提及 memory → fail-closed 擋
            print(json.dumps({
                'action': 'block',
                'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
            }))
            sys.exit(1)
        if mem in tgt_exp:
            print(json.dumps({
                'action': 'block',
                'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
            }))
            sys.exit(1)

    # cp/mv/rsync: 只有目的地（該段最後一個非旗標參數）落在 memory 才算寫；
    # 來源在 memory、目的地在外 = 純讀，放行
    # B5: 目的地為相對路徑 → fail-closed 擋（介面已含 memory 路徑，相對 dest 落點不明）。
    for seg in re.split(r'[|;&]+', cmd):
        toks = seg.strip().split()
        if not toks:
            continue
        if toks[0] in ('cp', 'mv', 'rsync'):
            pos = [t for t in toks[1:] if not t.startswith('-')]
            if not pos:
                continue
            dest = pos[-1]
            dest_exp = os.path.expanduser(dest)
            if not os.path.isabs(dest_exp):
                print(json.dumps({
                    'action': 'block',
                    'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
                }))
                sys.exit(1)
            if mem in dest_exp:
                print(json.dumps({
                    'action': 'block',
                    'message': 'memory 庫寫入被 H4 hook 攔截，需主 session 授權 via kanban_comment'
                }))
                sys.exit(1)

# Operation is read-only or involves memory dir only as source → allow
sys.exit(0)
" "$MEMORY_DIR"
