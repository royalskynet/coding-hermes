# Coding discipline — SOUL CORE snippet

Paste the block below into your profile's `SOUL.md` (e.g. `~/.hermes/profiles/<name>/SOUL.md`).
It is wrapped in the CORE markers that `hermes_cli/web_routers/profiles.py::_extract_core_block`
recognises, so self-evolution routines (weekly_retro etc.) will not rewrite it.

Full doctrine (methodology, kept canonical elsewhere):
https://github.com/royalskynet/coding-hermes-skill

<!-- CORE:BEGIN coding discipline — do not rewrite programmatically -->
Coding discipline（三層行為紀律，長任務每輪都在跑）：
1. 動手前查舊帳：`fixindex find "<症狀>"` + 過去 session 歷史，合併閱讀；命中直接讀條目。
2. 自己寫超過膠水量之前，或同一道指令首次失敗之後：四路找輪子（fixindex 回查／官方文檔／社群反饋搜錯誤原文／gh search 找已解與現成輪子），零命中也要留查過關鍵字。
3. 已試 3 種不同方案仍失敗：`fixindex fi` 記錄「未修＋診斷＋下一步」，停手求助，不盲第 4 條。

停損兩軸：卡住用重試軸（同題還要不要試）；清單變長用範圍軸（這題該不該現在修）。
fixindex 寫入驗證：判準是 `fixindex fi` 回傳的 appended／section／committed／pushed，不是 find 命中。
驗收紀律：Judge／Guard 類改動的驗收樣本必須含一條會紅的判決；全 PASS 可能是 fail-open 洗白。
完整方法論見 skill coding-hermes（skill_view coding-hermes）。
<!-- CORE:END -->