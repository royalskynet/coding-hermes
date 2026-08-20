---
name: coding-hermes
description: "Coding/debug 任務自動載入:三層行為紀律、fixindex 查寫、盲試停損、長期任務自給自足。"
version: 1.0.0
author: royalskynet (Ether)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [coding, debugging, fixindex, discipline, stop-loss, kanban, self-sufficient]
    related_skills: [systematic-debugging, hermes-agent, kanban-worker]
---

# coding-hermes — coding agent 三層行為紀律

> 來源: royalskynet/coding-hermes(此 repo)。本 skill 把 coding agent 的「先查舊帳 → 失敗回查 → 記完再停」紀律做成可載入的 Hermes skill。
> 目的:讓 coding/debug 代理在任務中減少反覆與人工介入。

## When to Use

正在處理**開發 / 整合 / 除錯 / 規劃多步 coding 任務**、或連續失敗需要重估時 → 自動載入本 skill。純問答、閒聊、資料查詢不觸發。coding 任務標誌:要求寫/改 code、修 bug、跑測試、整合系統、或任務需要「先查舊帳再動手」。

## 階段 0 — 開工查舊帳(必做)

開發/整合/除錯任務**動手前**必須先查,合併結果,不是查無才查另一個:

1. **修理日誌** — `fixindex find "<錯誤症狀 / 系統名>"`(預設 runbook 在 `~/notes/runbook/fixes`,可用 `FIXINDEX_DIR` 覆蓋;本機亦常見 `~/.claude/runbook/fixes`)。命中 → 直接讀對應 `fixes/NNNN-*.md`。若 `fixindex` 未安裝,改用本機記憶/搜尋工具。
2. **Hermes 歷史** — `session_search`(本地 session DB)搜過去修過的同類問題。
3. **既有範例 / 接口** — 找同類 config/模板當範例,跑 `--help` 或讀文件確認接口,不靠猜測。

違反特徵:一步一發現、靠 error message 往回推用法。

## 三層行為紀律(核心,長期任務每輪都在跑)

| 層 | 觸發 | 動作 |
|---|---|---|
| 1. 啟動前查 fixindex | coding 任務開頭 | `fixindex find "<症狀>"` + `session_search`,合併閱讀 |
| 2. 首輪失敗後查上游 | 同一道指令第一次失敗 | 以「上次失敗症狀」回查 `fixindex find` → 命中注入再重試;還有 `docs/` + GitHub(issues/PR) 查證 |
| 3. 三條路線失敗後記錄並阻斷 | 已試 3 種不同方案仍失敗 | 用 `fixindex fi` 記錄「未修 + 診斷 + 下一步」,停手求助,不盲第 4 條 |

**停損閾值(兩者不同義,勿混用):**
- 未定位根因的**盲試 2 次即停** → 重述假設,列 ≥3 個可證偽替代解釋(protocol / transport / routing / lifecycle / stale-state / 權限 / 依賴),給最小安全 probe。
- 已定位根因、每輪換不同修法的**修正輪上限 3 輪**。

**同一解法連續失敗 2 次**:停止變體嘗試,重述假設與反例,不原樣重送指令。

## 非模型反事實閘門

遇到 bug 別急著怪「模型太弱」或「timeout」。至少列 3 個可證偽替代表述並最小 probe 驗證;未排除 stale plan、state drift 前,不得只靠換模型 / 加 timeout / 重送 prompt / 重啟服務。

## 完工寫入 fixindex(只在修好 defect 時)

修好**一個 defect** 才記錄(階段完成、任務交付、session 收工並不算;phase 工作流最容易把 runbook 寫成日記)。

**判準**:寫不出別人會拿來搜尋的 `Symptom` → 這是進度報告,不是條目。

補記入口(fixindex CLI;勿手動 append `## §N` 或改 frontmatter):
```bash
printf 'SYMPTOM: ...\nROOT: ...\nFIX: ...\nVERIFY: <可重跑指令 + 期望結果>' | fixindex fi
```
insight(已固化的設計決策,非 defect):
```bash
printf 'CONTEXT: ...\nINSIGHT: ...\nIMPLICATION: ...\nREVISIT-WHEN: ...\nQUERIES: q1, q2' | fixindex fi
```
新域:`fixindex new <slug>`;frontmatter 損毀 → `fixindex doctor --fix`。

**禁**:date 進檔名、`## §N (date)` 修正片段(直接改原文)、Verify 用一次性讀數(必須可重跑+期望)、Fix 寫成「Phase 3 修好」、metrics/PID 塞進 `symptoms:`、一條記塞多個 defect、secrets(含截斷 key 前綴)。

**診斷未實作也寫**:標「not fixed」+ 下一步 — 診斷本身是資產。

## 長期任務自給自足(agent / subagent 在跑時)

- 每 iteration 邊界 / 續跑前檢查是否需要寫檢查點,記錄 iteration 數、判決、摘要,避免 timeout 失去進度。
- `kanban` worker:另見 `kanban-worker` skill — Block 理由要具體(如「rate-limit key 選擇」),`kanban_show` 優先於 CLI。
- Plan 定稿回報必帶**計畫檔絕對路徑**。

## 收工自檢清單

- [ ] 開工時有跑 fixindex find / session_search
- [ ] 首輪失敗後有回查舊帳 / docs / GitHub
- [ ] 盲試 ≤2 次,修正輪 ≤3 輪
- [ ] 完工後有 `fixindex fi` 記錄(除非無 defect 可記)
- [ ] 改 schema/config 後已推到 live,驗收條文含指令已實跑貼輸出