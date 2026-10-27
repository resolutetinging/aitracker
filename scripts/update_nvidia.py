#!/usr/bin/env python3
"""
NVIDIA 專區週查證腳本
獨立於每日新聞 pipeline（update_data.py）之外——這裡驗證的是「既有結構化參考
資料（金字塔/案例/roadmap/聯盟）是否過時」，跟「今天有沒有新聞」的任務性質、
排程頻率都不同，所以獨立成一支腳本、獨立週排程觸發（見
.github/workflows/weekly-nvidia-update.yml）。

安全設計（鐵律）：本腳本絕不直接改寫 data/nv_status.json（那是實際顯示在頁面上、
看起來權威的參考資料，LLM若把幻覺內容寫進去會誤導使用者）。所有候選異動一律
寫進 data/nv_pending_review.json，前端只顯示「本週查證偵測到 N 項候選異動」的
提示，實際套用需要使用者告知 Claude 人工複核後手動更新 nv_status.json。
"""
import json, os, re, time, smtplib
from datetime import datetime, timezone, timedelta
from groq import Groq
from groq import APIStatusError as GroqAPIStatusError

TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')

REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
NV_STATUS_PATH = os.path.join(REPO_DIR, 'data', 'nv_status.json')
NV_PENDING_PATH = os.path.join(REPO_DIR, 'data', 'nv_pending_review.json')
NV_ECO_PATH = os.path.join(REPO_DIR, 'data', 'nv_ecosystem.json')
HISTORY_PATH = os.path.join(REPO_DIR, 'data', 'history.json')

# 09-14新增：NVIDIA相關daily新聞關鍵字（第一版，不追求完美，使用者原話「先跑
# 起來觀察效果」）——比對title+body，只要出現任一關鍵字就視為候選。
NVIDIA_KEYWORD_PAT = re.compile(
    r'nvidia|輝達|黃仁勳|jensen\s*huang|geforce|cuda\b|cudnn|tensorrt|blackwell|'
    r'\brubin\b|h100|h200|b200|gb200|nvlink|dgx\b|omniverse',
    re.IGNORECASE
)

CATEGORY_LABELS = {
    'pyramid': '技術金字塔',
    'cases_live': '應用案例（已上線）',
    'cases_poc': '應用案例（POC）',
    'roadmap': '產品 Roadmap',
    'alliances': '多方聯盟／產業組織',
    'governance': '生態系權力布局／合規治理',
    'ecosystem': '生態系夥伴',
}
ECO_TIER_LABELS = {'upstream': '上游', 'midstream': '中游', 'downstream': '下游'}
ECO_REL_LABELS = {'investment': '投資', 'partnership': '合作', 'both': '投資＋合作'}
ACTION_LABELS = {'update': '更新既有項目', 'add': '新增項目'}

# 2026-09-22修TPM超限bug：原本6大類一次打包送1次Groq請求，current_status整份
# 序列化後約9,302字元／估算約6,200 token，已逼近甚至超過Groq免費tier 6,000 TPM
# 上限；即使news_snippets縮到0則仍超限，證實問題根本在current_status本身太大，
# 不在新聞多寡。比照Renewable Tracker 09-01同款修法（scripts/update_renewable.py
# 的CATEGORY_CONFIG／call_groq_diff_one），把6大類拆成各自獨立的Groq請求，
# category_enum限制Groq單次只能回傳這一類的category值，避免6類合併時enum混用。
# news_snippets刻意不比照renewable依label拆分——NVIDIA新聞量本來就小（約7則
# 上下），6類共用同一份完整news_snippets即可，不需要額外設計過濾邏輯。
CATEGORY_CONFIG = [
    {'label': '技術金字塔', 'keys': ['pyramid', 'pyramid_sources'], 'category_enum': 'pyramid'},
    {'label': '應用案例（已上線）', 'keys': ['cases_live'], 'category_enum': 'cases_live'},
    {'label': '應用案例（POC）', 'keys': ['cases_poc'], 'category_enum': 'cases_poc'},
    {'label': '產品 Roadmap', 'keys': ['roadmap'], 'category_enum': 'roadmap'},
    # 10-27：alliances 改定義為「NVIDIA 加入或主導的多方聯盟、產業組織、標準與政策聯盟」，
    # 雙邊合作移到第 7 類 ecosystem；alliance_overview 改為 [{text,src}] 條列，舊 key
    # alliance_overview_src 已移除（每點各自附來源）
    {'label': '多方聯盟／產業組織', 'keys': ['alliances', 'alliance_overview'], 'category_enum': 'alliances'},
    {'label': '生態系權力布局／合規治理', 'keys': ['governance', 'governance_stages', 'governance_links'], 'category_enum': 'governance'},
    # 10-27新增第 7 類：生態系夥伴（data/nv_ecosystem.json，36 節點）。全文約 14k 字送 Groq
    # 必超 6000 TPM，只送精簡欄位 name/tier/relation/amount/date（見 build_eco_category_data）；
    # 候選一律寫 nv_pending_review.json，本腳本絕不寫 nv_ecosystem.json
    {'label': '生態系夥伴', 'keys': None, 'category_enum': 'ecosystem', 'source': 'ecosystem'},
]
ECO_SLIM_FIELDS = ('name', 'tier', 'relation', 'amount', 'date')
# 10-27：這組 DDG 查詢的新聞只送 alliances 類，其他 6 類不帶（避免每類 prompt 都多出約 6 則、逼近 6000 TPM）
ALLIANCE_NEWS_LABEL = '多方聯盟/標準組織'
ALLIANCE_NEWS_PREFIX = '[' + ALLIANCE_NEWS_LABEL + ']'


def news_for_category(news, category_enum):
    if category_enum == 'alliances':
        return list(news)
    return [n for n in news if not str(n).startswith(ALLIANCE_NEWS_PREFIX)]
ALLIANCE_SLIM_FIELDS = ('name', 'type', 'nv_role', 'progress', 'date')


def build_eco_category_data(eco):
    """生態系夥伴類只送精簡欄位，且每個節點壓成一行「name|tier|relation|amount|date」
    字串（比逐筆 JSON 物件少掉重複的欄位名），避免單次請求超過 Groq 6000 TPM。"""
    nodes = [n for n in ((eco or {}).get('nodes') or []) if isinstance(n, dict)]
    if not nodes:
        raise ValueError('data/nv_ecosystem.json 無節點或讀取失敗')
    rows = ['|'.join(str(n.get(k) or '').replace('|', '／') for k in ECO_SLIM_FIELDS) for n in nodes]
    return {'format': '|'.join(ECO_SLIM_FIELDS), 'nodes': rows}


def build_alliance_category_data(status):
    """多方聯盟類只送核對推進所需欄位（成員證據、意義等長文字不送，約省一半字數），
    alliance_overview 只送每點文字。"""
    al = [a for a in (status.get('alliances') or []) if isinstance(a, dict)]
    ov = status.get('alliance_overview') or []
    ov_txt = [o.get('text', '') if isinstance(o, dict) else str(o) for o in (ov if isinstance(ov, list) else [ov])]
    return {'alliances': [{k: a.get(k, '') for k in ALLIANCE_SLIM_FIELDS} for a in al],
            'alliance_overview': ov_txt}


# 各類專屬的判斷規則與候選 schema（沒列到的類別沿用 call_groq_diff_one 內建的通用 schema）
CATEGORY_RULES = {
    'alliances': (
        "本類只追蹤「NVIDIA 加入或主導的多方聯盟、產業組織、標準與政策聯盟」（三方以上參與的"
        "標準組織、產業聯盟、開源聯盟、聯署公開信、政府計畫的產業合作等）的推進，例如規格發布、"
        "新成員加入、政策提案、治理移轉。"
        "排除：NVIDIA 與單一公司或單一政府的雙邊投資、合作、採購、供應協議（那屬於生態系夥伴類，不要在本類提出）。"
        "新增組織前，新聞必須明確顯示 NVIDIA 是該組織的成員、發起者或簽署方；看不出來就不要新增。"
        "alliance_overview 是整體動態條列的文字，若要更新某一點，target_name 填該點的原文字。"
    ),
    'ecosystem': (
        "本類資料是 NVIDIA 生態系夥伴節點的精簡清單，nodes 每一行依序為 name|tier|relation|amount|date（空欄代表未揭露）。"
        "只找 NVIDIA 與單一公司或單一政府的雙邊投資或合作的新消息：既有節點的金額、日期、關係有明確新聞變動，"
        "或出現新的重大投資／合作夥伴。tier 只能填 upstream（供應鏈上游）、midstream（雲端、系統、網路、融資等中游）、"
        "downstream（模型、產業應用、主權 AI 等下游）；relation 只能填 investment、partnership、both。"
        "多方聯盟或產業組織不屬於本類，不要提出。"
    ),
}
CATEGORY_ITEM_SCHEMA = {
    'alliances': """{
      "category": "alliances",
      "action": "update|add",
      "target_name": "若action=update，填現有資料裡對應組織的name（或alliance_overview該點的text）；若action=add則留空",
      "name": "組織名稱（中文名稱＋英文原名）",
      "type": "標準|產業聯盟|政策|開源",
      "nv_role": "NVIDIA 在該組織的角色（發起者／創始成員／董事會成員／一般會員／簽署方等）",
      "members_note": "代表成員（3到5家）",
      "progress": "最近推進（規格發布、新成員、政策提案等）",
      "date": "最近推進日期（YYYY-MM 或 YYYY-MM-DD）",
      "significance": "意義一句話",
      "reason": "為何提出這個異動，具體說明新聞依據",
      "source": "新聞來源URL"
    }""",
    'ecosystem': """{
      "category": "ecosystem",
      "action": "update|add",
      "target_name": "若action=update，填現有節點的name；若action=add則留空",
      "name": "公司或政府名稱",
      "tier": "upstream|midstream|downstream",
      "relation": "investment|partnership|both",
      "amount": "金額（新聞未提就留空）",
      "date": "YYYY-MM 或 YYYY-MM-DD",
      "item": "合作或投資項目一句話",
      "reason": "為何提出這個異動，具體說明新聞依據",
      "source": "新聞來源URL"
    }""",
}


def fetch_nvidia_news():
    """用DDG查最近一週NVIDIA相關新聞，涵蓋5個分類分別對應頁面的5個區塊"""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("  ⚠ 找不到 ddgs/duckduckgo_search 套件，本次跳過新聞蒐集")
            return []
    queries = [
        ("產品/晶片roadmap", "NVIDIA GPU chip roadmap Rubin Feynman announcement"),
        ("Omniverse應用案例", "NVIDIA Omniverse deployment customer case study"),
        ("聯盟/夥伴關係", "NVIDIA partnership investment alliance sovereign AI"),
        # 10-27：alliances 改追蹤多方聯盟／產業組織／標準與政策聯盟的推進，補一組對應查詢
        (ALLIANCE_NEWS_LABEL, "NVIDIA consortium coalition standard specification founding member joins"),
        ("財報/產能", "NVIDIA earnings capacity supply chain update"),
        # 08-13新增：生態系權力布局／合規治理維度（NVLink Fusion開放策略、CUDA鎖定、
        # 生態系資本投資、AI治理聯盟、主權AI合規部署），對應governance分類
        ("生態系布局/合規治理", "NVIDIA ecosystem strategy standard governance compliance sovereign AI investment"),
    ]
    snippets = []
    ddgs = DDGS()
    for label, q in queries:
        for attempt in range(3):
            try:
                results = list(ddgs.news(q, max_results=6, timelimit="w"))
                for r in results:
                    link = r.get('url', '')
                    url_part = f" | SOURCE_URL:{link}" if link else ""
                    snippets.append(f"[{label}] {r.get('title','')} — {r.get('body','')[:200]}{url_part}")
                print(f"  DDG '{label}': {len(results)} results")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                else:
                    print(f"  DDG '{label}' failed after 3 attempts: {e}")
    return snippets


def collect_nvidia_daily_items(history):
    """09-14新增「近期事件」週更：跟其餘6大類不同，這裡不對外重新查證（不像
    fetch_nvidia_news()那樣重新DDG搜尋），改成直接從data/history.json（daily
    pipeline每天已經call_groq()驗證、分級過的hw/corp/app條目，目前保留近7天）
    篩出NVIDIA相關候選。輸入源頭已經是每日驗證過的真實新聞，這正是下面
    merge_recent_events()敢直接寫回nv_status.json（不用像其他6大類一樣進
    nv_pending_review.json排隊等人工複核）的風險基礎——不是LLM自己上網查、
    沒有查無實據的幻覺風險，只是把已知真實新聞做分類跟時間線排序。"""
    items = []
    seen = set()
    for day in history:
        for section in ('hw', 'corp', 'app'):
            for it in day.get(section, []) or []:
                src = it.get('source', '')
                if not src or src in seen:
                    continue  # 佔位卡(source空字串)或本週已收錄過的同一來源
                if it.get('rating') == 'noise':
                    continue
                text = f"{it.get('title','')} {it.get('body','')}"
                if NVIDIA_KEYWORD_PAT.search(text):
                    seen.add(src)
                    items.append({
                        'date': day.get('date', ''),
                        'title': it.get('title', ''),
                        'body': it.get('body', ''),
                        'source': src,
                    })
    return items


def call_groq_events(existing_events, candidates):
    """把篩出的NVIDIA相關daily新聞候選交給LLM，判斷歸入既有recent_events的
    thread（同故事線延續）或建立新事件，並給confidence（high/low）。跟
    call_groq_diff()的差異：這裡的news本身已經是daily pipeline驗證過的真實
    新聞，不是LLM自己上網查來的，所以不需要「極度保守、沒證據就不提」那套
    防幻覺邏輯，只需要做分類判斷。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是AI供應鏈分析師，任務是把已知為真實可信的NVIDIA相關新聞歸類進"
        "「近期事件」時間軸。只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "這些新聞的真實性不需要質疑，你的任務只有分類與判斷是否延續既有故事線。"
        "confidence欄位：這則新聞明確屬於某個既有thread的延續、或明顯是獨立且"
        "值得記錄的重要事件，填'high'；分類、故事線歸屬、或是否值得記錄有疑慮"
        "時，填'low'（仍要輸出，不要因為不確定就整則捨棄）。"
        "太瑣碎或明顯重複既有事件內容的新聞，直接不輸出該則即可。"
    )
    existing_threads = sorted(set(e.get('thread') for e in existing_events if e.get('thread')))

    def build_prompt(cand_list):
        cand_text = '\n'.join(
            f"[{c['date']}] {c['title']} — {c['body'][:200]} | SOURCE:{c['source']}"
            for c in cand_list
        )
        return f"""既有「近期事件」故事線名稱（thread）清單：{json.dumps(existing_threads, ensure_ascii=False)}

以下是本週從每日新聞篩出的NVIDIA相關候選：
{cand_text}

針對每一則，判斷：
1. thread：若屬於上面某個既有故事線的延續，填該故事線名稱（一字不差）；
   若是全新獨立事件，thread留空字串
2. category：簡短分類詞（例如：財報動態、產品發布、政府調查、合作聯盟、供應鏈）
3. summary：2-3句話中文摘要，說明具體事實
4. confidence：'high'或'low'

輸出格式（純JSON）：
{{"events":[{{"date":"YYYY-MM-DD","title":"...","thread":"","category":"...","summary":"...","confidence":"high|low","source":"..."}}]}}"""

    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    cand_list = list(candidates)
    response = None
    for shrink_round in range(3):
        for model in models:
            try:
                response = client.chat.completions.create(
                    model=model,
                    reasoning_effort="low",
                    messages=[{"role": "system", "content": sys_msg},
                              {"role": "user", "content": build_prompt(cand_list)}],
                    temperature=0.2,
                    max_tokens=3000,
                )
                break
            except GroqAPIStatusError as e:
                if e.status_code == 413:
                    continue
                raise
        if response is not None:
            break
        if not cand_list:
            break
        cand_list = cand_list[:len(cand_list)//2]
    if response is None:
        raise ValueError("call_groq_events: 連續縮減候選新聞後仍超出Groq TPM限制")
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    if response.choices[0].finish_reason == 'length':
        raise ValueError(f"Groq回應被截斷（finish_reason=length，{len(raw)}字元）")
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw).get('events', [])


def merge_recent_events(status, new_events):
    """把LLM分類好的近期事件併進nv_status.json的recent_events。**09-14鐵律
    例外**：跟其餘6大類「絕不直接改寫nv_status.json」不同，這裡高信心與
    低信心的候選都直接寫入（不進nv_pending_review.json排隊）——因為輸入源頭
    是daily pipeline已驗證的新聞而非LLM自行查證，風險本質不同，使用者已
    明確同意。低信心的加needs_review:true讓前端直接顯示「待確認」徽章，
    不需要另外開口問使用者，使用者瀏覽頁面時就能看到。
    去重：用source URL比對，避免history.json近7天視窗跨週重疊造成重複收錄。
    短期不做刪除/歸檔（archive），recent_events持續增長，這是使用者明確決定。
    回傳新增筆數，供main()印log。"""
    existing = status.setdefault('recent_events', [])
    existing_sources = {e.get('source') or e.get('src') for e in existing if e.get('source') or e.get('src')}
    added = 0
    for ev in new_events:
        src = ev.get('source', '')
        if src and src in existing_sources:
            continue
        ev_date = ev.get('date', '')
        if not (isinstance(ev_date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', ev_date)):
            ev_date = ''  # 10-05 XSS：格式不合的日期清空，保留事件不中斷排程
        entry = {
            'date': ev_date,
            'title': ev.get('title', ''),
            'category': ev.get('category', ''),
            'summary': ev.get('summary', ''),
            'src': src,
            'src_note': '',
        }
        if ev.get('thread'):
            entry['thread'] = ev['thread']
        if ev.get('confidence') == 'low':
            entry['needs_review'] = True
        existing.append(entry)
        if src:
            existing_sources.add(src)
        added += 1
    return added


def update_recent_events(status):
    """近期事件週更主流程，main()一開始就呼叫，跟後面6大類查證（DDG搜尋）
    完全獨立、互不影響——即使DDG被限流或Groq call_groq_diff()失敗，近期事件
    仍應該正常運作，因為兩者的資料來源、風險模型都不同。"""
    history = load_json(HISTORY_PATH, [])
    candidates = collect_nvidia_daily_items(history)
    print(f"  → 從data/history.json篩出 {len(candidates)} 則NVIDIA相關daily新聞候選")
    if not candidates:
        print("  → 本週無NVIDIA相關daily新聞候選，略過近期事件更新")
        return 0
    try:
        new_events = call_groq_events(status.get('recent_events', []), candidates)
        added = merge_recent_events(status, new_events)
        print(f"  → 近期事件新增 {added} 則（{sum(1 for e in new_events if e.get('confidence')=='low')} 則待確認）")
        return added
    except Exception as e:
        # 近期事件失敗不可讓整個週查證中止，其餘6大類（DDG搜尋+call_groq_diff）
        # 要照常執行；也不send_email告知失敗——這是輕量級的分類補充功能，
        # 不像call_groq_diff()失敗那樣代表整週查證都沒發生
        print(f"  ⚠ 近期事件分類失敗，本次略過（不影響其餘6大類查證）：{e}")
        return 0


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def call_groq_diff_one(category_label, category_data, category_enum, news_snippets, client=None):
    """核對「單一大類」的資料是否過時，只回報有明確新聞佐證的候選異動。
    2026-09-22改造：原本call_groq_diff()一次打包6大類送1個Groq請求，current_status
    整份序列化後約9,302字元／估算約6,200 token，逼近甚至超過Groq免費tier
    6,000 TPM上限，即使news_snippets縮到0則仍超限——問題在current_status本身
    太大，不在新聞多寡，比照Renewable Tracker 09-01同款修法（見上方
    CATEGORY_CONFIG註解），拆成6類各自獨立呼叫本函式一次，每次只帶單一大類的
    資料，system prompt/保守原則不變。
    10-27：client 參數供本機 mock 測試注入（不傳就照常建立真的 Groq client）。"""
    if client is None:
        client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是AI供應鏈分析師，任務是核對一份既有的NVIDIA結構化參考資料"
        "（這次只給你其中一個大類）是否過時。只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "極度保守：沒有明確新聞佐證的欄位一律維持原樣、不提出更新建議；"
        "禁止臆測、禁止捏造來源URL、禁止把不確定的傳聞當成確定事實。"
        "多數週查證後的正確答案就是「沒有任何異動」，回傳空items陣列是完全正常且被期待的結果，"
        "不需要為了顯得有查證成果而硬湊出候選異動。"
    )
    # 10-27：alliances／ecosystem 有各自的判斷規則與候選 schema（CATEGORY_RULES／CATEGORY_ITEM_SCHEMA），
    # 其餘類別沿用下方通用 schema；先算成變數再插入 f-string（GA 為 Python 3.11，{} 內禁反斜線）
    generic_schema = (
        '{' + chr(10)
        + '      "category": "' + category_enum + '",' + chr(10)
        + '      "action": "update|add",' + chr(10)
        + '      "target_name": "若action=update，填現有資料裡對應項目的name或label文字；若action=add則留空",' + chr(10)
        + '      "name": "項目/公司名稱",' + chr(10)
        + '      "desc": "更新後或新增的描述文字",' + chr(10)
        + '      "status": "狀態文字（cases/roadmap/governance類別適用，pyramid類別留空）",' + chr(10)
        + '      "reason": "為何提出這個異動，具體說明新聞依據",' + chr(10)
        + '      "source": "新聞來源URL"' + chr(10)
        + '    }'
    )
    item_schema = CATEGORY_ITEM_SCHEMA.get(category_enum, generic_schema)
    rules = CATEGORY_RULES.get(category_enum, '')
    rules_text = ('本類判斷規則：' + rules + chr(10) + chr(10)) if rules else ''

    def build_prompt(news_list):
        data_json = json.dumps(category_data, ensure_ascii=False, separators=(',', ':'))
        news_text = chr(10).join(news_list) if news_list else '（本週未蒐集到相關新聞片段）'
        return f"""以下是「{category_label}」這個大類目前的NVIDIA結構化參考資料現況（JSON，只含
這一個大類，不含其他大類，那些不在本次查證範圍內，不要對它們提出異動）：

{data_json}

以下是過去一週蒐集到的NVIDIA相關新聞片段，每則片段結尾若有「| SOURCE_URL:網址」就是該則新聞的
原始來源網址；source欄位只能填這裡實際出現過的SOURCE_URL，禁止自己編造或憑記憶生成網址：

{news_text}

{rules_text}請核對上述新聞是否讓現有資料的任何欄位過時或不準確，只針對有明確新聞佐證的部分提出候選異動。
不要因為沒有新聞佐證就自己推測任何欄位「應該」要改；找不到能對應到具體新聞的變化就不要提出，
空陣列是完全正常的結果。

輸出格式（純JSON）：
{{
  "items": [
    {item_schema}
  ],
  "no_change_summary": "若items為空陣列，一句話說明本大類本週查證後判斷現有資料仍準確；若items非空則留空字串"
}}"""
    # 2026-08-24：8/24第一次修復（fallback model 120b→20b）實測時被使用者截圖
    # 抓到反效果——120b失敗後fallback到20b，但20b的TPM上限（實測8000）比120b
    # 更小，對「內容太大」的413錯誤來說換小模型只會更早爆掉，方向錯了。
    # 真正該做的是縮減內容本身：(a) category_data改用compact JSON（無indent）；
    # (b) 413時砍news_snippets數量對半重試，news是輔助佐證，砍了只是佐證變少。
    # 2026-09-22：既有這套413防護機制原封不動保留（拆成單一大類後現在幾乎不該
    # 再觸發，但仍留著防禦單一大類本身也異常肥大的邊緣情況）。最多縮5輪，5輪
    # 都失敗才真的放棄並拋出例外（讓main()記錄該大類失敗，其他大類照常繼續）。
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    news_list = list(news_snippets)
    response = None
    shrink_round = 0
    for shrink_round in range(5):
        for model in models:
            try:
                response = client.chat.completions.create(
                    model=model,
                    reasoning_effort="low",
                    messages=[{"role": "system", "content": sys_msg},
                              {"role": "user", "content": build_prompt(news_list)}],
                    temperature=0.2,
                    max_tokens=3000,
                )
                break
            except GroqAPIStatusError as e:
                if e.status_code == 413:
                    print(f"    → {model} 超出TPM（目前新聞{len(news_list)}則）...")
                    continue
                raise
        if response is not None:
            break
        if not news_list:
            break
        news_list = news_list[:len(news_list)//2]
        print(f"    → 縮減新聞片段至{len(news_list)}則重試...")
    if response is None:
        raise ValueError(f"連續{shrink_round+1}輪（含縮減新聞片段至{len(news_list)}則）仍超出Groq TPM限制")
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    finish_reason = response.choices[0].finish_reason
    if finish_reason == 'length':
        raise ValueError(f"Groq回應被截斷（finish_reason=length，{len(raw)}字元）")
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


import html as _html_mod


def _esc(x):
    """信件HTML插值用：外部來源／LLM文字一律escape。"""
    return _html_mod.escape(str(x if x is not None else ''), quote=True)


def _safe_url(u):
    """href只准http(s)://開頭，其餘回傳空字串（呼叫端改為只顯示文字）。"""
    u = str(u or '').strip()
    return u if re.match(r'^https?://', u, re.I) else ''


def build_overview_html(status, items=None):
    """08-17第二版：使用者澄清這封信要的是「一週總結」——不管本週有沒有偵測到候選異動，
    都要讓使用者看一眼就了解NVIDIA各block（技術堆疊/案例/roadmap/聯盟/生態系布局）
    當下的完整現況，週跟週之間本來就會有大量重複內容（架構圖平常不會變）是預期行為，
    不是要避免的東西。所以這個區塊改成每週信件都固定包含，不是只有「無候選異動」
    才出現的備案內容；且每個項目附一行實質desc/status，不是只列名字。"""
    def item_lines(key, show_status=False):
        # 08-24第五版：使用者截圖回報status文字（尤其⚠️查證更正這類長句）直接接在
        # 標題後面同一行，長句一多整行就疊字擠壓、看不出標題跟狀態的分界。改成比照
        # daily新聞信（update_data.py send_email的section_html）的卡片寫法——每則
        # 左側加border-left色條當引用線，標題單獨一行，狀態、內文各自退縮在自己的
        # 引用線下一行，形成標題→狀態→內文的階層（比照使用者手繪的樹狀縮排）。
        # 08-24第六版：使用者截圖回報巢狀引用線太多層（卡片外層綠色+狀態amber+
        # 內文灰色三條），要求只留最外層綠色那條，狀態/內文改純縮排、不再各自加
        # border-left，靠顏色（amber/灰）跟縮排本身區分階層即可，不需要疊三層線。
        rows = ''
        for it in status.get(key, []):
            name = it.get('name') or it.get('label') or ''
            detail = it.get('items') or it.get('desc') or ''
            status_html = f'<div style="margin-top:5px;padding-left:12px;font-size:12px;color:#8a6030;line-height:1.6;">{_esc(it["status"])}</div>' if show_status and it.get('status') else ''
            detail_html = f'<div style="margin-top:5px;padding-left:12px;font-size:12px;color:#6a6460;line-height:1.6;">{_esc(detail)}</div>' if detail else ''
            rows += f'''<div style="background:#faf9f7;border-left:3px solid #6a8a20;border-radius:0 6px 6px 0;padding:10px 14px;margin:8px 0;">
              <div style="font-size:13px;font-weight:700;color:#2c2a28;line-height:1.5;">{_esc(name)}</div>
              {status_html}
              {detail_html}
            </div>'''
        return rows

    def alliance_lines():
        # 10-27：聯盟改為多方組織卡，週信列每張卡的重點（類型／NVIDIA 角色／最近推進＋日期／來源）
        rows = ''
        for it in status.get('alliances', []):
            if not isinstance(it, dict):
                continue
            meta = '｜'.join(x for x in [it.get('type', ''), it.get('nv_role', '')] if x)
            if it.get('verify') == 'partial':
                meta = (meta + '｜' if meta else '') + '部分核對'
            prog = it.get('progress') or it.get('desc') or ''
            date_txt = it.get('date') or ''
            prog_line = prog + ('（' + date_txt + '）' if date_txt else '')
            meta_html = f'<div style="margin-top:4px;padding-left:12px;font-size:11.5px;color:#8a6030;line-height:1.6;">{_esc(meta)}</div>' if meta else ''
            prog_html = f'<div style="margin-top:4px;padding-left:12px;font-size:12px;color:#6a6460;line-height:1.6;">{_esc(prog_line)}</div>' if prog_line else ''
            src_url = _safe_url(it.get('src'))
            src_html = f'<div style="margin-top:4px;padding-left:12px;font-size:11px;"><a href="{_esc(src_url)}" style="color:#6a8a20;">來源連結 →</a></div>' if src_url else ''
            rows += f'''<div style="background:#faf9f7;border-left:3px solid #6a8a20;border-radius:0 6px 6px 0;padding:10px 14px;margin:8px 0;">
              <div style="font-size:13px;font-weight:700;color:#2c2a28;line-height:1.5;">{_esc(it.get('name', ''))}</div>
              {meta_html}
              {prog_html}
              {src_html}
            </div>'''
        return rows

    def eco_summary():
        # 10-27：生態系只寫家數與本週候選數，不列全部節點
        eco = load_json(NV_ECO_PATH, None) or {}
        nodes = [n for n in (eco.get('nodes') or []) if isinstance(n, dict)]
        cnt = {k: sum(1 for n in nodes if n.get('tier') == k) for k in ECO_TIER_LABELS}
        m = sum(1 for it in (items or []) if isinstance(it, dict) and it.get('category') == 'ecosystem')
        tier_txt = '／'.join(ECO_TIER_LABELS[k] + ' ' + str(cnt[k]) + ' 家' for k in ECO_TIER_LABELS)
        line = f'生態系 {len(nodes)} 家（{tier_txt}），本週候選 {m} 筆'
        return f'''<div style="margin:16px 0;">
          <div style="font-size:11.5px;font-weight:700;color:#6a8a20;margin-bottom:6px;">🌐 生態系夥伴</div>
          <div style="background:#faf9f7;border-left:3px solid #6a8a20;border-radius:0 6px 6px 0;padding:10px 14px;margin:8px 0;font-size:12.5px;color:#4a4744;line-height:1.6;">{_esc(line)}</div>
        </div>'''

    sections = [
        ('🔺', '技術堆疊', 'pyramid', False),
        ('✅', '已上線案例', 'cases_live', False),
        ('🔵', '概念驗證中', 'cases_poc', False),
        ('🗺', '產品 Roadmap', 'roadmap', True),
        ('🤝', '多方聯盟／產業組織', 'alliances', True),
        ('🔗', '生態系布局', 'governance', True),
    ]
    blocks = ''
    for emoji, label, key, show_status in sections:
        n = len(status.get(key, []))
        lines_html = alliance_lines() if key == 'alliances' else item_lines(key, show_status)
        blocks += f'''
        <div style="margin:16px 0;">
          <div style="font-size:11.5px;font-weight:700;color:#6a8a20;margin-bottom:6px;">{emoji} {label}（{n}）</div>
          {lines_html}
        </div>'''
    blocks += eco_summary()
    return f'''
    <div style="margin-top:8px;">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;margin-bottom:4px;">📌 本週 NVIDIA 專區現況總結</div>
      {blocks}
    </div>'''


def send_email(items, no_change_summary, status, category_failures=None):
    """獨立信件，跟daily的AI產業動態信完全分開發送、不合併內容。
    收件人只送GitHub Secret設定的NOTIFY_EMAIL（比照daily的secret_recipients），
    不碰data/email_config.json的飛鴿公開名單——那份名單是給每日AI新聞訂閱的，
    NVIDIA週查證是另一種性質的內容，不應該未經同意就多推給那些人。
    2026-09-22新增category_failures：拆成6大類獨立查證後，若部分大類失敗，
    比照Renewable Tracker send_email()的做法，獨立於items/no_change_summary
    之外渲染一個提醒區塊，不管items是否為空都會顯示，避免失敗訊息被靜默吞掉。"""
    user = os.environ.get('GMAIL_USER', '').replace('\xa0', '').replace(' ', '').strip()
    pwd = os.environ.get('GMAIL_APP_PASSWORD', '').replace('\xa0', '').replace(' ', '').strip()
    secret_to = os.environ.get('NOTIFY_EMAIL', user).replace('\xa0', '').replace(' ', '').strip()
    recipients = [a.strip() for a in secret_to.split(',') if a.strip()]

    if not user or not pwd or not recipients:
        print("  → Email 未設定，略過。")
        return

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    subject = f'🟢 NVIDIA 概況 {DATE_STR}'
    # 08-17第二版：使用者澄清這封信的定位是「每週固定總結」，不是「有異動才有內容」的
    # 通知信——候選異動（如果有）只是額外補充在最上面，下面的現況總結每週都要有，
    # 週跟週之間本來就會有大量重複（技術堆疊平常不會變）是預期行為
    changes_html = ''
    if items:
        cards = ''
        for it in items:
            cat = _esc(CATEGORY_LABELS.get(it.get('category', ''), it.get('category', '')))
            act = _esc(ACTION_LABELS.get(it.get('action', ''), it.get('action', '')))
            src_url = _safe_url(it.get('source'))
            if src_url:
                src = f'<div style="margin-top:6px;font-size:11px;"><a href="{_esc(src_url)}" style="color:#6a8a20;">來源連結 →</a></div>'
            elif it.get('source'):
                src = '<div style="margin-top:6px;font-size:11px;color:#9e9890;">來源連結</div>'
            else:
                src = ''
            status_line = f'<div style="font-size:12px;color:#6a6460;margin-top:4px;">狀態：{_esc(it["status"])}</div>' if it.get('status') else ''
            # 10-27：alliances／ecosystem 候選沒有 desc，改用各自欄位組出摘要與附加資訊
            cat_key = it.get('category', '')
            if cat_key == 'alliances':
                cand_desc = it.get('desc') or it.get('progress') or ''
                extra = [it.get('type', ''), it.get('nv_role', ''), it.get('date', '')]
            elif cat_key == 'ecosystem':
                cand_desc = it.get('desc') or it.get('item') or ''
                extra = [ECO_TIER_LABELS.get(it.get('tier', ''), it.get('tier', '')),
                         ECO_REL_LABELS.get(it.get('relation', ''), it.get('relation', '')),
                         it.get('amount', ''), it.get('date', '')]
            else:
                cand_desc = it.get('desc', '')
                extra = []
            extra_txt = '｜'.join(str(x) for x in extra if x)
            extra_line = f'<div style="font-size:12px;color:#8a6030;margin-top:4px;">{_esc(extra_txt)}</div>' if extra_txt else ''
            cards += f'''
            <div style="background:#faf9f7;border-left:3px solid #6a8a20;padding:14px 16px;margin:10px 0;border-radius:0 6px 6px 0;">
              <div style="display:flex;gap:8px;margin-bottom:6px;">
                <span style="font-size:11px;font-weight:700;color:#6a8a20;background:#6a8a2018;padding:2px 8px;border-radius:10px;">{cat}</span>
                <span style="font-size:11px;color:#888;">{act}</span>
              </div>
              <div style="font-size:14px;font-weight:700;color:#2c2a28;margin-bottom:6px;">{_esc(it.get("name",""))}</div>
              <div style="font-size:13px;color:#4a4744;line-height:1.6;margin-bottom:6px;">{_esc(cand_desc)}</div>
              {extra_line}
              {status_line}
              <div style="background:#f0ede9;border-radius:5px;padding:8px 12px;font-size:12px;color:#6a6460;margin-top:8px;">
                <span style="font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;display:block;margin-bottom:4px;">查證依據</span>
                {_esc(it.get("reason",""))}
              </div>
              {src}
            </div>'''
        changes_html = f'''
        <div style="margin-bottom:20px;">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;margin-bottom:8px;">🆕 本週偵測到候選異動（待人工複核）</div>
          {cards}
        </div>'''
    else:
        summary = no_change_summary or '本週查證後判斷現有資料仍準確。'
        changes_html = f'''
        <div style="background:#faf9f7;border-left:3px solid #6a8a20;padding:14px 16px;border-radius:0 6px 6px 0;font-size:13px;color:#4a4744;line-height:1.6;margin-bottom:20px;">
          {_esc(summary)}
        </div>'''
    fail_html = ''
    if category_failures:
        fail_note = '部分類別查證失敗（' + '；'.join(category_failures) + '），其餘分類查證結果如常。'
        fail_html = f'''
        <div style="background:#faf3ea;border-left:3px solid #c08040;padding:12px 16px;border-radius:0 6px 6px 0;font-size:12px;color:#8a5a30;line-height:1.6;margin-bottom:16px;">
          ⚠️ {_esc(fail_note)}
        </div>'''
    body_html = fail_html + changes_html + build_overview_html(status, items)

    html = f'''<html><body style="font-family:'Segoe UI',sans-serif;max-width:620px;margin:auto;padding:0;background:#eceae6;color:#2c2a28;">
      <div style="background:#faf9f7;padding:24px 28px;">
        <div style="border-bottom:1px solid #d8d4ce;padding-bottom:16px;margin-bottom:20px;">
          <div style="font-size:20px;font-weight:800;color:#2c2a28;">🟢 NVIDIA 概況</div>
          <div style="font-size:13px;color:#9e9890;margin-top:4px;">{DATE_STR} &nbsp;·&nbsp; 每週自動查證</div>
        </div>
        {body_html}
        <div style="border-top:1px solid #d8d4ce;padding-top:16px;margin-top:20px;text-align:center;">
          <a href="https://resolutetinging.github.io/aitracker/ai_tracker_v6.html" style="display:inline-block;background:#6a8a20;color:#fff;padding:10px 24px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;">🔗 查看完整 Dashboard →</a>
          <div style="font-size:11px;color:#b0b0b0;margin-top:12px;">AI Tracker · NVIDIA 週查證 · 自動產生 · {DATE_STR}</div>
        </div>
      </div>
    </body></html>'''

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = ','.join(recipients)
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(user, pwd)
            s.send_message(msg)
        print(f"  → Email 已發送至 {', '.join(recipients)}")
    except Exception as e:
        print(f"  → Email 失敗：{e}")


def main():
    print(f"\n{'='*50}")
    print(f"NVIDIA 專區週查證 — {NOW.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    status = load_json(NV_STATUS_PATH, None)
    if status is None:
        print("  ⚠ 找不到 data/nv_status.json，中止")
        return

    print("🗓 近期事件：從daily pipeline近7天資料篩選NVIDIA相關新聞...")
    update_recent_events(status)
    save_json(NV_STATUS_PATH, status)

    # 09-14發現的regression：update_recent_events()內部的call_groq_events()
    # 若有候選就會呼叫一次Groq，緊接著下面call_groq_diff()又立刻呼叫一次，
    # 兩次呼叫的token加總在同一分鐘內超出Groq免費額度6000TPM，導致
    # call_groq_diff()連縮減3輪都還是失敗——問題出在累積用量，不是它自己的
    # prompt太大。比照daily pipeline三分類呼叫間隔65秒的作法，固定睡滿一輪
    # TPM窗口再進行下一次Groq呼叫。
    time.sleep(65)

    print("📰 蒐集 NVIDIA 相關新聞（過去一週）...")
    news = fetch_nvidia_news()
    print(f"  → 共 {len(news)} 則片段")

    # DDG從GitHub Actions的機房IP常被積極限流（daily-update.yml的fetch_ddg()也有同樣情況），
    # 若這週完全沒抓到新聞片段，直接跳過Groq呼叫——沒有任何新聞佐證卻硬要模型「核對是否過時」，
    # 等於在誘導它憑空生出候選異動，只會產生每週固定跳出的假警報banner，使用者久了就會忽略它
    if not news:
        print("  → 本週未蒐集到任何新聞片段，跳過Groq呼叫（避免無佐證卻要求提出異動）")
        status['last_checked'] = DATE_STR
        save_json(NV_STATUS_PATH, status)
        send_email([], '本週未蒐集到相關新聞片段，僅更新查證時間戳，未進行內容查證。', status)
        print("✅ 完成（本週無新聞片段，僅更新查證時間戳）\n")
        return

    print(f"🤖 Groq 依{len(CATEGORY_CONFIG)}大類逐一核對是否過時...")
    # 2026-09-22改造：原本1次Groq請求打包6大類（current_status整份序列化後約
    # 9,302字元），連縮到0則新聞片段仍超出Groq免費tier 6,000 TPM上限，證實問題
    # 在current_status本身太大。現在改成6大類各自獨立呼叫call_groq_diff_one，
    # 比照Renewable Tracker 09-01同款修法：單一大類失敗（try/except包住）不會讓
    # 其他大類連坐失敗；只有6大類「全部」失敗才視同過去的全域例外處理（不更新
    # last_checked、寄技術失敗信），否則即使有部分大類失敗，仍照常寫入其餘大類
    # 的查證結果並更新last_checked，並在信件裡如實列出哪些大類失敗。
    all_items = []
    no_change_parts = []
    category_failures = []
    for idx, cfg in enumerate(CATEGORY_CONFIG):
        print(f"  → 查證大類「{cfg['label']}」...")
        try:
            # 10-27：第 7 類 ecosystem 的資料來自 nv_ecosystem.json（唯讀，只送精簡欄位）；
            # 讀取失敗會拋例外，跟 Groq 失敗一樣記進 category_failures，不影響其他大類
            if cfg.get('source') == 'ecosystem':
                cat_data = build_eco_category_data(load_json(NV_ECO_PATH, None))
            elif cfg['category_enum'] == 'alliances':
                cat_data = build_alliance_category_data(status)
            else:
                cat_data = {k: status.get(k) for k in cfg['keys']}
            diff = call_groq_diff_one(cfg['label'], cat_data, cfg['category_enum'], news_for_category(news, cfg['category_enum']))
            if not isinstance(diff, dict):
                raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
            cat_items = diff.get('items') or []
            # 10-27：本次請求只查單一大類，category 一律以本類為準（防模型填錯類別，前端徽章依此分類計數）
            cat_items = [dict(it, category=cfg['category_enum']) for it in cat_items if isinstance(it, dict)]
            all_items.extend(cat_items)
            if not cat_items and diff.get('no_change_summary'):
                no_change_parts.append(diff['no_change_summary'])
            print(f"    → {'偵測到'+str(len(cat_items))+'項候選異動' if cat_items else '無需更新'}")
        except Exception as e:
            print(f"    ⚠ 大類「{cfg['label']}」查證失敗，跳過此大類，其他大類繼續：{e}")
            category_failures.append(f"{cfg['label']}：{e}")
        # 大類間隔sleep：讓Groq TPM滾動視窗重置，非最後一類才需要等待
        if idx < len(CATEGORY_CONFIG) - 1:
            time.sleep(65)

    if len(category_failures) == len(CATEGORY_CONFIG):
        # 全部大類（10-27 起為 7 類）查證失敗：視同過去的全域例外處理，不更新last_checked，讓
        # nv_pending_review.json/nv_status.json誠實反映「這週其實沒查證成功」。
        fail_summary = '；'.join(category_failures)
        print(f"  ⚠ {len(CATEGORY_CONFIG)}大類全數查證失敗，本次不更新任何內容：{fail_summary}")
        send_email([], f'本週自動查證因技術問題失敗（{fail_summary}），現有資料未變動，將於下次排程自動重試。', status,
                   category_failures=category_failures)
        return

    # email只顯示一句固定摘要，不逐類拼接LLM各自產生的「沒變」句子——多句拼接
    # 對讀者是純噪音，沒有額外資訊。各大類的原始no_change_summary仍完整存進
    # pending.json的no_change_detail_by_category，供人工複核時參考。
    no_change_summary = '本週查證後判斷現有資料仍準確。' if not all_items else ''

    pending = {
        'checked_at': DATE_STR,
        'items': all_items,
        'no_change_summary': no_change_summary,
        'no_change_detail_by_category': no_change_parts,
        'category_failures': category_failures,
    }
    save_json(NV_PENDING_PATH, pending)

    # last_checked 時間戳寫回 nv_status.json 本身：只要至少1大類成功就更新，
    # 不要求全部6大類都成功才更新——否則只要有1類持續失敗，就會讓其他5類明明
    # 查證成功也被卡住永遠不更新時間戳。
    status['last_checked'] = DATE_STR
    save_json(NV_STATUS_PATH, status)

    if all_items:
        print(f"  → 共偵測到 {len(all_items)} 項候選異動，已寫入 data/nv_pending_review.json（未套用，待人工複核）")
    else:
        print(f"  → 本週查證後無需更新：{no_change_summary}")
    send_email(all_items, no_change_summary, status, category_failures=category_failures)
    # git commit/push 交給 GitHub Actions 的 git-auto-commit-action 處理（比照
    # daily-update.yml 慣例），本腳本只負責寫檔案，不自己動 git
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
