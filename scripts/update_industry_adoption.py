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
import json, os, re, sys, time
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
# 只用官方機構報告（WHO/OECD/各國政府機構/學術期刊），不用一般新聞媒體。
# 2026-10-29：原本只有medical有query，其餘6個產業完全沒新聞可查，補齊各產業query。
SEARCH_QUERIES_BY_INDUSTRY = {
    'medical': [
        ("FDA/官方核准", "FDA approval AI medical device clearance site:fda.gov"),
        ("WHO衛生AI報告", "WHO health AI report adoption site:who.int OR site:iris.who.int"),
        ("OECD衛生AI政策", "OECD artificial intelligence health policy report site:oecd.org"),
        ("學術期刊臨床試驗", "AI clinical trial results health outcomes site:nature.com OR site:nejm.org OR site:thelancet.com OR site:jamanetwork.com"),
    ],
    'supply_chain': [
        ("OECD供應鏈AI", "OECD artificial intelligence supply chain logistics report site:oecd.org"),
        ("WTO/世銀物流AI", "AI logistics trade supply chain report site:wto.org OR site:worldbank.org"),
    ],
    'finance': [
        ("BIS金融AI", "artificial intelligence finance banking report site:bis.org OR site:fsb.org"),
        ("IMF/OECD金融AI", "AI financial sector adoption report site:imf.org OR site:oecd.org"),
    ],
    'government': [
        ("OECD政府AI", "OECD artificial intelligence public sector government report site:oecd.org"),
        ("聯合國/ITU政府AI", "AI government public services report site:un.org OR site:itu.int"),
    ],
    'energy': [
        ("IEA能源AI", "artificial intelligence energy grid report site:iea.org"),
        ("美國能源部AI", "AI power grid utilities site:energy.gov OR site:nrel.gov"),
    ],
    'agriculture': [
        ("FAO農業AI", "AI agriculture adoption report site:fao.org"),
        ("USDA農業AI", "artificial intelligence precision agriculture site:usda.gov OR site:oecd.org"),
    ],
    'transportation': [
        ("NHTSA/DOT自駕", "autonomous vehicle AI transportation site:nhtsa.gov OR site:transportation.gov"),
        ("ITF/OECD交通AI", "AI transport mobility report site:itf-oecd.org OR site:oecd.org OR site:itu.int"),
    ],
}

# 2026-10-29：各產業額外白名單網域（補上其他6個產業會用到的官方機構）
ALLOWED_SOURCE_DOMAINS = [
    'who.int', 'iris.who.int', 'oecd.org', 'itu.int', 'wipo.int',
    'fda.gov', 'nih.gov', 'cdc.gov', 'ncbi.nlm.nih.gov', 'nhs.uk',
    'meti.go.jp', 'mhlw.go.jp',
    'nature.com', 'nejm.org', 'ai.nejm.org', 'thelancet.com',
    'jamanetwork.com', 'jmir.org',
    'wto.org', 'worldbank.org', 'bis.org', 'fsb.org', 'imf.org', 'un.org',
    'iea.org', 'energy.gov', 'nrel.gov', 'fao.org', 'usda.gov',
    'nhtsa.gov', 'transportation.gov', 'itf-oecd.org', 'europa.eu',
    # .gov / .go.jp 等政府網域一般規則（system prompt文字裡另外強調「任何國家政府
    # 機構官網，網域結尾含.gov或.go.xx」也算符合，不是只有清單裡列出的才算）
]

# 分批設定：每次送Groq的產業資料（不含新聞）字元上限；新聞每批最多幾則
MAX_DATA_CHARS = 4500
MAX_NEWS_PER_CALL = 6
CALL_INTERVAL_SEC = 65   # Groq免費額度6000 TPM，呼叫間隔等滾動視窗重置
TEXT_FIELD_CAP = 120     # effect/events/scope 等長文字截斷長度


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


def _cap(s, n=TEXT_FIELD_CAP):
    s = s if isinstance(s, str) else ''
    return s if len(s) <= n else s[:n] + '…'


def build_industry_units(industry):
    """把單一產業拆成精簡單元（不送flow/risk/future/goal/src這類長文字）。
    每個單元是(category, dict)，供呼叫端依字元上限打包成批次。"""
    units = []
    for cat in ('cases_live', 'cases_poc'):
        for c in industry.get(cat) or []:
            units.append((cat, {
                'name': c.get('name', ''),
                'timeline': _cap(c.get('timeline', '')),
                'scope': _cap(c.get('scope', '')),
                'effect': _cap(c.get('effect', '')),
                'events': _cap(c.get('events', '')),
            }))
    for r in industry.get('adoption_by_region') or []:
        units.append(('adoption_by_region', {
            'region': r.get('region', ''),
            'pct_or_status': _cap(r.get('pct_or_status', ''), 160),
        }))
    for r in industry.get('roadmap') or []:
        units.append(('roadmap', {
            'name': r.get('name', ''),
            'status': _cap(r.get('status', '')),
        }))
    return units


def _compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def build_industry_batches(industry, max_chars=MAX_DATA_CHARS):
    """依字元上限把產業精簡單元貪婪打包成多批；每批回傳compact JSON字串
    （{"key","label","cases_live":[..],...}），長度<=max_chars（單一單元本身超過則自成一批）。"""
    base = {'key': industry.get('key', ''), 'label': industry.get('label', '')}
    batches, cur = [], {}
    def render(d):
        return _compact(dict(base, **d))
    for cat, unit in build_industry_units(industry):
        trial = {k: list(v) for k, v in cur.items()}
        trial.setdefault(cat, []).append(unit)
        if cur and len(render(trial)) > max_chars:
            batches.append(render(cur))
            cur = {cat: [unit]}
        else:
            cur = trial
    if cur:
        batches.append(render(cur))
    return batches


def call_groq_diff_one(industry_label, batch_json, news_snippets):
    """單一產業、單一批次的核對。回傳dict(items, no_change_summary)。
    413時只縮減新聞片段對半重試（資料已在分批時控制在字元上限內）。"""
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
    def build_prompt(news_lines):
        news_text = chr(10).join(news_lines) if news_lines else '（本月未蒐集到相關新聞片段）'
        return f"""以下是「{industry_label}」AI導入結構化參考資料的精簡現況（JSON，僅含比對必要欄位）：

{batch_json}

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
      "industry_key": "候選異動屬於哪個產業，填資料裡的key",
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
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    news_lines = list(news_snippets)
    response = None
    for shrink_round in range(5):
        for model in models:
            try:
                response = client.chat.completions.create(
                    model=model,
                    reasoning_effort="low",
                    messages=[{"role": "system", "content": sys_msg},
                              {"role": "user", "content": build_prompt(news_lines)}],
                    temperature=0.2,
                    max_tokens=3000,
                )
                break
            except GroqAPIStatusError as e:
                if e.status_code == 413:
                    print(f"  → {model} 超出TPM（目前新聞{len(news_lines)}則）...")
                    continue
                raise
        if response is not None:
            break
        if not news_lines:
            break
        news_lines = news_lines[:len(news_lines) // 2]
        print(f"  → 縮減新聞片段至{len(news_lines)}則重試...")
    if response is None:
        raise ValueError("連續多輪（含縮減新聞片段）仍超出Groq TPM限制")
    raw = (response.choices[0].message.content or '').strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    finish_reason = response.choices[0].finish_reason
    if finish_reason == 'length':
        raise ValueError(f"Groq回應被截斷（finish_reason=length，{len(raw)}字元）")
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


def set_last_checked_text(text, industry_key, date_str):
    """文字層級只改指定產業的last_checked值，不重新序列化整份JSON，
    避免把既有flow等陣列的緊湊格式整個展開（git diff只會有last_checked那幾行）。"""
    m = re.search(r'"key":\s*"' + re.escape(industry_key) + r'"', text)
    if not m:
        raise ValueError(f"industry_adoption.json 找不到 key={industry_key}")
    pat = re.compile(r'("last_checked":\s*")[^"]*(")')
    m2 = pat.search(text, m.end())
    if not m2:
        raise ValueError(f"key={industry_key} 找不到 last_checked 欄位")
    return text[:m2.start()] + m2.group(1) + date_str + m2.group(2) + text[m2.end():]


def main():
    print(f"\n{'='*50}")
    print(f"產業導入專區月查證 — {NOW.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    status = load_json(IA_STATUS_PATH, None)
    if status is None or 'industries' not in status:
        print("  ⚠ 找不到 data/industry_adoption.json 或格式不符，中止")
        sys.exit(1)

    print("📰 蒐集各產業AI導入相關新聞（過去一個月，限官方機構來源）...")
    news_by_industry = fetch_industry_news()
    total = sum(len(v) for v in news_by_industry.values())
    print(f"  → 共 {total} 則片段")

    industries = status.get('industries', [])
    all_items, no_change_parts, category_failures, ok_keys = [], [], [], []
    for idx, industry in enumerate(industries):
        key, label = industry.get('key', ''), industry.get('label', '')
        # 新聞分流：只送屬於本產業的片段（fetch時已依industry key分桶）
        news = list(news_by_industry.get(key, []))[:MAX_NEWS_PER_CALL]
        if not news:
            print(f"  → 「{label}」本月無新聞片段，跳過Groq（僅更新查證時間戳）")
            ok_keys.append(key)
            continue
        batches = build_industry_batches(industry)
        print(f"  → 查證「{label}」：{len(batches)}批，新聞{len(news)}則")
        failed = False
        for bi, batch_json in enumerate(batches):
            print(f"    批{bi+1}/{len(batches)}：資料{len(batch_json)}字元（不含新聞）")
            try:
                diff = call_groq_diff_one(label, batch_json, news)
                if not isinstance(diff, dict):
                    raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
                its = [dict(it, industry_key=key) for it in (diff.get('items') or []) if isinstance(it, dict)]
                all_items.extend(its)
                if not its and diff.get('no_change_summary'):
                    no_change_parts.append(f"{label}：{diff['no_change_summary']}")
            except Exception as e:
                print(f"    ⚠ 「{label}」第{bi+1}批查證失敗：{e}")
                category_failures.append(f"{label}（批{bi+1}/{len(batches)}）：{e}")
                failed = True
            # 批次間隔：讓Groq TPM滾動視窗重置（最後一批之後不用等）
            if not (idx == len(industries) - 1 and bi == len(batches) - 1):
                time.sleep(65)
        if not failed:
            ok_keys.append(key)

    pending = {
        'checked_at': DATE_STR,
        'items': all_items,
        'no_change_summary': '' if all_items else '本月查證後判斷現有資料仍準確。',
        'no_change_detail_by_category': no_change_parts,
        'category_failures': category_failures,
    }
    save_json(IA_PENDING_PATH, pending)

    if category_failures:
        print(f"  ⚠ 失敗的產業/批次共{len(category_failures)}項：")
        for f in category_failures:
            print(f"     - {f}")

    # 全部產業都沒成功：不更新last_checked，讓時間戳誠實反映，並以exit 1讓workflow變紅
    if not ok_keys:
        print("  ❌ 所有產業查證皆失敗，不更新任何 last_checked")
        sys.exit(1)

    # last_checked只對成功的產業做文字層級更新（唯一允許自動改寫的部分）
    with open(IA_STATUS_PATH, 'r', encoding='utf-8') as f:
        text = f.read()
    for key in ok_keys:
        text = set_last_checked_text(text, key, DATE_STR)
    with open(IA_STATUS_PATH, 'w', encoding='utf-8') as f:
        f.write(text)

    if all_items:
        print(f"  → 偵測到 {len(all_items)} 項候選異動，已寫入 data/industry_adoption_pending_review.json（未套用，待人工複核）")
    else:
        print("  → 本月查證後無需更新")
    # git commit/push 交給 GitHub Actions 的 git-auto-commit-action 處理，
    # 本腳本只負責寫檔案，不自己動 git
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
