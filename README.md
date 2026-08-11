# coding-hermes

> Hermes agent — fork with kanban iteration checkpointing for coding agent self-sufficiency

[![GitHub](https://img.shields.io/badge/GitHub-coding--hermes-blue?logo=github)](https://github.com/royalskynet/coding-hermes)

An opinionated fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) with local modifications focused on making kanban‑driven coding agents more self‑sufficient:

- **Iteration Checkpoint** — JSON checkpoint at every iteration boundary, so turn‑budget exhaustion does not lose progress
- **SIGTERM Resilience** — loop‑entry checkpoint dumps before each continuation turn survive runtime‑limit kills
- **Resume Context** — `build_worker_context()` injects a human‑readable resume block into the task context
- **Behavior Guards** — task‑level discipline rules (pre‑task fixindex lookup, doc research after first failure, three‑strike block)

Built on upstream `NousResearch/hermes-agent` with 4 local commits (~100 lines of code changed).

---

## 繁體中文

### 這是什麼

`coding-hermes` 是 Hermes agent 的個人分支，專注於讓 kanban 驅動的 coding agent 在長期任務中更自給自足，減少人工介入。

### 主要改動

| 改動 | 說明 |
|------|------|
| **Iteration Checkpoint** | 每次 iteration 邊界寫入 JSON checkpoint（`~/.hermes/kanban-checkpoints/`），記錄 iteration 數、judge verdict、response snippet |
| **Loop-Entry Checkpoint** | 在每次 continuation turn 前也寫入 checkpoint，避免 runtime timeout 導致進度遺失 |
| **Resume Context Injection** | `build_worker_context()` 自動偵測 checkpoint 存在，在 task context 中注入 resume block |
| **Task-Level Behavior Rules** | 透過 SOUL.md 設定三層行為紀律：啟動前查 fixindex、首輪失敗後查 docs/GitHub、三路線失敗後記錄並阻斷 |

### Dev Log

**2026-08-10** — Initial implementation: `_checkpoint_dump()` / `_checkpoint_load()` / `checkpoint_readable()` in `goals.py`, two injection points in `run_kanban_goal_loop()`, and `build_worker_context()` modification in `kanban_db.py` for resume context injection.

**2026-08-11** — Loop-entry checkpoint added after testing revealed that SIGTERM from max-runtime limits killed the process before the post-turn checkpoint could be written. Now checkpoint is written at loop entry (before `run_turn()`) with `task_status=looping`, so even if the current turn is killed, the previous iteration's progress survives.

---

## English

### What is this

`coding-hermes` is a personal fork of the Hermes agent focused on making kanban‑driven coding agents more self‑sufficient during long‑running tasks.

### Key Changes

| Change | Description |
|--------|-------------|
| **Iteration Checkpoint** | JSON checkpoint written at every iteration boundary (`~/.hermes/kanban-checkpoints/`), recording iteration count, judge verdict, and response snippet |
| **Loop-Entry Checkpoint** | Checkpoint also written before each continuation turn, so SIGTERM from runtime limits does not lose progress |
| **Resume Context Injection** | `build_worker_context()` automatically detects existing checkpoints and injects a resume block into the task context |
| **Task-Level Behavior Rules** | Three‑layer discipline baked into the agent's SOUL.md: pre‑task fixindex lookup, doc/GitHub research after first failure, and fixindex recording + block after three distinct approaches fail |

### Dev Log

**2026-08-10** — Initial implementation: `_checkpoint_dump()` / `_checkpoint_load()` / `checkpoint_readable()` in `goals.py`, two injection points in `run_kanban_goal_loop()`, and `build_worker_context()` modification in `kanban_db.py` for resume context injection.

**2026-08-11** — Loop-entry checkpoint added after testing revealed that SIGTERM from max-runtime limits killed the process before the post-turn checkpoint could be written. Now checkpoint is written at loop entry (before `run_turn()`) with `task_status=looping`, so even if the current turn is killed, the previous iteration's progress survives.

---

## Patches

Based on upstream `NousResearch/hermes-agent` commit `d1afa16053`, with patches:

- `hermes_cli/goals.py`: `_checkpoint_dump()`, `_checkpoint_load()`, `checkpoint_readable()` — checkpoint I/O helpers and three injection points in `run_kanban_goal_loop()`
- `hermes_cli/kanban_db.py`: `build_worker_context()` — lazy import and inject resume checkpoint block

```
e2af7d96  fix: add loop-entry checkpoint dump to survive SIGTERM during run_turn
b813924d  fix: rename _checkpoint_readable → checkpoint_readable for public export
b7e3c8bf  kanban: inject resume checkpoint context in build_worker_context
76f65ce1  kanban: add iteration checkpoint dump at every goal-loop boundary
```

---

## Dependencies

- [fixindex](https://github.com/royalskynet/fixindex) — bug runbook CLI for symptom→fix lookup; used in pre-task context lookup
- [mdispatch](https://github.com/royalskynet/mdispatch) — plan.md dispatch tool for kanban task creation with falsifiability lint
- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — upstream source

### Quick install

```bash
# fixindex
curl -sSL https://raw.githubusercontent.com/royalskynet/fixindex/main/fixindex \
  | sudo tee /usr/local/bin/fixindex > /dev/null && sudo chmod +x /usr/local/bin/fixindex

# mdispatch (separate repo)
curl -sSL https://raw.githubusercontent.com/royalskynet/mdispatch/main/mdispatch \
  | sudo tee /usr/local/bin/mdispatch > /dev/null && sudo chmod +x /usr/local/bin/mdispatch

# Verify
fixindex help
mdispatch --help
```