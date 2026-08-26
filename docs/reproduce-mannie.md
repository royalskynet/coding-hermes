# 在空白設備上復現 Mannie 架構（Reproduce Mannie）

本 playbook 說明如何在**空白設備**上，以本 repo 的脫敏骨架逐步重建「Mannie」agent 架構。此處只提供 **元件清單、安裝順序、環境變數介面與安全規則**；憑證一律由你本機注入，不寫入任何 repo。

## 1. 元件清單

Mannie 完整架構由 4 個獨立部分組成：

| 元件 | 位置 | 用途 | Clone／取得 |
|---|---|---|---|
| 引擎 repo | `royalskynet/coding-hermes`（本 repo，Hermes agent fork） | Hermes agent 本體；含你需要的 bug 修復（見下方 §0 版本確認） | `git clone git@github.com:royalskynet/coding-hermes.git` |
| skill repo | `royalskynet/coding-hermes-skill` | `coding-hermes` skill（人格／方法論本體） | `git clone git@github.com:royalskynet/coding-hermes-skill.git` |
| fixindex repo | `~/dev/fixindex` | fixindex CLI（私人修復日誌＋檢索） | 單獨 git 專案（不在公開 repo） |
| profile 骨架 | `man-profiles/skeleton/`（本 repo） | SOUL／config／hooks／cron 的脫敏模板 | 已隨本 repo clone 取得 |

> 引擎 repo 的 `upstream` 是 `https://github.com/NousResearch/hermes-agent.git`，本 repo 在 `main` 上額外含 2 個 bug 修復（cron scheduler 靜默 non-ok、gateway SIGTERM ledger 回歸測試）。**版本確認**：clone 後 `git log --oneline -3` 至少要看到 `6ac0253a2e`（cron C2 修復）與 `b7d27fe121`（gateway B1 回歸），否則先 `git pull origin main`。

## 2. 安裝順序

### Step 1 — clone 引擎並設定

```bash
git clone git@github.com:royalskynet/coding-hermes.git ~/.hermes/hermes-agent
cd ~/.hermes/hermes-agent
# 引擎安裝（含 setup-hermes.sh）
./setup-hermes.sh    # 或依引擎 README 的其他安裝方式
git remote add upstream https://github.com/NousResearch/hermes-agent.git
```

### Step 2 — 安裝 coding-hermes skill

```bash
git clone git@github.com:royalskynet/coding-hermes-skill.git
# 把 skill 掛到 Hermes 的 skills 目錄
cp -r coding-hermes-skill/skills/coding-hermes ~/.agents/skills/coding-hermes
# 依 skill repo 的 README 確認掛載路徑（~/.agents/skills/ 或 profile skills_dir）
```

### Step 3 — 安裝 fixindex

```bash
git clone <你的 fixindex repo> ~/dev/fixindex   # 單存在，非公開
# 依 fixindex repo README 安裝與 index
echo 'export FIXINDEX_DIR=${FIXINDEX_DIR:-$HOME/.claude/projects/.../memory/fixes}' >> ~/.zshenv
```

### Step 4 — 建 profile

以骨架為模板建立 profile：

```bash
PROFILE=myagent
mkdir -p ~/.hermes/profiles/$PROFILE/{hooks,cron,cache}
# 複製骨架（骨架內變數以環境變數注入，勿回填本機值）
cp man-profiles/skeleton/SOUL.md           ~/.hermes/profiles/$PROFILE/SOUL.md
cp man-profiles/skeleton/config.yaml       ~/.hermes/profiles/$PROFILE/config.yaml
cp man-profiles/skeleton/hooks/*           ~/.hermes/profiles/$PROFILE/hooks/
cp man-profiles/skeleton/cron/*.sh         ~/.hermes/profiles/$PROFILE/cron/
# profile 層.env：放入憑證
touch ~/.hermes/profiles/$PROFILE/.env   # 見 §4 安全注意
```

### Step 5 — 裝 hooks 與 cron

- hooks 由 config.yaml 的 `hooks:` 區參照，路徑需對應 `$PROFILE_DIR/hooks/*.sh`。
- cron：把 `cron/jobs.json.example` 改成真實 `jobs.json`，填入 schedule 與 prompt；啟動 cron 用 `hermes -p $PROFILE cron` 系列指令。

## 3. 環境變數清單（不寫值）

| 變數 | 用途 | 注入位置 |
|---|---|---|
| `OPENROUTER_API_KEY` | 主模型 chain 第一段 | `config.yaml` model / fallback / auxiliary |
| `AGNES_API_KEY` | fallback provider（agnes） | `config.yaml` fallback / custom_providers |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub MCP server | `config.yaml` mcp_servers.github |
| `PROFILE_DIR` | profile 根目錄絕對路徑 | hooks（fixindex-recall.sh、memory-write-guard.sh 預設） |
| `FIXINDEX_DIR` | fixindex fixes 資料庫目錄 | fixindex-recall.sh |
| `FIXINDEX_REPO` | fixindex repo 根目錄 | fixindex-recall.sh |
| `OMNIRUTE_HOME` | omniroute-free-tools 根目錄 | cron/harness-poll.sh |
| `MEMORY_DIR` | memory 庫（H4 guard 監控路徑） | memory-write-guard.sh |

> 部署時以環境變數注入實際值；`omniroute-local` 為本機 local 偽值（連本機 omniroute 代理），可保留。

## 4. 安全注意

- **憑證只放 profile `.env`**，repo `.gitignore` 已排除 `.env`／`cli-config.yaml`／`.op.env` 等；**永不寫入 repo／log／commit**。
- config.yaml 的 secret 全部走 `${VAR}` 環境參照，不寫真值。
- 本骨架 `man-profiles/skeleton/` 不得出現真 `/Users/...`、`sk-...`、`AKIA...` 等洩漏樣式。
- 若你是 public repo：push 前跑 `gitleaks detect --source . --redact` 並 `grep -rInE 'sk-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20}'` 且手動確認不含本機使用者家目錄樣式。
- 對外發布屬外部動作，先確認 repo 清淨再 push。

## 5. 驗收建議（空白重建後）

- `hermes -p $PROFILE -z "ping"` → 回 `pong`／OK（healthcheck.sh 同）。
- `docs/reproduce-mannie.md`（本檔）每步有具體指令即可追溯。
- config.yaml 能被 Hermes 正常 parse（`hermes -p $PROFILE doctor` 或直接啟動）。