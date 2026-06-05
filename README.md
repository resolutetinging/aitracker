# AI Tracker — 規格書 v1.5（2026-06-05）

## 概覽

每日自動抓取 AI/半導體供應鏈新聞，透過 Groq LLM 分析，輸出結構化 JSON，展示於 GitHub Pages。

---

## 架構

```
GitHub Actions (cron-job.org 觸發)
  └─ scripts/update_data.py
       ├─ 抓新聞（RSS + DDG）
       ├─ 呼叫 Groq API → 生成 JSON
       ├─ 品質管線（validate_sources → downgrade_unsourced → downgrade_low_quality → fix_chains）
       ├─ 儲存 data/history.json（保留最近 7 天）
       ├─ send_email()
       └─ push_notion()
```

---

## Groq API 呼叫規格

| 參數 | 值 |
|------|----|
| 主模型 | `llama-3.3-70b-versatile` |
| 備用模型 | `llama-3.1-8b-instant`（413 TPM 超限時切換） |
| `temperature` | `0.3` |
| `max_tokens` | `4000`（低於此值 JSON 中途截斷） |
| 輸出格式 | 純 JSON，`finish_reason=length` 拋 ValueError |

---

## Prompt 規格（make_prompt）

### 分類
- **hw**：半導體/封裝（CoWoS/OSAT/HBM）/晶片製造
- **corp**：CSP（Microsoft/Google/Meta/Amazon）CapEx、AI 投資、財報訊號（不含股價）
- **app**：Agentic AI、Physical AI、VLA、推論部署

### 每條目欄位

| 欄位 | 規格 |
|------|------|
| `title` | 繁體中文標題 |
| `layer` | 子層級 |
| `body` | **EXACTLY 3 句**，每句以**不同主語**開頭，每句陳述新事實，含具體數字/日期/公司名；來自新聞原文，不得捏造 |
| `impact` | **2-3 句**上下游供應鏈影響敘述（不得列舉關鍵字）：點名 foundry/OSAT/memory/CSP/OEM/ODM 角色及影響方向（產能吃緊/ASP走高/訂單轉移） |
| `rating` | `core` / `opp` / `noise` |
| `insight` | 1 句投資者重點（繁體中文） |
| `source_label` | 來源名稱 |
| `source` | 原始 URL（逐字複製，不得捏造） |

### 數量規則
- 每節 2-4 條；無相關新聞 → 僅 1 條 noise
- 有 source URL 才能評為 core/opp，否則強制 noise

---

## 品質管線（每日固定執行）

```
validate_sources()      → HEAD/GET 驗 URL，失效者清空 source
downgrade_unsourced()   → 無 source 的 core/opp → noise
downgrade_low_quality() → body 三道重複偵測 + 無數字 → noise
fix_chains()            → chain 不合格（泛稱/節點<2）→ 重新生成
```

### body 重複偵測三閘

| 閘 | 方法 | 門檻 | 攔截對象 |
|----|------|------|---------|
| 字元集重疊 | `set(s1) ∩ set(s2)` | > 65% | 完全相同內容重排 |
| CJK bigram 重疊 | 2字組交集 | > 45% | 相同片語換句型說 |
| 共用前綴 | 前 5 個中文字 | 2+ 句相同 | 同主語說三次 |
| 無數字 | `re.search(r'\d\|%\|億…')` | — | 空洞/套話 body |

---

## 觸發機制

- **唯一自動觸發**：cron-job.org → `repository_dispatch: daily-trigger`
- GitHub Actions `schedule cron` **已移除**（避免重複觸發）
- 冪等保護：今日已有 ≥2 CORE → 直接沿用，不重新生成（`--force` 強制覆蓋）

---

## Email 推送（send_email）

- 收件人來源：`NOTIFY_EMAIL` Secret（隱藏 dashboard）+ `data/email_config.json`（公開清單）
- `email_config.json` 格式：
  ```json
  {
    "recipients": ["a@example.com"],
    "enabled": true
  }
  ```
- `enabled: false` → **整個 send_email 跳過**（Secret 收件人也不送）
- UI：飛鴿 🕊️ 按鈕 → modal → toggle 開/關 → 儲存至 GitHub

---

## 資料結構（history.json）

```json
[
  {
    "date": "2026-06-05",
    "is_sunday": false,
    "hw": [ { "title":"…","layer":"…","body":"…","impact":"…","rating":"core","insight":"…","source_label":"…","source":"…","chain":[{"label":"TSMC CoWoS 產能↑","type":"up"}] } ],
    "corp": […],
    "app": […],
    "glossary_new": [ { "term":"…","full":"…","def":"…","why":"…","category":"…" } ],
    "weekly_summary": null
  }
]
```

- 保留最近 7 天，按日期降序排列

---

## 前端（ai_tracker_v6.html）

- GitHub Pages 靜態部署，直接讀取 `data/history.json`
- 分頁：今日動態 / 歷史紀錄 / 術語庫 / 封存
- 供應鏈影響分析：`item.impact`（文字敘述）優先；無 impact 則顯示 `item.chain`（視覺節點鏈）
- 飛鴿 modal：推送開關 toggle + 收件人清單，儲存至 `data/email_config.json`

---

## 常見問題排查

| 症狀 | 根因 | 處理 |
|------|------|------|
| `JSONDecodeError: Unterminated string` | max_tokens 不足，JSON 截斷 | 確認 `max_tokens=4000`；若仍截斷提高至 6000 |
| `finish_reason=length` ValueError | 同上 | 同上 |
| chain 全部空陣列 | `fix_chains()` 未被呼叫 | 確認 main flow 有呼叫 |
| impact 只有關鍵字列表 | prompt 不夠明確 | 確認 impact 欄位說明含「2-3 sentences」+ 範例句 |
| body 重複句未被攔截 | 只用字元集重疊，無法攔同主語重複 | 確認三閘都在 `_body_is_low_quality()` |
| 兩封 email | cron/auto run 重疊 | 冪等保護已上，第二次不重新生成 |
| pages fail | notes.json 連續 push race condition | 偶發，不影響功能 |
