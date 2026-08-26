# Mannie Profile Skeleton（脫敏）

這是 Mannie profile 的**脫敏骨架**，用於在空白設備上重建 Mannie 架構時當作可追溯、可操作的模板。**本目錄不含任何真憑證值或本機絕對路徑**，全部以環境變數／`<redacted>` 取代。

## 用途

- 要看「Mannie 的 profile 長什麼樣」→ 讀此目錄。
- 要在新機重建 Mannie profile → 照 `docs/reproduce-mannie.md`（repo 根）的 playbook，把本目錄內容複製到 `~/.hermes/profiles/<name>/`，並以環境變數注入憑證。

## 參數化規範

| 佔位符 | 意義 | 用法 |
|---|---|---|
| `${PROFILE_DIR}` | profile 根目錄絕對路徑（如 `~/.hermes/profiles/mannie`） | 所有「profile 內部檔案」的絕對路徑引用 |
| `${HOME}` | 使用者家目錄 | SOUL／部分 script 的家目錄參照 |
| `${FIXINDEX_DIR}` | fixindex fixes 資料庫目錄 | fixindex-recall.sh |
| `${OMNIRUTE_HOME}` | omniroute-free-tools 工具根目錄 | harness-poll.sh（cron） |
| `$OPENROUTER_API_KEY` | OpenRouter 憑證（env 注入） | config.yaml model 區 |
| `$AGNES_API_KEY` | Agnes 憑證（env 注入） | config.yaml fallback／custom_providers |
| `$GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub MCP token（env 注入） | config.yaml mcp_servers |

## 目錄結構

```
man-profiles/skeleton/
  README.md       # 本檔
  SOUL.md         # profile 人格與工作紀律（脫敏）
  config.yaml     # profile 設定（憑證全走 env 參照）
  hooks/          # Hermes hooks（pre_llm_call / pre_verify / pre_tool_call）
    claim-verify-gate.sh     # 原樣（無絕對路徑）
    fixindex-recall.sh       # 路徑參數化
    memory-write-guard.sh    # 路徑參數化
  cron/           # cron job scripts + jobs.json 結構範例
    add_allowlist.sh         # 原樣
    healthcheck.sh           # 原樣
    memory-backup.sh         # 原樣
    model-check.sh           # 原樣
    harness-poll.sh          # 本機絕對路徑帶出樣式 → $OMNIRUTE_HOME 參數化
    jobs.json.example        # cron job 結構範例（值脫敏）
```

## 安全紅線

- 部署時把真憑證放 profile `.env`（已被 repo `.gitignore` 排除），**永不寫進 repo／log／commit**。
- 本骨架任何檔案出現真實本機絕對路徑（含使用者家目錄展開值）、`sk-...`、`AKIA...` 即屬洩漏，需重做。
