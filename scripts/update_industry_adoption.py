#!/usr/bin/env python3
"""
產業導入分頁月查證腳本
獨立於NVIDIA週查證（update_nvidia.py）跟每日新聞pipeline（update_data.py）之外——
這裡驗證的是「各產業AI導入現況參考資料（已規模化應用/試點應用/各國導入占比/
長期路線圖）是否過時」，查證頻率是月（不是NVIDIA的週），資料結構為多產業預留
擴充空間（data/industry_adoption.json 的 industries 是陣列，目前只有醫療產業一筆）。

安全設計（鐵律，比照update_nvidia.py同等級）：本腳本絕不直接改寫
data/industry_adoption.json（那是實際顯示在頁面上、看起來權威的參考資料，LLM若
把幻覺內容寫進去會誤導使用者）。所有候選異動一律寫進
data/industry_adoption_pending_review.json，前端只顯示「本月查證偵測到 N 項候選
異動」的提示，實際套用需要使用者告知Claude人工複核後手動更新
industry_adoption.json。

跟NVIDIA模式的關鍵差異：
1. 查證頻率為月，不是週（見 .github/workflows/monthly-industry-adoption-update.yml）
2. 資料來源限定官方機構——搜尋query加官方機構關鍵字，且Groq system prompt明確
   規定候選異動的source欄位網域必須落在ALLOWED_SOURCE_DOMAINS清單，否則即使內容
   相關也不可以拿來當候選異動依據，寧可判定無異動
3. 分類schema不同：cases_live/cases_poc/adoption_by_region/roadmap
   （沒有pyramid/alliances/governance，多一個adoption_by_region）
4. 多產業預留擴充空間：SEARCH_QUERIES_BY_INDUSTRY用industry key查表，未來新增
   產業只要在這裡加一組query、在industry_adoption.json的industries陣列加一筆，
   不需要改動這支腳本的邏輯本身
5. 09-04：不寄email通知（使用者比照Renewable Tracker的定位，這類參考資料月更新
   不需要主動推送，僅安靜寫檔即可）。查證結果照樣只看
   data/industry_adoption_pending_review.json 跟 last_checked 時間戳，
   不需要NOTIFY_EMAIL/GMAIL_USER/GMAIL_APP_PASSWORD這三個secrets，
   只有GROQ_API_KEY是必要的（驅動查證本身）。
"""
import json, os, re, time
from datetime import datetime, timezone, timedelta
from groq import Groq
from groq import APIStatusError as GroqAPIStatusError

TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')

REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
IA_STATUS_PATH = os.path.join(REPO_DIR, 'data', 'industry_adoption.json')
IA_PENDING_PATH = os.path.join(REPO_DIR, 'data', 'industry_adoption_pending_review.json')

# 只用官方機構報告（WHO/OECD/各國政府機構/學術期刊），不用一般新聞媒體。
# 之後新增產業時，在這裡加一組 "industry_key": [(標籤, DDG查詢字串), ...] 即可，
# 不需要改動下面的fetch/處理邏輯。
SEARCH_QUERIES_BY_INDUSTRY = {
    'medical': [
        ("FDA/官方核准", "FDA approval AI medical device clearance site:fda.gov"),
        ("WHO衛生AI報告", "WHO health AI report adoption site:who.int OR site:iris.who.int"),
        ("OECD衛生AI政策", "OECD artificial intelligence health policy report site:oecd.org"),
        ("學術期刊臨床試驗", "AI clinical trial results health outcomes site:nature.com OR site:nejm.org OR site:thelancet.com OR site:jamanetwork.com"),
    ],
}

# Groq候選異動的source欄位網域必須落在這個清單（含子網域），否則即使內容相關
# 也不可以當作候選異動依據——system prompt會把這份清單原樣寫進規則裡
ALLOWED_SOURCE_DOMAINS = [
    'who.int', 'iris.who.int', 'oecd.org', 'itu.int', 'wipo.int',
    'fda.gov', 'nih.gov', 'cdc.gov', 'ncbi.nlm.nih.gov', 'nhs.uk',
    'meti.go.jp', 'mhlw.go.jp',
    'nature.com', 'nejm.org', 'ai.nejm.org', 'thelancet.com',
    'jamanetwork.com', 'jmir.org',
    # .gov / .go.jp 等政府網域一般規則（system prompt文字裡另外強調「任何國家政府
    # 機構官網，網域結尾含.gov或.go.xx」也算符合，不是只有清單裡列出的才算）
]


def fetch_industry_news():
    """用DDG查最近一個月各產業AI導入相關新聞，query強制加官方機構關鍵字／站內限定，
    盡量在蒐集端就過濾掉一般新聞媒體，Groq prompt再做第二層網域白名單把關。"""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("  ⚠ 找不到 ddgs/duckduckgo_search 套件，本次跳過新聞蒐集")
            return {}
    ddgs = DDGS()
    news_by_industry = {}
    for industry_key, queries in SEARCH_QUERIES_BY_INDUSTRY.items():
        snippets = []
        for label, q in queries:
            for attempt in range(3):
                try:
                    results = list(ddgs.news(q, max_results=6, timelimit="m"))
                    for r in results:
                        link = r.get('url', '')
                        url_part = f" | SOURCE_URL:{link}" if link else ""
                        snippets.append(f"[{industry_key}/{label}] {r.get('title','')} — {r.get('body','')[:200]}{url_part}")
                    print(f"  DDG '{industry_key}/{label}': {len(results)} results")
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(3)
                    else:
                        print(f"  DDG '{industry_key}/{label}' failed after 3 attempts: {e}")
        news_by_industry[industry_key] = snippets
    return news_by_industry


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def call_groq_diff(current_status, news_snippets):
    """核對現有各產業導入結構化資料是否過時，只回報有明確新聞佐證且來源網域落在
    ALLOWED_SOURCE_DOMAINS白名單的候選異動，system prompt刻意極度保守，比照
    update_nvidia.py同等級：沒有證據支持的欄位一律維持原樣，空items是正常結果。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    domains_text = '、'.join(ALLOWED_SOURCE_DOMAINS)
    sys_msg = (
        "你是全球醫療與各產業AI導入分析師，任務是核對一份既有的「產業導入AI現況」結構化"
        "參考資料是否過時。只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "極度保守：沒有明確新聞佐證的欄位一律維持原樣、不提出更新建議；"
        "禁止臆測、禁止捏造來源URL、禁止把不確定的傳聞當成確定事實。"
        "多數月查證後的正確答案就是「沒有任何異動」，回傳空items陣列是完全正常且被期待的結果，"
        "不需要為了顯得有查證成果而硬湊出候選異動。"
        "資料來源限定規則（最重要，違反這條的候選異動一律不可提出）："
        f"候選異動的source欄位網域只能是官方機構或學術期刊，例如：{domains_text}，"
        "或任何國家政府機構官網（網域結尾為.gov、.go.jp等政府網域格式）。"
        "如果新聞片段的來源不是這類官方/學術/政府機構網站，即使內容看起來相關，"
        "也絕對不可以拿來當作候選異動的依據，寧可判定該部分無異動、不提出。"
        "一般新聞媒體（科技媒體、財經媒體、部落格等）一律不得作為source。"
    )
    def build_prompt(news_by_industry):
        status_json = json.dumps(current_status, ensure_ascii=False, separators=(',', ':'))
        news_lines = []
        for key, items in news_by_industry.items():
            news_lines.extend(items)
        news_text = chr(10).join(news_lines) if news_lines else '（本月未蒐集到相關新聞片段）'
        return f"""以下是各產業AI導入結構化參考資料的現況（JSON，頂層industries陣列每筆代表一個產業，
用key欄位識別）：

{status_json}

以下是過去一個月蒐集到的相關新聞片段，每則片段開頭[產業key/查詢標籤]標示屬於哪個產業，
結尾若有「| SOURCE_URL:網址」就是該則新聞的原始來源網址；source欄位只能填這裡實際出現過的
SOURCE_URL，禁止自己編造或憑記憶生成網址，且該網址網域必須符合上述資料來源限定規則：

{news_text}

請核對上述新聞是否讓現有資料的任何欄位過時或不準確，只針對「有明確新聞佐證」且「來源網域
符合官方機構/學術期刊/政府網站白名單」的部分提出候選異動。不要因為沒有符合規則的新聞佐證
就自己推測任何欄位「應該」要改；找不到能對應到具體合格新聞的變化就不要提出，空陣列是完全
正常的結果。

輸出格式（純JSON）：
{{
  "items": [
    {{
      "industry_key": "候選異動屬於哪個產業，填現有industries陣列裡對應的key（例如medical）",
      "category": "cases_live|cases_poc|adoption_by_region|roadmap",
      "action": "update|add",
      "target_name": "若action=update，填現有資料裡對應項目的name或region文字；若action=add則留空",
      "name": "項目/案例名稱（cases_live/cases_poc/roadmap類別適用）",
      "region": "地區/國家名稱（僅adoption_by_region類別適用，其他類別留空）",
      "pct_or_status": "量化占比或質化狀態描述（僅adoption_by_region類別適用，其他類別留空）",
      "desc": "更新後或新增的描述文字",
      "status": "狀態文字（roadmap類別適用，其他類別可留空）",
      "scope": "規模/範圍描述（cases_live/cases_poc類別適用，可留空）",
      "timeline": "時間點（cases_live/cases_poc類別適用，可留空）",
      "reason": "為何提出這個異動，具體說明新聞依據",
      "source": "新聞來源URL（網域須符合資料來源限定規則）"
    }}
  ],
  "no_change_summary": "若items為空陣列，一句話說明本月查證後判斷現有資料仍準確；若items非空則留空字串"
}}"""
    # 沿用update_nvidia.py同款413防護：current_status用compact JSON省字元，
    # 413時砍news片段對半重試，最多5輪，5輪都失敗才拋例外讓main()寄失敗通知信
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    news_by_industry = {k: list(v) for k, v in news_snippets.items()}
    response = None
    shrink_round = 0
    for shrink_round in range(5):
        for model in models:
            try:
                response = client.chat.completions.create(
                    model=model,
                    reasoning_effort="low",
                    messages=[{"role": "system", "content": sys_msg},
                              {"role": "user", "content": build_prompt(news_by_industry)}],
                    temperature=0.2,
                    max_tokens=3000,
                )
                break
            except GroqAPIStatusError as e:
                if e.status_code == 413:
                    total = sum(len(v) for v in news_by_industry.values())
                    print(f"  → {model} 超出TPM（目前新聞{total}則）...")
                    continue
                raise
        if response is not None:
            break
        total = sum(len(v) for v in news_by_industry.values())
        if total == 0:
            break
        for k in news_by_industry:
            news_by_industry[k] = news_by_industry[k][:len(news_by_industry[k]) // 2]
        total = sum(len(v) for v in news_by_industry.values())
        print(f"  → 縮減新聞片段至共{total}則重試...")
    if response is None:
        total = sum(len(v) for v in news_by_industry.values())
        raise ValueError(f"連續{shrink_round+1}輪（含縮減新聞片段至共{total}則）仍超出Groq TPM限制，current_status本身可能已過大")
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    finish_reason = response.choices[0].finish_reason
    if finish_reason == 'length':
        raise ValueError(f"Groq回應被截斷（finish_reason=length，{len(raw)}字元）")
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


def main():
    print(f"\n{'='*50}")
    print(f"產業導入專區月查證 — {NOW.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    status = load_json(IA_STATUS_PATH, None)
    if status is None or 'industries' not in status:
        print("  ⚠ 找不到 data/industry_adoption.json 或格式不符，中止")
        return

    print("📰 蒐集各產業AI導入相關新聞（過去一個月，限官方機構來源）...")
    news_by_industry = fetch_industry_news()
    total = sum(len(v) for v in news_by_industry.values())
    print(f"  → 共 {total} 則片段")

    # 跟update_nvidia.py同樣的防護：DDG常被機房IP限流，若整個月完全沒抓到任何
    # 新聞片段，直接跳過Groq呼叫，避免沒有佐證卻硬要模型「核對是否過時」
    if total == 0:
        print("  → 本月未蒐集到任何新聞片段，跳過Groq呼叫（避免無佐證卻要求提出異動）")
        for industry in status.get('industries', []):
            industry['last_checked'] = DATE_STR
        save_json(IA_STATUS_PATH, status)
        print("✅ 完成（本月無新聞片段，僅更新查證時間戳）\n")
        return

    print("🤖 Groq 核對現有資料是否過時...")
    try:
        diff = call_groq_diff(status, news_by_industry)
        if not isinstance(diff, dict):
            raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
    except Exception as e:
        # 比照update_nvidia.py：Groq失敗時last_checked刻意不更新，讓pending_review/
        # industry_adoption.json誠實反映「這月其實沒查證成功」（不寄信通知，靠
        # last_checked時間戳過舊來察覺，比照使用者要求跟Renewable Tracker一樣不推送mail）
        print(f"  ⚠ Groq 呼叫失敗，本次不更新任何內容：{e}")
        return

    items = diff.get('items') or []
    pending = {
        'checked_at': DATE_STR,
        'items': items,
        'no_change_summary': diff.get('no_change_summary', ''),
    }
    save_json(IA_PENDING_PATH, pending)

    # last_checked 時間戳寫回 industry_adoption.json 本身（每個產業各自的欄位，
    # 這是唯一允許自動改寫的部分，純粹是「上次查證時間」的紀錄用途）
    for industry in status.get('industries', []):
        industry['last_checked'] = DATE_STR
    save_json(IA_STATUS_PATH, status)

    if items:
        print(f"  → 偵測到 {len(items)} 項候選異動，已寫入 data/industry_adoption_pending_review.json（未套用，待人工複核）")
    else:
        print(f"  → 本月查證後無需更新：{pending['no_change_summary']}")
    # git commit/push 交給 GitHub Actions 的 git-auto-commit-action 處理，
    # 本腳本只負責寫檔案，不自己動 git
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
