#!/usr/bin/env python3
"""
AI Tracker 每日自動更新腳本 v3
新聞來源：RSS feeds（主）+ DuckDuckGo（副）
"""

import json, os, sys, smtplib, urllib.request, urllib.error, xml.etree.ElementTree as ET, time, re, copy
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from groq import Groq

FORCE_REGEN   = '--force' in sys.argv
PIGEON_ONLY   = '--pigeon-only' in sys.argv

# 跨分類生成端避重（2026-10-13）：corp 的 prompt 追加 hw 今日已產出條目清單，
# 避免同一事件同日出現在兩個分類。回復：改成 False，或環境變數 CROSS_BUCKET_AVOID=0。
# 關閉時 prompt 與改動前逐字相同。詳見 ../ROLLBACK_20261013.md
CROSS_BUCKET_AVOID = True
if os.environ.get('CROSS_BUCKET_AVOID', '').strip() == '0':
    CROSS_BUCKET_AVOID = False
CROSS_BUCKET_MAX_ITEMS = 15
CROSS_BUCKET_TITLE_CHARS = 80

# 傳聞寫成事實／補寫原文沒有的數字（2026-10-13 方案 A＋B）。三個開關全關時，
# 擷取結果與 prompt 和 tag quality-baseline-20261013b 逐字相同。詳見 ../ROLLBACK_20261013.md
# A1 擷取端：全文抓取時過濾樣板段落（同頁重複段、高連結密度、非句子、訂閱/cookie/活動廣告），
#    不足再補 <article> 內段落與 og:description。回復：False 或環境變數 CLEAN_BOILERPLATE=0。
CLEAN_BOILERPLATE = True
if os.environ.get('CLEAN_BOILERPLATE', '').strip() == '0':
    CLEAN_BOILERPLATE = False
# A2 生成端：prompt 加「數字/日期/規格只能寫原文有的；預告/傳聞須用預計/據報導語氣」，
#    並放寬「每句必含數字」與 body 最低字數。回復：False 或環境變數 STRICT_SOURCE_PROMPT=0。
STRICT_SOURCE_PROMPT = True
if os.environ.get('STRICT_SOURCE_PROMPT', '').strip() == '0':
    STRICT_SOURCE_PROMPT = False
# C 補深：LLM 選中、但只用 RSS 短摘要寫成的卡片，補抓原文全文後只重寫那張卡片；
#    補抓或重寫失敗則標 thin_source=true（前端與 Email 顯示「僅摘要」）。
#    回復：False 或環境變數 DEEPEN_THIN_CARDS=0（關閉時行為與改動前相同）。詳見 ../ROLLBACK_20261102.md
DEEPEN_THIN_CARDS = True
if os.environ.get('DEEPEN_THIN_CARDS', '').strip() == '0':
    DEEPEN_THIN_CARDS = False
# B 生成後比對：卡片數字/日期/型號是否出現在所掛 source 的素材原文。
#    off＝不跑；log＝只寫 run_stats.fact_audit，不改卡片與評級；
#    enforce＝刪除含疑似補寫的句子，刪完不達標 core→opp，全部句子都疑似則 rating 改 noise（預設）。環境變數 FACT_CHECK_MODE 覆寫。
#    11-02 依 fact_audit_review_20261102.md 啟用 enforce（已修單位夾字、區間共用單位兩項誤報，
#    標題檢查只記錄不刪改）。回復：此處改 'log'，或環境變數 FACT_CHECK_MODE=log；
#    基準 tag factcheck-baseline-20261102。enforce 只對 coverage=full 的卡刪句／降評，
#    low_coverage（僅摘要素材）維持只記錄，計入 low_coverage.skipped_enforce。
FACT_CHECK_MODE = 'enforce'
if os.environ.get('FACT_CHECK_MODE', '').strip().lower() in ('off', 'log', 'enforce'):
    FACT_CHECK_MODE = os.environ['FACT_CHECK_MODE'].strip().lower()

# ── 內容正確性（11-16）：稽核 11-09～11-15 共 36 則有 12 則與原文不符，成因分兩類：
# (a) 輸入假內文（Google News 轉址頁介紹句、付費牆廣告字被當成全文）；(b) 有原文仍寫錯
# （時間範圍、指標種類、限定語、發言歸屬、人名翻譯、數字矛盾）。對應四層防線：
# 1. Google News 解碼（只對即將抓全文的候選）＋內文與標題相關性檢查
# 2. 抓不到全文的候選在素材行標 [HEADLINE_ONLY]；該類卡片最高 opp、寫入 headline_only=true
# 3. 程式數字閘門 numeric_gate（title/body 數字一致、須出自原文）
# 4. LLM 查核關卡 llm_fact_check（ok／fix／drop，以編號對應），fix 後再跑一次數字閘門
# DRY_RUN（11-16）：環境變數 DRY_RUN=1。照常抓新聞、解碼、呼叫 Groq、跑所有閘門與查核，
# 但不寄信、不推 Notion、不寫 data/ 任何檔（history/run_stats/notes_backup）；
# 改把 data 物件、RUN_STATS、每則條目的原文素材寫到 dryrun/result_YYYYMMDD_HHMM.json。
DRY_RUN = os.environ.get('DRY_RUN', '').strip() == '1'
DRY_RUN_DIR = os.environ.get('DRY_RUN_DIR', '').strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'dryrun')
GNEWS_DECODE = True
if os.environ.get('GNEWS_DECODE', '').strip() == '0':
    GNEWS_DECODE = False
GNEWS_DECODE_MAX = 15        # 單次執行解碼上限（Google 對 batchexecute 連打約 40 次就回 429）
GNEWS_DECODE_GAP = 5         # 每則解碼之間至少間隔秒數
GNEWS_TIMEOUT = 15
# 內文有效性：標題關鍵詞（英文去停用詞、長度≥4；中文取 bigram）至少命中幾個才算真內文
TEXT_TITLE_MIN_HITS = 2
# 摘要候選的 RSS 描述扣掉標題後，至少要有幾個新詞才算有內容（Google News 描述＝標題＋媒體名）
HEADLINE_DESC_MIN_NEW_WORDS = 5
HEADLINE_ONLY_TAG = '[HEADLINE_ONLY]'
# LLM 查核關卡：環境變數 LLM_FACT_CHECK=0 關閉；查核失敗一律保留原卡，不影響當日輸出
LLM_FACT_CHECK = True
if os.environ.get('LLM_FACT_CHECK', '').strip() == '0':
    LLM_FACT_CHECK = False
# Groq 免費檔 gpt-oss-120b/20b：TPM 8K（11-16 查官方 rate-limits 頁），prompt＋max_tokens 預留須 ≤ 約 7K
FACT_CHECK_TOKEN_BUDGET = 7000
FACT_CHECK_MAX_TOKENS = 4000      # 與 call_groq 分類主呼叫相同，留足 reasoning 餘裕
FACT_CHECK_MATERIAL_CHARS = 1500  # 每則送查核的原文素材上限字數
FACT_CHECK_REASONING = 'low'      # 與其餘生成後呼叫一致，降低推理吃光 max_tokens 的機率
# 人名中文對照表：表外人名一律保留原文英文拼寫（prompt 明令不得自行翻譯）
PERSON_NAME_ZH = {
    'Jensen Huang': '黃仁勳',
    'Lisa Su': '蘇姿丰',
    'Donald Trump': '川普',
    'Morris Chang': '張忠謀',
    'C.C. Wei': '魏哲家',
    'Elon Musk': '馬斯克',
    'Xi Jinping': '習近平',
    'Joe Biden': '拜登',
    'Masayoshi Son': '孫正義',
    'Lip-Bu Tan': '陳立武',
}

TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')
IS_SUNDAY = NOW.weekday() == 6

KNOWN_TERMS = ["HBM","CoWoS","CSP","OSAT","VLA 模型",
               "Reticle Limit","CapEx","Agentic AI","Physical AI","TSV"]

# ══════════════════════════════════════════════════════════════════
#  漏斗可觀測性：各管線邊界計數收集，跑完寫入 data/run_stats.json
#  （鐵律：統計收集本身絕不可讓主管線崩潰——所有寫入點需 try/except，
#   失敗只印警告，資料照樣往下生成）
# ══════════════════════════════════════════════════════════════════
RUN_STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'run_stats.json')

RUN_STATS = {
    'feeds': {},
    'digest_filtered': 0,
    'after_dedup': 0,
    'buckets': {},
    'news_char_budget': {},
    # 11-09：各分類實際送進 LLM 的素材組成（候選數、全文篇數、送出字數、被截掉的候選數），
    # 見 _fit_news_context；用來分辨「來源少」還是「素材被預算截掉」
    'news_context': {},
    'enriched': 0,
    # llm_items＝LLM 原始回傳條數（call_groq 解析後立即記，任何閘門之前；含 LLM 自己給的佔位卡）
    'llm_items': {},
    # 11-09：各分類主呼叫的 Groq usage（prompt/completion tokens），供日後 TPM 試算用實測值
    'llm_usage': {},
    # 11-09：生成後各閘門每刪一張／改一次評級／清掉來源網址就記一筆，見 _run_gate
    'gate_removed': [],
    'llm_failed': {},
    'final': {},
    'placeholders': 0,
}

def save_run_stats(stats):
    """把本次執行的漏斗計數寫入 data/run_stats.json：新到舊排列、同日覆寫、
    最近 14 筆上限。呼叫端必須自行包 try/except（本函式的檔案寫入可能失敗，
    但絕不可因此中斷主管線）。"""
    if DRY_RUN:
        print("  → DRY_RUN：不寫 data/run_stats.json")
        return
    entry = dict(stats)
    entry['date'] = DATE_STR
    entry['generated_at'] = datetime.now(TW).isoformat()
    try:
        with open(RUN_STATS_PATH, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        runs = existing.get('runs', [])
    except Exception:
        runs = []
    runs = [r for r in runs if r.get('date') != entry['date']]
    runs.insert(0, entry)
    runs = runs[:14]
    with open(RUN_STATS_PATH, 'w', encoding='utf-8') as f:
        json.dump({'runs': runs}, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════════════════
#  1. RSS FEEDS（主要新聞來源，穩定免費）
# ══════════════════════════════════════════════════════════════════
RSS_FEEDS = [
    # ── 硬體供應鏈 & 半導體（供應鏈投資訊號優先）───────────────────────
    ("Semiconductor", "https://semiengineering.com/feed/"),
    ("Semiconductor", "https://www.eetimes.com/feed/"),
    ("Semiconductor", "https://www.digitimes.com/rss/"),          # 台灣供應鏈第一手
    ("Semiconductor", "https://www.theregister.com/headlines.atom"),
    ("Semiconductor", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),  # Micron/NVIDIA 財報
    # ── 產業採用 / 巨頭投資 ──────────────────────────────────────────
    ("CSP/CapEx",    "https://www.datacenterdynamics.com/en/rss/"),
    ("CSP/CapEx",    "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("CSP/CapEx",    "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    # ── AI 應用落地（跨行業）──────────────────────────────────────────
    ("App/AI",       "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("App/AI",       "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("App/AI",       "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("App/AI",       "https://venturebeat.com/category/ai/feed/"),  # 企業 AI 採用與垂直行業落地
    # ── Google News RSS 主題查詢（覆蓋面大，補齊 app 類長期擠不進的缺口）───
    ("gnews-chip", "https://news.google.com/rss/search?q=AI+chip+OR+semiconductor+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("gnews-dc",   "https://news.google.com/rss/search?q=%22data+center%22+AI+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("gnews-app",  "https://news.google.com/rss/search?q=AI+adoption+enterprise+OR+healthcare+OR+fintech+when:1d&hl=en-US&gl=US&ceid=US:en"),
    # ── 垂直媒體補強（實測有效者）───────────────────────────────────
    ("Semiconductor", "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss"),  # IEEE Spectrum AI
    ("App/AI",        "https://www.technologyreview.com/feed/"),        # MIT Technology Review
    ("CSP/CapEx",      "https://www.datacenterknowledge.com/rss.xml"),   # Data Center Knowledge
    ("Semiconductor", "https://www.tomshardware.com/feeds/all"),        # Tom's Hardware（301→200，urllib 預設會跟隨轉址）
    # 註：HPCwire（https://www.hpcwire.com/feed/）實測回傳 403 Cloudflare 人機驗證頁，非有效 RSS，捨棄不加入
]

# 硬體供應鏈關鍵字（hw 分類必須命中其中之一）
HW_KEYWORDS = [
    "hbm","cowos","tsmc","nvidia","amd","intel","sk hynix","micron","samsung foundry",
    "semiconductor","packaging","wafer","gpu","chip","osat","asic","foundry",
    "advanced packaging","chiplet","tsv","emib","soi","reticle","capacity","fab",
    "export control","export ban","restriction","sanction","supply chain","earnings","revenue",
    "computex","ces","gtc"  # 重大展會：展覽期間新聞直通 hw section
]

# CSP / 巨頭關鍵字
CORP_KEYWORDS = [
    "microsoft","google","meta","amazon","apple","capex","data center","cloud",
    "investment","revenue","earnings","openai","anthropic","infrastructure"
]

# 通用 AI 關鍵字（寬篩選，用於 fetch_rss 初步過濾）
AI_KEYWORDS = [
    "nvidia","hbm","cowos","tsmc","ai chip","gpu","semiconductor","memory",
    "microsoft","google","meta","amazon","capex","data center","cloud",
    "agentic","robot","humanoid","physical ai","inference","llm","foundation model",
    "artificial intelligence","machine learning","openai","anthropic","groq",
    "packaging","wafer","foundry","chiplet","osat",
    "export control","export ban","restriction","sanction","supply chain","earnings","revenue",
    "computex","ces","gtc",  # 展會關鍵字
    # 應用行業關鍵字：企業採用與垂直行業落地
    "healthcare","hospital","clinical","drug discovery","pharma","diagnosis","medical ai",
    "autonomous","self-driving","robotaxi","adas","automotive ai",
    "fintech","fraud","trading","financial services","insurance ai",
    "manufacturing","industrial","factory","automation","enterprise",
    "deployment","commercial launch","production","real-world"
]

# 高信號關鍵字：公司/產品名或財務詞，出現代表有實質新聞素材
HIGH_SIGNAL_PAT = re.compile(
    r'nvidia|tsmc|hbm|cowos|micron|sk hynix|samsung foundry|amd|\bintel\b|\barm\b|'
    r'earnings|revenue|\$\d+\s*[bm]\b|capex|data.?center investment|'
    r'openai|anthropic|gemini|gpt-\d|claude|llama|mistral|'
    r'robot|humanoid|autonomous vehicle|physical ai|agentic|'
    r'export (?:ban|control|restriction)|supply chain disruption|'
    r'chip ban|wafer capacity|foundry capacity|'
    # 應用領域關鍵字：企業導入/垂直行業落地，避免應用類新聞永遠低信號
    r'healthcare|clinical|hospital|diagnos|medical ai|drug discovery|pharma|醫療|臨床|'
    r'fintech|fraud detection|financial services|insurance ai|金融科技|詐欺偵測|'
    r'manufacturing|industrial automation|factory automation|製造|工廠自動化|'
    r'enterprise deployment|enterprise adoption|企業導入|企業部署|'
    r'retail|e-commerce|零售|'
    r'logistics|supply chain automation|物流|'
    r'legal tech|legaltech|法律科技|'
    r'edtech|education technology|教育科技',
    re.IGNORECASE
)

def count_high_signal(news_text: str) -> int:
    """計算 news_text 中包含高信號關鍵字的行數（代表有實質新聞素材）"""
    return sum(1 for line in news_text.splitlines()
               if line.strip() and HIGH_SIGNAL_PAT.search(line))

# ══════════════════════════════════════════════════════════════════
#  分類路由：把 enrich 後的新聞素材分流到 hw/corp/app 三個桶，
#  取代舊版「單一素材池、先到先贏」的設計——一篇文章可同時命中多桶（不互斥），
#  三桶都沒命中的進「其餘」桶，附加到當下素材最少的那個桶，避免浪費。
# ══════════════════════════════════════════════════════════════════
CATEGORY_ROUTE_PATS = {
    'hw': re.compile(
        r'nvidia|tsmc|台積|hbm|cowos|micron|sk.?hynix|samsung foundry|amd|\bintel\b|\barm\b|'
        r'半導體|晶片|封裝|晶圓|foundry|\bwafer\b|\bfab\b|osat|asic|chiplet|\btsv\b|emib|reticle|'
        r'export (?:ban|control|restriction)|chip ban|供應鏈|supply chain',
        re.IGNORECASE
    ),
    'corp': re.compile(
        r'data.?center|資料中心|\bcsp\b|capex|\bcloud\b|hyperscaler|\bmw\b|\bgw\b|'
        r'收購|acquisition|merger|融資|funding|投資|microsoft|google|meta|amazon|apple|'
        r'openai|anthropic|infrastructure investment',
        re.IGNORECASE
    ),
    'app': re.compile(
        r'healthcare|clinical|hospital|diagnos|medical ai|drug discovery|pharma|醫療|臨床|'
        r'fintech|fraud|financial services|insurance ai|金融科技|詐欺|'
        r'manufacturing|industrial automation|factory automation|製造|工廠自動化|'
        r'enterprise deployment|enterprise adoption|企業導入|企業部署|'
        r'retail|e-commerce|零售|logistics|物流|legal.?tech|法律科技|'
        r'edtech|education technology|教育科技',
        re.IGNORECASE
    ),
}

# corp 桶選材 80/20 原則用：全球市占領先巨頭清單（中英文皆收，命中即視為「巨頭」）。
# 用於 route_snippets 內把 corp 桶排序、讓 enrich_with_full_text 的 round-robin
# 優先分到巨頭文章的全文抓取名額，避免區域性小型雜訊擠佔巨頭動態的版面。
MAJOR_PLAYERS_PAT = re.compile(
    r'\baws\b|amazon|microsoft|\bazure\b|\bgoogle\b|alphabet|\bmeta\b|\bapple\b|'
    r'\bnvidia\b|openai|anthropic|\boracle\b|\bxai\b|\btesla\b|'
    r'alibaba|阿里|tencent|騰訊|bytedance|字節|samsung|三星|'
    r'tsmc|台積|broadcom|\bamd\b|\bintel\b|英特爾|softbank|軟銀|coreweave',
    re.IGNORECASE
)

# ── hw / app 桶內排序（讓全文挑選與 6000 字裝箱都先吃到最對題的來源）──
# 來源鍵：gnews-* 用 label；其餘用 SOURCE_URL 的網域（RSS label 如 "Semiconductor" 多來源共用，
# 無法區分）。優先級 0=高、1=中（未列者預設）、2=低。分級理由與調整方式：
#  - hw：gnews-chip／Tom's Hardware／DCD／digitimes／eetimes 直接報晶片與基礎設施，最對題；
#    semiengineering 常為研討會場次、CNBC 常被單一事件洗版，降為低優先（仍保留，排在後面）。
#  - app：gnews-app 與垂直／科技媒體為高優先；CNBC、semiengineering 與 app 關聯弱，降為低。
#  - DDG 的 label（產業投資／出口管制／企業採用／應用落地）不在表內者視為中優先。
HW_SOURCE_PRIORITY = {
    'gnews-chip': 0, 'tomshardware.com': 0, 'datacenterdynamics.com': 0,
    'digitimes.com': 0, 'eetimes.com': 0,
    'semiengineering.com': 2, 'cnbc.com': 2,
}
APP_SOURCE_PRIORITY = {
    'gnews-app': 0, 'technologyreview.com': 0, 'venturebeat.com': 0,
    'theverge.com': 0, 'arstechnica.com': 0, 'techcrunch.com': 0,
    '企業採用': 0, '應用落地': 0,
    'semiengineering.com': 2, 'cnbc.com': 2,
}
BUCKET_SOURCE_PRIORITY = {'hw': HW_SOURCE_PRIORITY, 'app': APP_SOURCE_PRIORITY}
# 同話題判定：標題關鍵詞共享 >= 2 個，或 Jaccard >= 0.3，視為同話題；同話題最多 2 條排前面
TOPIC_SHARED_KW_MIN = 2
TOPIC_JACCARD_MIN = 0.3
TOPIC_MAX_PER_BUCKET = 2
_TOPIC_STOPWORDS = frozenset((
    'this that with from have will says said about after over into more than what when '
    'their they them been were also just your like amid could would should while where '
    'which these those here there some most much many other only news report reports '
    'chip chips data center centers new year week today first back make makes made '
).split())

def _snippet_source_key(s):
    m = re.match(r'\[([^\]]+)\]', s)
    label = m.group(1) if m else ''
    # gnews 與 DDG 主題標籤（企業採用／應用落地）以 label 為準，不看網域
    if label.startswith('gnews') or label in ('企業採用', '應用落地'):
        return label
    um = re.search(r'SOURCE_URL:https?://([^/\s]+)', s)
    if um:
        host = um.group(1).lower()
        return host[4:] if host.startswith('www.') else host
    return label

def _snippet_topic_kw(s):
    body = re.sub(r'^\[[^\]]+\]\s*', '', s).split(' — ', 1)[0].split(' | SOURCE_URL:', 1)[0]
    kws = set()
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", body):
        lw = w.lower().strip("'-")
        if lw in _TOPIC_STOPWORDS:
            continue
        if len(lw) >= 4 or (len(lw) >= 3 and w.isupper()):
            kws.add(lw)
    return kws

def _same_topic(a, b):
    if not a or not b:
        return False
    inter = len(a & b)
    return inter >= TOPIC_SHARED_KW_MIN or inter / len(a | b) >= TOPIC_JACCARD_MIN

def rank_bucket(items, priorities):
    """hw/app 桶排序（穩定、決定性、不用隨機）：
    1) 依來源優先級分層；2) 同層內各來源輪流取（round-robin，來源依首次出現順序）；
    3) 同話題最多保留 TOPIC_MAX_PER_BUCKET 條在前，其餘移到最後（不刪除）。"""
    tiers = {}
    for s in items:
        key = _snippet_source_key(s)
        tiers.setdefault(priorities.get(key, 1), {}).setdefault(key, []).append(s)
    ordered = []
    for tier in sorted(tiers):
        queues = list(tiers[tier].values())
        idx = 0
        while any(idx < len(q) for q in queues):
            for q in queues:
                if idx < len(q):
                    ordered.append(q[idx])
            idx += 1
    clusters, head, tail = [], [], []
    for s in ordered:
        kw = _snippet_topic_kw(s)
        hit = None
        for c in clusters:
            if any(_same_topic(kw, m) for m in c['kws']):
                hit = c
                break
        if hit is None:
            clusters.append({'kws': [kw], 'n': 1})
            head.append(s)
        else:
            hit['kws'].append(kw)
            if hit['n'] < TOPIC_MAX_PER_BUCKET:
                hit['n'] += 1
                head.append(s)
            else:
                tail.append(s)
    return head + tail

def route_snippets(snippets):
    """把新聞摘要分流到 hw/corp/app 三桶；可同時命中多桶（不互斥）；
    三桶都沒命中者：原邏輯會附加到當下素材最少的桶，現改為只在該桶是 corp 時保留
    （corp 行為與舊版完全相同），若會落入 hw/app 則丟棄（避免雜訊稀釋對題新聞）。
    corp 桶另依 80/20 選材原則排序：命中 MAJOR_PLAYERS_PAT（全球市占領先巨頭）
    的文章排到最前面，讓後續 enrich_with_full_text 的全文抓取名額與
    enrich 揀選都優先落在巨頭動態上，抑制區域性小型雜訊。
    hw/app 桶另以 rank_bucket 排序（來源分級、輪流取、同話題上限）。"""
    buckets = {'hw': [], 'corp': [], 'app': []}
    unmatched = []
    for s in snippets:
        hit_any = False
        for cat, pat in CATEGORY_ROUTE_PATS.items():
            if pat.search(s):
                buckets[cat].append(s)
                hit_any = True
        if not hit_any:
            unmatched.append(s)
    sizes = {k: len(v) for k, v in buckets.items()}
    dropped_unmatched = 0
    for s in unmatched:
        target = min(sizes, key=lambda k: sizes[k])
        sizes[target] += 1
        if target == 'corp':
            buckets['corp'].append(s)
        else:
            dropped_unmatched += 1
    buckets['corp'].sort(key=lambda s: 0 if MAJOR_PLAYERS_PAT.search(s) else 1)
    for cat in ('hw', 'app'):
        buckets[cat] = rank_bucket(buckets[cat], BUCKET_SOURCE_PRIORITY[cat])
    print(f"  → 分類分流：hw {len(buckets['hw'])} / corp {len(buckets['corp'])} / app {len(buckets['app'])} 條"
          + (f"（其中 {len(unmatched)} 條無命中，{dropped_unmatched} 條丟棄）" if unmatched else ""))
    try:
        RUN_STATS['buckets'] = {c: len(buckets[c]) for c in ('hw', 'corp', 'app')}
        RUN_STATS['unmatched_dropped'] = dropped_unmatched
    except Exception as e:
        print(f"  ⚠ 漏斗統計收集失敗（route_snippets，不影響資料生成）：{e}")
    return buckets

def make_empty_day() -> dict:
    """素材不足時回傳空日結構，不呼叫 LLM"""
    return {
        'date': DATE_STR,
        'is_sunday': IS_SUNDAY,
        'hw': [],
        'corp': [],
        'app': [],
        'glossary_new': [],
        'weekly_summary': None,
        '_skip_reason': 'insufficient_source_material',
    }

def parse_rss_date(item, ns):
    """嘗試解析 RSS/Atom 條目的發布時間，失敗回傳 None"""
    raw = (item.findtext('pubDate') or item.findtext('published') or
           item.findtext('atom:published', namespaces=ns) or
           item.findtext('updated') or item.findtext('atom:updated', namespaces=ns) or '')
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw.strip())
    except Exception:
        pass
    # 嘗試 ISO 8601
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S%z'):
        try:
            return datetime.strptime(raw.strip()[:25], fmt)
        except Exception:
            pass
    return None

# 彙整型文章標題 pattern：論文彙整/週報/每日摘要，本身是 meta-post 而非單一新聞事件，跳過
DIGEST_TITLE_PATS = re.compile(
    r'(?:literature digest|research digest|technical digest|chip industry.*digest|'
    r'\bweek\s+in\s+review\b|weekly\s+(?:digest|roundup|update|wrap)|'
    r'daily\s+(?:digest|roundup|briefing)|'
    r'\broundup\b|'
    r'論文匯總|技術論文|研究簡報|每週.*摘要|週報|每日簡報)',
    re.IGNORECASE
)

def fetch_rss():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    snippets = []
    feed_counts = {}
    digest_filtered = 0
    # 11-16：1116b 試跑 gnews 三個 feed 全部消失卻看不出原因 → 每個 feed 失敗原因、0 則、抓取時間都記錄
    feed_errors = []
    gnews_times = []
    for label, url in RSS_FEEDS:
        host = url.split('/')[2]
        if label.startswith('gnews'):
            gnews_times.append({'label': label, 'at': datetime.now(TW).isoformat(timespec='seconds')})
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    raw = r.read()
            except urllib.error.HTTPError as he:
                feed_errors.append({'label': label, 'host': host, 'error': f"HTTP {he.code}",
                                    'detail': str(he.reason)[:120]})
                print(f"  RSS failed ({host}): HTTP {he.code}")
                continue
            root = ET.fromstring(raw)
            ns = {'atom':'http://www.w3.org/2005/Atom'}
            # Handle both RSS and Atom
            items = root.findall('.//item') or root.findall('.//atom:entry', ns)
            kept = 0
            for item in items[:25]:
                title = (item.findtext('title') or item.findtext('atom:title',namespaces=ns) or '').strip()
                desc  = (item.findtext('description') or item.findtext('summary') or
                         item.findtext('atom:summary',namespaces=ns) or '').strip()
                # 跳過彙整型 meta-post（論文匯總、每週/每日摘要等）
                if DIGEST_TITLE_PATS.search(title):
                    digest_filtered += 1
                    continue
                # 擷取真實 URL（RSS <link> 或 Atom <link href>）
                link = item.findtext('link') or ''
                if not link:
                    link_el = item.find('atom:link', ns)
                    if link_el is not None:
                        link = link_el.get('href', '')
                link = link.strip()
                # 日期過濾：跳過 48h 前的舊文章
                pub = parse_rss_date(item, ns)
                if pub:
                    if pub.tzinfo is None:
                        pub = pub.replace(tzinfo=timezone.utc)
                    if pub < cutoff:
                        continue
                # Strip HTML tags
                desc = re.sub(r'<[^>]+>', '', desc)[:200]
                if title and any(kw in (title+desc).lower() for kw in AI_KEYWORDS):
                    url_part = f" | SOURCE_URL:{link}" if link else ""
                    snippets.append(f"[{label}] {title} — {desc}{url_part}")
                    kept += 1
            feed_counts[label] = feed_counts.get(label, 0) + kept
            if kept == 0:
                feed_errors.append({'label': label, 'host': host, 'error': 'zero_kept',
                                    'detail': f"items={len(items)} bytes={len(raw)}"})
            print(f"  RSS {label}({host}): {len(items)} items, {kept} kept (72h filter)")
        except Exception as e:
            feed_errors.append({'label': label, 'host': host, 'error': type(e).__name__,
                                'detail': str(e)[:120]})
            print(f"  RSS failed ({host}): {e}")
    try:
        RUN_STATS['feeds'] = feed_counts
        RUN_STATS['feed_errors'] = feed_errors
        RUN_STATS['gnews_feed_times'] = gnews_times
        RUN_STATS['digest_filtered'] = digest_filtered
    except Exception as e:
        print(f"  ⚠ 漏斗統計收集失敗（fetch_rss，不影響資料生成）：{e}")
    return snippets

def fetch_ddg():
    snippets = []
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return snippets
    queries = [
        ("產業投資",  "AI chip supply chain CapEx capacity investment strategic 2026"),
        ("出口管制",  "semiconductor export control restriction China supply chain 2026"),
        ("企業採用",  "enterprise AI deployment healthcare finance manufacturing commercial 2026"),
        ("應用落地",  "AI product launch real-world deployment industry adoption 2026"),
    ]
    ddgs = DDGS()
    for label, q in queries:
        for attempt in range(3):
            try:
                results = list(ddgs.news(q, max_results=5, timelimit="w"))
                for r in results:
                    link = r.get('url', '')
                    url_part = f" | SOURCE_URL:{link}" if link else ""
                    snippets.append(f"[{label}] {r.get('title','')} — {r.get('body','')[:150]}{url_part}")
                print(f"  DDG '{label}': {len(results)} results")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                else:
                    print(f"  DDG '{label}' failed after 3 attempts: {e}")
    return snippets

# ── 內文有效性（11-16）：舊判準「>80 字元即成功」會把 Google News 轉址頁介紹句、
# 付費牆會員廣告當成內文。改成結構性判準：抓回的文字必須含該則標題的實質關鍵詞
# （英文去停用詞、長度≥4；中文取 bigram）至少 TEXT_TITLE_MIN_HITS 個，不寫死任何網站或句子。
_RELEVANCE_STOPWORDS = frozenset((
    'with from that this will says said about after over into more than what when their '
    'they them been were also just your like amid could would should while where which '
    'these those here there some most much many other only news report reports year week '
    'today first back make makes made have has having being does doing gets than then '
    'amid against among before below between during under until upon within without '
    'latest update updates live watch video photos exclusive analysis opinion '
).split())
_FETCH_STATE = {'last_reason': ''}

def _strip_media_suffix(title):
    """Google News 標題尾端「 - 媒體名」不算標題內容（媒體名常出現在任何頁面）。"""
    return re.sub(r'\s+[-–—|]\s+[^-–—|]{2,40}$', '', (title or '').strip())

def _title_keywords(title):
    t = _strip_media_suffix(title)
    eng = set()
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", t):
        lw = w.lower().strip("'-")
        if lw.endswith("'s"):
            lw = lw[:-2]
        if len(lw) >= 4 and lw not in _RELEVANCE_STOPWORDS and not lw.isdigit():
            eng.add(lw)
    cjk = set()
    for run in re.findall(r'[一-鿿]{2,}', t):
        cjk.update(run[i:i + 2] for i in range(len(run) - 1))
    return eng, cjk

def text_matches_title(text, title, min_hits=None):
    """抓回的內文是否真的是這則新聞：標題關鍵詞命中數 ≥ min(門檻, 關鍵詞總數)；
    標題完全沒有可用關鍵詞時無法判定，視為不通過（保守）。"""
    if not text:
        return False
    min_hits = TEXT_TITLE_MIN_HITS if min_hits is None else min_hits
    eng, cjk = _title_keywords(title)
    total = len(eng) + len(cjk)
    if total == 0:
        return False
    low = text.lower()
    hits = sum(1 for w in eng if re.search(r'(?<![a-z0-9])' + re.escape(w), low))
    hits += sum(1 for b in cjk if b in text)
    if total == 1 and min_hits >= 2:
        # 11-16 審查：只有 1 個關鍵詞（如 "Nvidia's big week"）時，任何提到該公司的付費牆頁都會通過；
        # 改要求該詞＋標題中至少 1 個數字或短專有名詞（全大寫或首字大寫、2～3 字母）同時命中，否則判失敗
        if hits < 1:
            return False
        t = _strip_media_suffix(title)
        extras = set(re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w])', t))
        extras |= {w.lower() for w in re.findall(r'\b[A-Z][A-Za-z]{1,2}\b', t)
                   if w.lower() not in ('ai', 'the', 'and', 'for', 'new', 'its', 'to', 'in', 'on', 'of', 'at', 'by', 'is', 'as')}
        return any(re.search(r'(?<![a-z0-9])' + re.escape(x) + r'(?![a-z0-9])', low) for x in extras)
    return hits >= min(min_hits, total)

def summary_is_headline_only(title, desc):
    """摘要候選的 RSS 描述是否只是標題（＋媒體名）：扣掉標題已有的詞後，新詞少於
    HEADLINE_DESC_MIN_NEW_WORDS 個（中文以 bigram 計，門檻×2）即視為只有標題。"""
    tw = {w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", title or '')}
    dw = [w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", desc or '')]
    new_eng = {w for w in dw if w not in tw and len(w) >= 3}
    t_cjk = set()
    for run in re.findall(r'[一-鿿]{2,}', title or ''):
        t_cjk.update(run[i:i + 2] for i in range(len(run) - 1))
    new_cjk = set()
    for run in re.findall(r'[一-鿿]{2,}', desc or ''):
        new_cjk.update(b for b in (run[i:i + 2] for i in range(len(run) - 1)) if b not in t_cjk)
    return len(new_eng) < HEADLINE_DESC_MIN_NEW_WORDS and len(new_cjk) < HEADLINE_DESC_MIN_NEW_WORDS * 2

# ── Google News 解碼（11-16）：news.google.com/rss/articles/… 直接抓只會拿到 Google 介紹頁。
# 取文章頁的 data-n-a-sg／data-n-a-ts 簽章，POST batchexecute（rpcid Fbv4je，garturlreq）
# 換回原網址。只對 enrich_with_full_text 即將抓全文的候選解碼；每則間隔 ≥ GNEWS_DECODE_GAP 秒、
# 單次上限 GNEWS_DECODE_MAX 則；遇 HTTP 429 立刻停止本次所有後續解碼（不重試）。失敗不中斷管線。
_GNEWS_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
_GNEWS_ARTICLE_PAT = re.compile(r'^https?://news\.google\.com/(?:rss/)?articles/([^?/#]+)', re.I)

class GNewsRateLimited(Exception):
    pass

def is_gnews_url(url):
    return bool(_GNEWS_ARTICLE_PAT.match(url or ''))

def _gnews_get(url, data=None, headers=None):
    import urllib.error
    h = {'User-Agent': _GNEWS_UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=GNEWS_TIMEOUT) as r:
            return r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise GNewsRateLimited(str(e))
        raise

def _gnews_parse_batchexecute(out):
    """從 batchexecute 回應取原網址：先正規解析（JSON 內嵌 JSON，\\u003d 等跳脫由 json 還原），
    失敗再用正規式退回並手動還原 unicode 跳脫。"""
    for line in (out or '').splitlines():
        line = line.strip()
        if not line.startswith('[['):
            continue
        try:
            arr = json.loads(line)
        except Exception:
            continue
        for row in arr:
            if isinstance(row, list) and len(row) > 2 and row[0] == 'wrb.fr' and isinstance(row[2], str):
                try:
                    inner = json.loads(row[2])
                except Exception:
                    continue
                if isinstance(inner, list) and len(inner) > 1 and inner[0] == 'garturlres' \
                        and isinstance(inner[1], str) and inner[1].startswith('http'):
                    return inner[1]
    m = re.search(r'garturlres\\+",\\+"(.*?)\\+"', out or '')
    if not m:
        return None
    u = m.group(1)
    u = re.sub(r'\\+u([0-9a-fA-F]{4})', lambda x: chr(int(x.group(1), 16)), u)
    u = u.replace('\\/', '/').replace('\\', '')
    return u if u.startswith('http') else None

def decode_gnews_url(url):
    """回傳原網址或 None；遇 429 拋 GNewsRateLimited（呼叫端據此停止後續解碼）。"""
    import urllib.parse
    m = _GNEWS_ARTICLE_PAT.match(url or '')
    if not m:
        return None
    aid = m.group(1)
    sg = ts = None
    for page in (f"https://news.google.com/articles/{aid}", f"https://news.google.com/rss/articles/{aid}"):
        html = _gnews_get(page)
        sgm = re.search(r'data-n-a-sg="([^"]+)"', html)
        tsm = re.search(r'data-n-a-ts="([^"]+)"', html)
        if sgm and tsm:
            sg, ts = sgm.group(1), tsm.group(1)
            break
    if not (sg and ts):
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"' + aid + '",' + ts + ',"' + sg + '"]')
    payload = json.dumps([[["Fbv4je", inner, None, "generic"]]])
    body = urllib.parse.urlencode({'f.req': payload}).encode()
    out = _gnews_get('https://news.google.com/_/DotsSplashUi/data/batchexecute', body,
                     {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'})
    dec = _gnews_parse_batchexecute(out)
    if dec and not is_gnews_url(dec):
        return dec
    return None

def gnews_decode_limited(url, state):
    """加上次數上限、間隔與 429 熔斷的解碼；state 會被更新並寫進 RUN_STATS['gnews_decode']。"""
    if not GNEWS_DECODE or state.get('rate_limited'):
        state['skipped'] = state.get('skipped', 0) + 1
        return None
    if state.get('attempted', 0) >= GNEWS_DECODE_MAX:
        state['skipped_cap'] = state.get('skipped_cap', 0) + 1
        return None
    wait = GNEWS_DECODE_GAP - (time.time() - state.get('_last', 0.0))
    if state.get('_last') and wait > 0:
        time.sleep(wait)
    state['attempted'] = state.get('attempted', 0) + 1
    if 'started_at' not in state:
        state['started_at'] = datetime.now(TW).isoformat(timespec='seconds')
    try:
        dec = decode_gnews_url(url)
    except GNewsRateLimited:
        state['rate_limited'] = True
        dec = None
        print("  ⚠ Google News 解碼遇到 HTTP 429，本次執行停止所有後續解碼")
    except Exception as e:
        dec = None
        print(f"  ⚠ Google News 解碼失敗（不影響管線）：{str(e)[:80]}")
    state['_last'] = time.time()
    if dec:
        state['ok'] = state.get('ok', 0) + 1
    else:
        state['failed'] = state.get('failed', 0) + 1
    return dec

def fetch_article_text(url, max_chars=1100, timeout=6, title=None):
    """抓取文章內文前幾段真實段落，取代單薄的 RSS/DDG 摘要（title+200字短句無法支撐具體事實）。
    title：給了就做內文有效性檢查（text_matches_title），不通過視為抓取失敗回 None。
    _FETCH_STATE['last_reason'] 記錄最近一次失敗原因（http_4xx／invalid_text／error）。"""
    _FETCH_STATE['last_reason'] = ''
    text = _fetch_article_text_raw(url, max_chars, timeout)
    if text and title and not text_matches_title(text, title):
        _FETCH_STATE['last_reason'] = 'invalid_text'
        return None
    if not text and not _FETCH_STATE['last_reason']:
        _FETCH_STATE['last_reason'] = 'invalid_text'
    return text

def _fetch_article_text_raw(url, max_chars=1100, timeout=6):
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            _FETCH_STATE['last_reason'] = f'http_{e.code}'
            return None
        with r:
            raw = r.read(CLEAN_READ_BYTES if CLEAN_BOILERPLATE else 300_000)
        html = raw.decode('utf-8', errors='ignore')
        if CLEAN_BOILERPLATE:
            return extract_clean_text(html, max_chars)
        html = re.sub(r'<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>', ' ', html, flags=re.S|re.I)
        paras = re.findall(r'<p[^>]*>(.*?)</p>', html, flags=re.S|re.I)
        parts, total = [], 0
        for p in paras:
            t = re.sub(r'<[^>]+>', '', p)
            t = re.sub(r'\s+', ' ', t).strip()
            if len(t) > 40:  # 跳過導覽列/版權宣告等短句雜訊
                parts.append(t)
                total += len(t)
            if total >= max_chars:
                break
        text = ' '.join(parts)[:max_chars]
        return text if len(text) > 80 else None
    except Exception:
        _FETCH_STATE['last_reason'] = 'error'
        return None

# ── A1：樣板段落過濾（CLEAN_BOILERPLATE）──
# 2026-10-13 實例：舊擷取把 The Verge 頁首「Posts from this topic will be added…」
# 等追蹤按鈕文字、TechCrunch 活動廣告當成內文塞滿 1100 字，真正內文幾乎沒進素材，
# 模型只好自己補規格。判準一律結構性（同頁重複、連結密度、非句子），只有
# 訂閱/cookie/活動報名這類各站通用的片語用字串比對；不寫死任何特定網站。
CLEAN_READ_BYTES = 1_200_000   # 新式網頁 <head> 內嵌腳本常超過 300KB，舊上限會讀不到內文
CLEAN_MIN_CHARS = 300          # 文章範圍內過濾後少於此字數，改用整頁段落並補 og:description
_P_TAG_PAT = re.compile(r'<p(?:\s[^>]*)?>(.*?)</p>', flags=re.S | re.I)   # 舊版 <p[^>]*> 會誤吃 <path>/<picture>
_A_TAG_PAT = re.compile(r'<a\b[^>]*>(.*?)</a>', flags=re.S | re.I)
_BOILER_PHRASE_PAT = re.compile(
    r'subscribe|newsletter|sign up|sign in|log in|cookie|privacy policy|terms of (?:service|use)|'
    r'all rights reserved|advertisement|email digest|homepage feed|follow (?:topics|authors|us)|'
    r'register now|book (?:your|an?) |get (?:your|a) (?:pass|ticket)|tickets? (?:are|now)|'
    r'early[- ]bird|% off|save \$|limited time|exhibit table|sponsored',
    re.I)
_SENTENCE_END_PAT = re.compile(r'[.!?。！？]["”’)]?(?:\s|$)')

def _html_unescape(t):
    import html as _h
    return _h.unescape(t)

def _para_text(inner):
    t = re.sub(r'<[^>]+>', '', inner)
    t = _html_unescape(t)
    return re.sub(r'\s+', ' ', t).strip()

def _clean_paragraphs(html_fragment):
    """回傳過濾後段落清單。過濾：同頁重複段落（全部移除）、連結文字佔比>50%、
    不像句子（無句尾標點且少於 12 個字詞）、含通用訂閱/cookie/活動廣告片語。"""
    raw = []
    for inner in _P_TAG_PAT.findall(html_fragment):
        t = _para_text(inner)
        if len(t) <= 40:
            continue
        link_txt = ''.join(_para_text(a) for a in _A_TAG_PAT.findall(inner))
        raw.append((t, len(link_txt) / max(len(t), 1)))
    counts = {}
    for t, _ in raw:
        k = t.lower()
        counts[k] = counts.get(k, 0) + 1
    out, seen = [], set()
    for t, link_ratio in raw:
        k = t.lower()
        if counts[k] > 1 or k in seen:
            continue
        seen.add(k)
        if link_ratio > 0.5:
            continue
        if not _SENTENCE_END_PAT.search(t + ' ') and len(t.split()) < 12:
            continue
        if _BOILER_PHRASE_PAT.search(t):
            continue
        out.append(t)
    return out

def _og_description(html):
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*>', html, flags=re.I)
    if not m:
        return ''
    c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), flags=re.I)
    return re.sub(r'\s+', ' ', _html_unescape(c.group(1))).strip() if c else ''

def extract_clean_text(html, max_chars=1100):
    """CLEAN_BOILERPLATE 版擷取：先取 <article> 內過濾後段落，不足 CLEAN_MIN_CHARS
    改用整頁過濾後段落；仍不足則把 og:description 放最前面補足。"""
    og = _og_description(html)
    html = re.sub(r'<(script|style|noscript|nav|footer|header|aside|form|figcaption|button|svg)\b[^>]*>.*?</\1>',
                  ' ', html, flags=re.S | re.I)
    paras = []
    arts = re.findall(r'<article\b[^>]*>(.*?)</article>', html, flags=re.S | re.I)
    if arts:
        paras = _clean_paragraphs(' '.join(arts))
    if sum(len(p) for p in paras) < CLEAN_MIN_CHARS:
        paras = _clean_paragraphs(html)
    if og and sum(len(p) for p in paras) < CLEAN_MIN_CHARS and not any(og[:60] in p for p in paras):
        paras = [og] + paras
    parts, total = [], 0
    for p in paras:
        parts.append(p)
        total += len(p)
        if total >= max_chars:
            break
    text = ' '.join(parts)[:max_chars]
    return text if len(text) > 80 else None

def _entry_title(s):
    """素材條目的原文標題（去 [label]／[HEADLINE_ONLY] 標記、去內文與網址）。"""
    t = re.sub(r'^(?:\[[^\]]+\]\s*)+', '', s)
    return t.split(' — ', 1)[0].split(' | SOURCE_URL:', 1)[0].strip()

def _entry_desc(s):
    """摘要條目（標題 — 描述 | SOURCE_URL:…）的描述部分；全文條目回傳空字串。"""
    if _FULLTEXT_ENTRY_PAT.search(s):
        return ''
    rest = s.split(' — ', 1)[1] if ' — ' in s else ''
    return rest.split(' | SOURCE_URL:', 1)[0].strip()

def mark_headline_only(entry):
    """在 [label] 之後插入 [HEADLINE_ONLY] 標記（已標過不重複）；無 label 時放最前面。"""
    if HEADLINE_ONLY_TAG in entry:
        return entry
    m = re.match(r'\[[^\]]+\]\s*', entry)
    if m:
        return entry[:m.end()] + HEADLINE_ONLY_TAG + ' ' + entry[m.end():]
    return HEADLINE_ONLY_TAG + ' ' + entry

def _replace_source_url(entry, new_url):
    return re.sub(r'SOURCE_URL:\S+', lambda _m: 'SOURCE_URL:' + new_url, entry, count=1)

def _would_send_entries(entries, cat):
    """試算這一桶目前會送進 LLM 的條目（依送出順序）：三分類都走 _fit_news_context 裝箱（11-17 corp 也改 pack）。
    用分類原始預算（make_prompt 之後可能再縮減，屬超集合）。"""
    budget = _category_char_budget(cat)
    out = []
    _fit_news_context('\n\n'.join(entries), cat, budget, sent_out=out)
    return out

def enrich_with_full_text(buckets, max_fetch=12):
    """三分類輪流分配全文抓取名額（round-robin：hw桶第1篇→corp桶第1篇→app桶第1篇→hw桶第2篇…），
    取代舊版「單一素材池、先到先贏」（hw 類新聞源強勢佔滿、app 類永遠擠不進）。
    只對高信號候選（有 SOURCE_URL 且命中 HIGH_SIGNAL_PAT）抓取真實內文取代薄摘要；
    同一 URL 若重複出現於多桶（一篇文章可同時進多桶），只實際抓取一次並快取結果，
    避免重複網路請求。成功抓到內文的條目移到該桶最前面，確保後續預算截斷時
    優先保留高密度素材（沿用舊版行為；hw/app 的全文佔比上限見 _fit_news_context）。
    11-16：Google News 連結先解碼成原網址再抓（gnews_decode_limited），成功即把 SOURCE_URL
    換成原網址；抓回的文字須通過 text_matches_title。全文失敗（解碼失敗、原站 401/403、
    內文無效）的候選，以及 RSS 描述只有標題的摘要候選，素材行標 [HEADLINE_ONLY]。"""
    cats = ['hw', 'corp', 'app']
    lists = {c: list(buckets.get(c, [])) for c in cats}
    eligible = {c: [i for i, s in enumerate(lists[c])
                    if 'SOURCE_URL:' in s and HIGH_SIGNAL_PAT.search(s)] for c in cats}
    cursor = {c: 0 for c in cats}
    enriched_idx = {c: set() for c in cats}
    failed_idx = {c: set() for c in cats}
    cache = {}
    url_map = {}
    decode_state = {'attempted': 0, 'ok': 0, 'failed': 0, 'rate_limited': False,
                    'skipped': 0, 'skipped_cap': 0}
    fail_reasons = {}
    fetched = 0
    attempted = {c: set() for c in cats}

    def _process(c, i):
        """對 lists[c][i] 解碼（Google News）＋抓全文；成功換成全文格式。回傳是否抓到全文。"""
        attempted[c].add(i)
        s = lists[c][i]
        m = re.search(r'SOURCE_URL:(\S+)', s)
        url = m.group(1) if m else None
        if url not in cache:
            fetch_url = url
            reason = ''
            if url and is_gnews_url(url):
                dec = gnews_decode_limited(url, decode_state)
                if dec:
                    url_map[url] = dec
                    fetch_url = dec
                else:
                    fetch_url = None
                    reason = 'decode_failed'
            text = None
            if fetch_url:
                text = fetch_article_text(fetch_url, title=_entry_title(s))
                if not text:
                    reason = _FETCH_STATE.get('last_reason') or 'invalid_text'
            cache[url] = text
            if not text:
                fail_reasons[reason] = fail_reasons.get(reason, 0) + 1
        text = cache[url]
        new_url = url_map.get(url, url)
        if text:
            head = s.split(' — ', 1)[0].split(' | SOURCE_URL:', 1)[0]
            # SOURCE_URL 緊接標題之後，不放在長內文最後面——內文拉長到 1100 字後，
            # LLM 常常讀到文末前就已經寫完 JSON，導致 source 欄位留空或抓錯
            lists[c][i] = f"{head} | SOURCE_URL:{new_url} — {text}"
            enriched_idx[c].add(i)
            return True
        failed_idx[c].add(i)
        return False

    # 11-16 C：先處理「實際會送進 LLM」的 Google News 候選（依送出順序、三分類輪流），
    # 解碼上限、間隔、429 熔斷仍由 gnews_decode_limited 控制；成功的全文計入 max_fetch
    sent_gn = {c: [] for c in cats}
    for c in cats:
        try:
            for e in _would_send_entries(lists[c], c):
                i = lists[c].index(e)
                mm = re.search(r'SOURCE_URL:(\S+)', e)
                if mm and is_gnews_url(mm.group(1)) and i not in sent_gn[c]:
                    sent_gn[c].append(i)
        except Exception as e:
            print(f"  ⚠ 送出候選試算失敗（{c}，略過優先解碼）：{e}")
    gn_order = []
    for k in range(max([len(v) for v in sent_gn.values()] + [0])):
        for c in cats:
            if k < len(sent_gn[c]):
                gn_order.append((c, sent_gn[c][k]))
    for c, i in gn_order:
        if fetched >= max_fetch:
            break
        if decode_state.get('rate_limited') or decode_state.get('attempted', 0) >= GNEWS_DECODE_MAX:
            break
        if _process(c, i):
            fetched += 1

    active = True
    while fetched < max_fetch and active:
        active = False
        for c in cats:
            if fetched >= max_fetch:
                break
            elist = eligible[c]
            if cursor[c] >= len(elist):
                continue
            active = True
            i = elist[cursor[c]]
            cursor[c] += 1
            if i in attempted[c]:
                continue
            if _process(c, i):
                fetched += 1
    ho = {'fetch_failed': 0, 'thin_summary': 0, 'fetch_failed_desc_kept': 0}
    for c in cats:
        for i, s in enumerate(lists[c]):
            if i in enriched_idx[c]:
                continue
            m = re.search(r'SOURCE_URL:(\S+)', s)
            if m and m.group(1) in url_map:
                s = _replace_source_url(s, url_map[m.group(1)])
            # 11-16 審查：全文抓取失敗但 RSS 描述本身有足夠新資訊（非 Google News「標題＋媒體名」）時不標，
            # 描述照常當素材；只有描述不足才標 HEADLINE_ONLY
            if summary_is_headline_only(_entry_title(s), _entry_desc(s)):
                s = mark_headline_only(s)
                if i in failed_idx[c]:
                    ho['fetch_failed'] += 1
                else:
                    ho['thin_summary'] += 1
            elif i in failed_idx[c]:
                ho['fetch_failed_desc_kept'] += 1
            lists[c][i] = s
    for c in cats:
        front = [lists[c][i] for i in range(len(lists[c])) if i in enriched_idx[c]]
        rest  = [lists[c][i] for i in range(len(lists[c])) if i not in enriched_idx[c]]
        lists[c] = front + rest
    if fetched:
        print(f"  → 已抓取 {fetched} 篇完整內文取代薄摘要（三分類輪流分配，非先到先贏）")
    rl_note = '（遇 429 已停止）' if decode_state['rate_limited'] else ''
    print(f"  → Google News 解碼：嘗試 {decode_state['attempted']}、成功 {decode_state['ok']}{rl_note}；"
          f"標記 HEADLINE_ONLY：全文失敗 {ho['fetch_failed']} 條、描述只有標題 {ho['thin_summary']} 條")
    try:
        RUN_STATS['enriched'] = fetched
        RUN_STATS['gnews_decode'] = {k: v for k, v in decode_state.items() if not k.startswith('_')}
        RUN_STATS['headline_only'] = dict(ho, fail_reasons=fail_reasons)
    except Exception as e:
        print(f"  ⚠ 漏斗統計收集失敗（enrich_with_full_text，不影響資料生成）：{e}")
    return lists

def _title_tokens(text):
    """混合 tokenizer：CJK bigram + 英文單詞（len>2），解決中文無空格的詞切分問題"""
    cjk = re.sub(r'[^一-鿿]', '', text)
    bigrams = set(cjk[i:i+2] for i in range(len(cjk) - 1))
    eng = set(w for w in re.sub(r'[^\w]', ' ', text).lower().split()
              if len(w) > 2 and not re.search(r'[一-鿿]', w))
    return bigrams | eng

def _norm_url(url):
    """URL 最小正規化：去尾端斜線、去 utm_* query 參數"""
    url = (url or '').strip()
    if '?' in url:
        base, _, query = url.partition('?')
        kept = [p for p in query.split('&') if p and not p.lower().startswith('utm_')]
        url = base + ('?' + '&'.join(kept) if kept else '')
    return url.rstrip('/')

_SOURCE_URL_PAT = re.compile(r'SOURCE_URL:(\S+)')

def filter_recent(snippets, recent_titles, recent_urls=None):
    """過濾與近日已報道標題高度重疊、或來源網址與近日完全相同的新聞摘要
    （code-level 去重，不依賴 LLM 指令；使用者拍板：同一篇文章只分析一次）"""
    if not recent_titles and not recent_urls:
        return snippets
    recent_norm = [_title_tokens(rt) for rt in (recent_titles or [])]
    recent_urls = recent_urls or set()
    filtered, dropped_t, dropped_u = [], 0, 0
    for s in snippets:
        # URL 完全比對（正規化後）：同一篇文章不管標題怎麼改寫都會被丟棄
        m = _SOURCE_URL_PAT.search(s)
        if m and _norm_url(m.group(1)) in recent_urls:
            dropped_u += 1
            continue
        dash = s.find(' — ')
        bracket = s.find(']')
        title_part = s[bracket+1:dash if dash > 0 else bracket+80].strip()
        tokens = _title_tokens(title_part)
        # ≥3 個 bigram/英文詞重疊才視為重複（CJK bigram 比單詞更精準）
        is_dup = any(len(tokens & rn) >= 3 for rn in recent_norm) if tokens else False
        if is_dup:
            dropped_t += 1
        else:
            filtered.append(s)
    if dropped_t or dropped_u:
        print(f"  → 預過濾舊新聞 {dropped_t} 條（標題重疊）+ {dropped_u} 條（來源網址相同）")
    return filtered


def fetch_news(recent_titles=None, recent_urls=None):
    """回傳 {'hw':str,'corp':str,'app':str} 三分類各自的新聞素材文字（一分類一次 LLM 呼叫架構）。
    每篇文章先分流到 hw/corp/app 桶（可同時進多桶），全文抓取名額三分類輪流分配，
    取代舊版「單一素材池、先到先贏、3500 字硬砍」造成 app 類永遠擠不進的結構性瓶頸。"""
    print("  → RSS feeds...")
    rss = fetch_rss()
    print(f"  → RSS 取得 {len(rss)} 條")
    print("  → DuckDuckGo...")
    ddg = fetch_ddg()
    print(f"  → DDG 取得 {len(ddg)} 條")
    all_news = rss + ddg
    # Deduplicate by title（去掉 [label] 前綴再比對，同一文章因 section 不同不視為兩條）
    seen, unique = set(), []
    for s in all_news:
        body = re.sub(r'^\[[^\]]+\]\s*', '', s)
        key = re.sub(r'[^\w\s]', '', body[:80]).lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(s)
    print(f"  → 去重後 {len(unique)} 條")
    # code-level 預過濾：移除與近三日標題高度重疊、或來源網址相同的摘要
    if recent_titles or recent_urls:
        unique = filter_recent(unique, recent_titles, recent_urls)
        print(f"  → 預過濾後剩 {len(unique)} 條")
    try:
        RUN_STATS['after_dedup'] = len(unique)
    except Exception as e:
        print(f"  ⚠ 漏斗統計收集失敗（fetch_news/after_dedup，不影響資料生成）：{e}")
    # 分類分流：一篇文章可同時進多桶，三桶都沒命中的進素材最少的桶
    buckets = route_snippets(unique)
    # 對高信號候選抓取完整內文取代薄摘要，三分類輪流分配名額（不再先到先贏）
    try:
        buckets = enrich_with_full_text(buckets, max_fetch=12)
    except Exception as e:
        # 11-16：全文抓取／GN 解碼任何例外都退回 RSS 摘要，不中斷管線
        print(f"  ⚠ 全文抓取失敗，改用 RSS 摘要（不影響資料生成）：{e}")
    return {cat: "\n\n".join(buckets[cat]) for cat in ['hw', 'corp', 'app']}


def get_recent_titles(history, days=3, max_titles=30):
    """取得近 N 天所有標題（含 noise）用於跨日去重；排除今日自身。
    同一篇文章只分析一次，冷熱以第一次判定為準，noise 也需列入 NO-REPEAT
    避免已判 noise 的新聞隔天原樣重現後被重新判成 core/opp。"""
    titles = []
    count = 0
    for entry in history:
        if entry.get('date') == DATE_STR:
            continue  # 跳過今日，防止自我封鎖
        if count >= days:
            break
        count += 1
        for section in ['hw', 'corp', 'app']:
            for item in entry.get(section, []):
                t = item.get('title', '').strip()
                if t:
                    titles.append(t)
    return titles[:max_titles]

def get_recent_urls(history, days=3):
    """取得近 N 天所有條目的來源網址（含 noise、正規化後）用於 URL 完全比對去重；排除今日自身"""
    urls = set()
    count = 0
    for entry in history:
        if entry.get('date') == DATE_STR:
            continue  # 跳過今日，防止自我封鎖
        if count >= days:
            break
        count += 1
        for section in ['hw', 'corp', 'app']:
            for item in entry.get(section, []):
                u = _norm_url(item.get('source', ''))
                if u:
                    urls.add(u)
    return urls

def get_recent_items(history, days=3, max_items=30):
    """取得近 N 天非佔位卡條目的 title+body（排除今日自身），供語意層跨日
    去重／新鮮度判斷用。get_recent_titles 只給標題，字面改寫就比不出來，
    這裡多帶 body 讓 LLM 判斷『是否為同一真實世界事件』而非比對字面。"""
    items = []
    count = 0
    for entry in history:
        if entry.get('date') == DATE_STR:
            continue  # 跳過今日，防止自我封鎖
        if count >= days:
            break
        count += 1
        for section in ['hw', 'corp', 'app']:
            for item in entry.get(section, []):
                if _is_placeholder(item):
                    continue
                title = item.get('title', '').strip()
                if not title:
                    continue
                items.append({'title': title, 'body': item.get('body', '')[:200]})
    return items[:max_items]

def load_notes():
    """讀取每日筆記：優先用當日 Gist 快照 notes_backup.json，退回舊 notes.json"""
    for path in ('data/notes_backup.json', 'data/notes.json'):
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    return {}

def snapshot_notes_backup():
    """每日快照：從 Gist 抓 notes.json 寫入 data/notes_backup.json（config 缺失則靜默跳過）"""
    if DRY_RUN:
        print("  → DRY_RUN：不寫 data/notes_backup.json")
        return
    cfg_path = 'data/notes_config.json'
    if not os.path.exists(cfg_path):
        return
    try:
        with open(cfg_path, encoding='utf-8') as f:
            gist_id = json.load(f).get('gist_id')
        if not gist_id:
            return
        url = f"https://gist.githubusercontent.com/resolutetinging/{gist_id}/raw/notes.json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read().decode('utf-8')
        json.loads(content)  # 確認 JSON 合法再落地
        os.makedirs('data', exist_ok=True)
        with open('data/notes_backup.json', 'w', encoding='utf-8') as f:
            f.write(content)
        print("  → 筆記快照已更新 data/notes_backup.json")
    except Exception as e:
        print(f"  → 筆記快照失敗（略過）：{e}")

# ══════════════════════════════════════════════════════════════════
#  2. PROMPT
# ══════════════════════════════════════════════════════════════════
CATEGORY_DESC = {
    'hw':   "hw: AI infrastructure supply chain signals — capacity commitments (CoWoS/HBM/OSAT/fab), strategic supplier decisions, export controls that shift production geography; prioritize financial/strategic signals over technical specifications; NOT GPU architecture analysis, chip benchmark comparisons, or speculative roadmap commentary",
    'corp': "corp (巨頭角力 — 80/20 選材原則): roughly 80% of items should be moves by globally dominant incumbents (AWS/Amazon, Microsoft/Azure, Google/Alphabet, Meta, Apple, NVIDIA, OpenAI, Anthropic, Oracle, xAI, Tesla, Alibaba, Tencent, ByteDance, Samsung, TSMC, Broadcom, AMD, Intel, SoftBank, CoreWeave) — CSP CapEx, major enterprise AI contracts, vertical-sector deployments, acquisitions, mega-round financing, model commercialization milestones that show where AI is being adopted at scale; the remaining roughly 20% may cover emerging/challenger companies whose moves could realistically shift market share away from those incumbents; a small regional data-center story with no major player involved, under 100MW, or a project that was rejected/abandoned should be rated noise (or simply not selected) unless it carries clear strategic significance; NOT stock prices",
    'app':  "app: real-world AI application advances — deployed products and services across any industry (healthcare, finance, manufacturing, legal, creative, education, logistics), measurable commercial traction, new business models enabled by AI; prefer concrete launched products over research-stage announcements",
}

# 一分類一次呼叫架構下，weekly_summary 拆成三段，各分類只負責自己那一行，
# main 流程合併三次呼叫結果時用 \n 接回原本的多行格式；
# 「下週看點」放在 corp（CSP/CapEx 視角最貼近總體展望）、
# 「用戶洞察」放在 app（三次呼叫中順序最後，象徵彙整收尾）。
# 11-16：weekly_rule 舊版要求「每點一行」與這裡 hw「一句」互相矛盾，HW 段出現字面「\n」。
# 統一：HW／APP 各一句；CORP 一句＋下週看點一句（用戶洞察行只在有筆記時附加於 app）。
CATEGORY_WEEKLY_TEMPLATE = {
    'hw':   'HW：[半導體本週最重要的一句摘要]',
    'corp': 'CORP：[CSP/CapEx本週一句摘要]\\n下週看點：[下週最值得追蹤的一個指標或事件，一句]',
    'app':  'APP：[新興AI本週一句摘要]',
}
CATEGORY_WEEKLY_RULE = {
    'hw':   'exactly ONE line: "HW：" followed by exactly one sentence ending with 。 — no line breaks, no second sentence',
    'corp': 'exactly TWO lines separated by a single \\n: line 1 "CORP：" + one sentence ending with 。; '
            'line 2 "下週看點：" + one sentence ending with 。 — no other lines',
    'app':  'exactly ONE line: "APP：" followed by exactly one sentence ending with 。 — no line breaks, no second sentence',
}
WEEKLY_RULE_NOTES_SUFFIX = ('; because user notes are provided, add exactly one more line after a single \\n: '
                            '"用戶洞察：" + one sentence ending with 。')

# ── 原文忠實規則（11-16）：稽核發現有原文仍寫錯的 8 則都屬下列型態；分類主呼叫、補深重寫、
# LLM 查核關卡共用同一段文字，避免三處規則漂移。
def person_name_rule_text():
    pairs = ', '.join(f"{en}→{zh}" for en, zh in PERSON_NAME_ZH.items())
    return ("- PERSON NAMES: keep each person's name in the source's English spelling; only this fixed table may be "
            "written in Chinese: " + pairs + "; never translate or transliterate any other name")

# 11-16 精簡版：分類主呼叫的 corp prompt 實測已達 5.8K tokens，規則以最短措辭加入，避免逼近 TPM 8K
SOURCE_FIDELITY_RULES = (
    "- SOURCE FIDELITY (facts): each of these must come from the same source entry, else leave it out — "
    "a person's title and employer; the period (single quarter vs. nine-month/YTD cumulative vs. full year; "
    "actual vs. forecast, and the forecast year); the metric (revenue vs. operating profit vs. adjusted operating "
    "profit vs. net income; projection vs. reported); qualifiers (up to→最多, at least→至少, about→約; never write "
    "超過 for up to); attribution (a headline's or author's words are never a person's quote); scope (global vs. "
    "one country, multi-year plan vs. one deadline)\n"
    # 11-17 C：首日稽核（某模型的 prompt processing 約三天寫成整個 GPU 上線約三天、cannot maintain 寫成將失去）
    "- SOURCE FIDELITY (scope, strength, attribution): never widen scope (a claim about one part, step, model or "
    "product stays that narrow, never the whole system/company/industry); never change modal strength "
    "(cannot maintain→無法維持 not 將失去, may/could→可能 not will, up to→最多 not 超過); when the source gives "
    "something as a person's or company's claim, or as said to a specific audience, keep that attribution in "
    "body (據 X 表示／X 向投資人表示)\n"
)

def system_fidelity_text():
    """system 訊息用的原文忠實鐵律（繁中精簡版），與 SOURCE_FIDELITY_RULES 同一精神。"""
    return (
        "【原文忠實】職稱與所屬機構、時間範圍、指標種類、限定語、發言歸屬、地理與對象範圍都必須出自同一則原文，"
        "原文沒寫就不寫。人名保留英文原文，只有對照表內可寫中文。[HEADLINE_ONLY] 素材只能一句改寫標題，最高 opp。"
    )

HEADLINE_ONLY_RULES = (
    "- [HEADLINE_ONLY] ENTRIES have no article text, only a headline: an item from one gets a ONE-sentence body that "
    "only restates the headline (no titles, background, numbers, dates, model numbers, causes it lacks), "
    "impact/insight add no new number or named fact, and rating is at most opp\n"
)

# 11-17 B：只有 RSS 描述的素材（「標題 — 描述 | SOURCE_URL:…」，網址後沒有內文）；程式另以 desc_only 決定性清理
DESC_ONLY_RULES = (
    "- ENTRIES WITH NO TEXT AFTER SOURCE_URL have only a short RSS description: an item from one gets a body of at "
    "most 2 sentences using only facts in that description (no added scope, region, comparison, trend or purpose), "
    "impact/insight add no new fact, and rating is at most opp\n"
)

BASE_NEWS_CHAR_BUDGET = 3500
MAX_NEWS_CHAR_BUDGET = 8000
REFERENCE_BUCKET_SIZE = 30  # hw/app 常態bucket大小

def _category_char_budget(category):
    """依當日該分類bucket大小動態調整送進LLM的新聞文字上限，取代原本三類
    統一3500字的寫法。2026-09-14查證：corp類候選常態暴增到94-143條（hw/app
    穩定22-37條），但enrich_with_full_text把最多12篇全文（每篇最多1100字）
    優先塞到news_context最前面，corp只要輪到3-4篇全文就已吃光3500字，其餘
    90幾條候選連LLM都看不到就被截斷丟棄——連續多天冷天的根因在此，跟去重
    邏輯無關。上限8000字是為了不超出Groq免費額度6000 TPM/分鐘（見main()
    註解），實際completion很少用滿max_tokens，仍有安全餘裕。"""
    bucket_sizes = RUN_STATS.get('buckets', {})
    size = bucket_sizes.get(category, REFERENCE_BUCKET_SIZE)
    budget = int(BASE_NEWS_CHAR_BUDGET * (size / REFERENCE_BUCKET_SIZE))
    # 11-09：hw/app 預算改固定 6000 字（舊：3500×bucket/30，實際 3500–4400；corp 公式與 8000 上限不變）。
    # 實測 11-09 本機抓到的真實 bucket：全文條目每條約 1300 字（標題＋網址＋1100 字內文），
    # 摘要條目平均約 390–440 字（Google News 網址很長）。舊預算 3500–4400 字時
    # hw 只看得到前 2–3 條（全是全文），其餘 30 幾條候選全被截掉，LLM 只能回 1 則或佔位卡
    # （10-27 起 llm_items hw/app 幾乎天天＝1）。6000 字搭配 _fit_news_context 的全文縮短
    # 與佔比上限，hw 可看到約 10–12 條不同候選。token 試算：hw/app 6000 字新聞約 1400–1900
    # tokens，整個請求仍小於 corp（8000 字）每天都成功的請求量。不隨 bucket 放大到 8000：
    # hw/app bucket 常態 22–45 條，6000 字已能放進 10–15 條，固定值讓 TPM 最壞情況可預估。
    if category in ('hw', 'app'):
        return HW_APP_NEWS_CHAR_BUDGET
    return max(BASE_NEWS_CHAR_BUDGET, min(MAX_NEWS_CHAR_BUDGET, budget))

HW_APP_NEWS_CHAR_BUDGET = 6000
# hw/app 素材分配（11-09）：全文條目在 prompt 內只放前 600 字內文（約 100 個英文詞，
# 通常就是導言段＋第一個具體數字段），且全文條目合計不超過預算的一半，
# 另一半留給摘要候選；否則 3–4 篇全文就吃光預算、LLM 永遠只看到 3 個事件。
# 只影響「送進 prompt 的那份文字」：fact_audit／補深用的 news_by_cat 仍是完整全文。
PROMPT_FULLTEXT_CHARS = 600
PROMPT_FULLTEXT_SHARE = 0.5
# corp（11-17）：舊 slice 模式下 3 則全文（每則約 1300 字）就吃光 4871 字預算，109 則候選只送 6 則、
# LLM 回 noise。改走 pack：全文內文同樣只放前 600 字，佔比上限見下（依 1116 三份試跑素材離線重算）。
CORP_PROMPT_FULLTEXT_CHARS = 600
CORP_PROMPT_FULLTEXT_SHARE = 0.3
_FULLTEXT_ENTRY_PAT = re.compile(r'SOURCE_URL:\S+ — ')  # 與 _fa_index_materials 同一判準

def _trim_fulltext_entry(entry, max_chars=PROMPT_FULLTEXT_CHARS):
    """全文條目（標題 | SOURCE_URL:… — 內文）只保留前 max_chars 字內文；
    盡量切在句尾（. 。! ?），找不到句尾才硬切並加 …。標題與網址原樣保留。"""
    m = _FULLTEXT_ENTRY_PAT.search(entry)
    if not m:
        return entry
    head, text = entry[:m.end()], entry[m.end():]
    if len(text) <= max_chars:
        return entry
    cut = text[:max_chars]
    ends = [cut.rfind(p) for p in ('. ', '。', '! ', '? ')]
    pos = max(ends)
    if pos >= max_chars * 0.5:
        cut = cut[:pos + 1]
    else:
        cut = cut.rstrip() + '…'
    return head + cut

def _fulltext_params(category):
    """(全文條目內文上限, 全文合計佔預算比例)。11-17：corp 改用 pack 裝箱，參數見 CORP_PROMPT_FULLTEXT_*。"""
    if category in ('hw', 'app'):
        return PROMPT_FULLTEXT_CHARS, PROMPT_FULLTEXT_SHARE
    return CORP_PROMPT_FULLTEXT_CHARS, CORP_PROMPT_FULLTEXT_SHARE

def _fit_news_context(news_context, category, budget, sent_out=None):
    """把分類素材裝進 budget 字內，回傳 (送進 prompt 的文字, 統計 dict)。
    三分類都以「條」為單位裝箱、不從中間切斷條目；全文條目縮短到該分類的全文上限
    （hw/app PROMPT_FULLTEXT_CHARS、corp CORP_PROMPT_FULLTEXT_CHARS）且合計 ≤ budget×佔比，
    超出佔比的全文條目延後到摘要之後、有空間才放。
    11-17：corp 舊行為 news_context[:budget] 讓 3 則 1100 字全文吃光預算（109 則候選只送 6 則），改走同一套裝箱。
    sent_out：可選 list；給了就把實際送出的「原始條目」依序 append 進去（補抓排除、優先解碼試算用）。"""
    entries = [e for e in (news_context or '').split('\n\n') if e.strip()]
    full_total = sum(1 for e in entries if _FULLTEXT_ENTRY_PAT.search(e))
    full_chars, full_share = _fulltext_params(category)
    full_cap = int(budget * full_share)
    chosen, deferred = [], []
    chosen_orig = []
    total, full_used = 0, 0

    def _fits(e):
        return total + len(e) + (2 if chosen else 0) <= budget

    for e in entries:
        if _FULLTEXT_ENTRY_PAT.search(e):
            t = _trim_fulltext_entry(e, full_chars)
            if full_used + len(t) <= full_cap and _fits(t):
                total += len(t) + (2 if chosen else 0)
                full_used += len(t)
                chosen.append((t, True))
                chosen_orig.append(e)
            else:
                deferred.append((t, e))
    for e in entries:
        if _FULLTEXT_ENTRY_PAT.search(e):
            continue
        if _fits(e):
            total += len(e) + (2 if chosen else 0)
            chosen.append((e, False))
            chosen_orig.append(e)
    for t, e in deferred:
        if _fits(t):
            total += len(t) + (2 if chosen else 0)
            chosen.append((t, True))
            chosen_orig.append(e)
    if sent_out is not None:
        sent_out.extend(chosen_orig)
    text = '\n\n'.join(e for e, _ in chosen)
    stats = {'mode': 'pack', 'budget': budget, 'candidates': len(entries),
             'full_text_total': full_total,
             'full_text_sent': sum(1 for _, f in chosen if f),
             'sent': len(chosen), 'partial_cut': 0,
             'chars_sent': len(text), 'dropped': len(entries) - len(chosen)}
    return text, stats

RETRY_MIN_CANDIDATES = 20   # 補抓：該區候選總數下限
RETRY_MIN_REMAINING = 5     # 補抓：第一批之後剩餘候選下限
RETRY_CATEGORIES = ('hw', 'app')


def _has_real_news(items):
    """與前端「真實新聞」一致：有非空 source 且不是佔位卡。"""
    return any((it.get('source') or '').strip() and not _is_placeholder(it)
               for it in (items or []) if isinstance(it, dict))


def retry_bucket_if_empty(cat, result, news_context, recent_titles, avoid_items=None):
    """hw/app 第一次 LLM 無真實新聞且還有未看過的候選時，用下一批再問一次（最多 1 次）。
    回傳 (採用的 result, retry 統計 dict)。任何失敗都保留第一次 result，不拋例外。"""
    st = {'tried': False, 'sent': 0, 'items': 0, 'used': False, 'error': ''}
    if cat not in RETRY_CATEGORIES:
        return result, st
    saved = None
    try:
        if _has_real_news((result or {}).get(cat)):
            return result, st
        entries = [e for e in (news_context or '').split('\n\n') if e.strip()]
        if len(entries) < RETRY_MIN_CANDIDATES:
            return result, st
        first_sent = []
        saved = copy.deepcopy(RUN_STATS.get('news_context', {}).get(cat))
        _fit_news_context(news_context, cat, (RUN_STATS.get('main_call_estimate') or {}).get(cat, {}).get('news_char_budget')
                          or _category_char_budget(cat), sent_out=first_sent)
        sent_set = set(first_sent)
        remaining = [e for e in entries if e not in sent_set]
        if len(remaining) < RETRY_MIN_REMAINING:
            return result, st
        st['tried'] = True
        print(f"  → {cat} 第一批無真實新聞，剩 {len(remaining)} 條未看過，補抓第二批（等 65 秒避開 TPM）...")
        time.sleep(65)
        second_sent = []
        prompt = make_prompt(news_context, cat, recent_titles, avoid_items=avoid_items,
                             exclude_entries=first_sent, sent_out=second_sent)
        st['sent'] = len(second_sent)
        r2 = call_groq(prompt)
        st['items'] = len((r2 or {}).get(cat) or [])
        if _has_real_news((r2 or {}).get(cat)):
            st['used'] = True
            print(f"  → {cat} 補抓成功，採用第二批結果（{st['items']} 則）")
            return r2, st
        print(f"  → {cat} 補抓仍無真實新聞，保留第一次結果")
        return result, st
    except Exception as e:
        st['error'] = str(e)[:200]
        print(f"  ⚠ {cat} 補抓失敗，保留第一次結果（不影響 pipeline）：{e}")
        return result, st
    finally:
        # make_prompt 會覆寫 RUN_STATS['news_context'][cat]；還原成第一批的統計，既有欄位不變
        try:
            if saved is not None:
                RUN_STATS.setdefault('news_context', {})[cat] = saved
        except Exception:
            pass


def build_cross_bucket_block(avoid_items):
    """把同次執行中先產生的分類條目（標題+來源網址）組成追加段落；無條目回傳空字串。"""
    lines = []
    for it in (avoid_items or []):
        title = (it.get('title') or '').strip()[:CROSS_BUCKET_TITLE_CHARS]
        if not title or _is_placeholder(it):
            continue
        src = (it.get('source') or '').strip()
        lines.append("- " + title + (" (" + src + ")" if src else ""))
        if len(lines) >= CROSS_BUCKET_MAX_ITEMS:
            break
    if not lines:
        return ""
    return ("\nALREADY COVERED TODAY IN ANOTHER CATEGORY (hardware): if a candidate news item reports "
            "the same event (same company, same product, same announcement) as any report below, do NOT "
            "output it; a different event is still allowed even from the same company:\n" + "\n".join(lines))

# ── A2：STRICT_SOURCE_PROMPT ──
# 以「對 baseline prompt 做逐字替換」實作，關閉時 prompt 一個字都不動。
# 取捨：舊版「每句必含數字/日期/具名實體」＋「body ≥60 字」會逼素材只有一兩個事實的
# 新聞補寫規格與日期（10-13 Maia 300 TOPS／7 奈米、HomeView 7 吋）。最低字數降到 40，
# 對齊程式閘門 _body_is_low_quality（<40 字才判低品質），不會因此多降評；
# 「至少 1 個具體事實」保留，但必須是原文出現過的。
STRICT_PROMPT_REPLACEMENTS = [
    ("every sentence must contain at least one specific number, date, or named entity from the source AND must state a fact NOT already stated in a previous sentence. HARD MINIMUM BAR: the total body must be at least 60 Traditional Chinese characters long AND must contain at least 1 distinct concrete fact (pick one: specific numbers/quantities, technical specs, monetary amounts, timelines/dates, or place names/locations) PLUS at least 1 clearly named entity (a specific company, institution, or product name)",
     "every sentence must state a fact that appears in the source text AND that was NOT already stated in a previous sentence (a sentence does not need its own number or date). MINIMUM BAR: the total body should be at least 40 Traditional Chinese characters long AND must contain at least 1 concrete fact that appears in the source text (a number/quantity, spec, monetary amount, date/timeline, place, or a named company/institution/product doing something specific)"),
    ("If the source material genuinely cannot support 60 characters with 1 concrete fact and 1 named entity, do NOT pad",
     "If the source material genuinely cannot support 40 characters with 1 source-backed concrete fact, do NOT pad"),
    ("the 60-character / named-entity / concrete-fact bar below still applies",
     "the 40-character / source-backed-fact bar below still applies"),
    ("- body MINIMUM BAR (hard requirement): body must total at least 60 characters AND include at least 1 concrete fact (numbers/quantities, specs, monetary amounts, timelines/dates, or place names) PLUS at least 1 named entity (a specific company, institution, or product name)",
     "- body MINIMUM BAR (hard requirement): body must total at least 40 characters AND include at least 1 concrete fact that appears in the source text (numbers/quantities, specs, monetary amounts, timelines/dates, place names, or a named company/institution/product doing something specific)"),
    ("rather than stretching it to 60 characters with filler",
     "rather than stretching it to 40 characters with filler"),
    ("- HALLUCINATION IS FORBIDDEN:",
     "- SOURCE FIDELITY (numbers, dates, specs): every number, date, spec, model number, process node, screen size, launch/availability timing, and price in title/body/impact/insight MUST appear in the source text of that same news entry — if the source does not state it, leave it out; never fill in a plausible-sounding spec, date, or price from general knowledge or from other news entries\n"
     "- SOURCE FIDELITY (tense): if the source reports something as upcoming, planned, reported, or unconfirmed (wording such as will, plans to, is expected to, reportedly, according to, sources say, may, could, rumored, or a question headline), write it in zh-TW with 預計／據報導／傳出 — never as something that has already happened or been officially announced; keep the attribution (e.g. 據 Bloomberg 報導) when the source gives one\n"
     "- HALLUCINATION IS FORBIDDEN:"),
]

def apply_strict_source_prompt(prompt):
    """把 STRICT_PROMPT_REPLACEMENTS 套到 baseline prompt；找不到的片段只印警告不中斷
    （baseline 措辭若日後被改，代表這裡要同步更新）。"""
    for old, new in STRICT_PROMPT_REPLACEMENTS:
        if old in prompt:
            prompt = prompt.replace(old, new, 1)
        else:
            print(f"  ⚠ STRICT_SOURCE_PROMPT 找不到要替換的片段（略過）：{old[:50]}")
    return prompt

# 11-16：分類主呼叫 TPM 控制。1116 試跑 corp prompt 5748＋completion 1360＝7108 tokens，逼近 TPM 8K。
# 估算「system＋prompt＋預期 completion」超過 MAIN_CALL_TOKEN_TARGET 時，按差額縮減新聞字數重組 prompt（最多 4 次）。
MAIN_CALL_TOKEN_TARGET = 6800
MAIN_MIN_NEWS_CHARS = 3000
# 校準依據（11-16 兩次真 Groq 試跑）：實際 prompt_tokens ÷ estimate_tokens(system＋prompt)
#   1116a：hw 0.872、corp 0.826、app 0.841；1116b：hw 0.836、corp 0.822、app 0.845 → 平均 0.840
# 預期 completion：各分類近 7 天正式排程＋兩次試跑共 9 筆實測的中位數（hw 1168、corp 1266、app 914）
# 安全餘裕 300：目標是「估算 ≈ 實際＋300」。1116b 以此公式回算：hw 6524（實際 6196）、
#   corp 6639（實際 5718，當天 completion 只有 761）、app 5435（實際 4719）
MAIN_TOKEN_CALIBRATION = 0.84
MAIN_EXPECTED_COMPLETION = {'hw': 1170, 'corp': 1270, 'app': 910}
MAIN_TOKEN_SAFETY = 300

def make_prompt(news_context, category, recent_titles=None, avoid_items=None,
                exclude_entries=None, sent_out=None):
    budget = _category_char_budget(category)
    sys_tok = estimate_tokens(main_system_message())
    prompt, est, est_prompt = None, 0, 0
    for _ in range(4):
        tmp_sent = [] if sent_out is not None else None
        prompt = _make_prompt_once(news_context, category, recent_titles, avoid_items,
                                   exclude_entries, tmp_sent, char_budget=budget)
        est_prompt = int((sys_tok + estimate_tokens(prompt)) * MAIN_TOKEN_CALIBRATION)
        est = est_prompt + MAIN_EXPECTED_COMPLETION.get(category, 1270) + MAIN_TOKEN_SAFETY
        if est <= MAIN_CALL_TOKEN_TARGET or budget <= MAIN_MIN_NEWS_CHARS:
            break
        cut = int((est - MAIN_CALL_TOKEN_TARGET) / MAIN_TOKEN_CALIBRATION * 3.5) + 100
        budget = max(MAIN_MIN_NEWS_CHARS, budget - cut)
    if sent_out is not None and tmp_sent is not None:
        sent_out.extend(tmp_sent)
    try:
        RUN_STATS.setdefault('main_call_estimate', {})[category] = {
            'news_char_budget': budget, 'est_prompt_tokens': est_prompt,
            'est_completion_tokens': MAIN_EXPECTED_COMPLETION.get(category, 1270),
            'safety': MAIN_TOKEN_SAFETY, 'est_total_tokens': est, 'target': MAIN_CALL_TOKEN_TARGET}
    except Exception:
        pass
    return prompt

def _make_prompt_once(news_context, category, recent_titles=None, avoid_items=None,
                      exclude_entries=None, sent_out=None, char_budget=None):
    notes = load_notes() if IS_SUNDAY else {}
    notes_text = "; ".join(f"{d}:{n}" for d, n in sorted(notes.items()) if n.strip()) if notes else ""
    weekly_line = CATEGORY_WEEKLY_TEMPLATE[category]
    if category == 'app' and notes_text:
        weekly_line += '\\n用戶洞察：[根據用戶筆記的核心洞察]'
    weekly_val = f'"{weekly_line}"' if IS_SUNDAY else 'null'
    no_repeat_str = ("NO-REPEAT (STRICT): these topics were covered in recent days — do NOT generate any item about the same story or event even with a different headline; only include if there is a significant new development with wholly new facts not present before: " + "; ".join(recent_titles)) if recent_titles else ""
    notes_ctx = ("User notes context: " + notes_text[:200]) if notes_text else ""
    char_budget = char_budget or _category_char_budget(category)
    if exclude_entries:
        # 補抓用：排除第一批已送出的條目，其餘維持原排序
        _ex = set(exclude_entries)
        news_context = '\n\n'.join(e for e in (news_context or '').split('\n\n')
                                   if e.strip() and e not in _ex)
    try:
        news_short, ctx_stats = _fit_news_context(news_context, category, char_budget, sent_out=sent_out)
    except Exception as e:
        # 裝箱邏輯出錯時退回舊行為（直接截斷），不讓統計／裝箱拖垮生成
        print(f"  ⚠ 素材裝箱失敗，退回直接截斷（不影響資料生成）：{e}")
        news_short, ctx_stats = news_context[:char_budget], None
    try:
        RUN_STATS.setdefault('news_char_budget', {})[category] = char_budget
        if ctx_stats is not None:
            RUN_STATS.setdefault('news_context', {})[category] = ctx_stats
            print(f"  → {category} 送進 LLM：{ctx_stats['sent']}/{ctx_stats['candidates']} 條候選"
                  f"（全文 {ctx_stats['full_text_sent']}/{ctx_stats['full_text_total']}），"
                  f"{ctx_stats['chars_sent']}/{char_budget} 字，截掉 {ctx_stats['dropped']} 條")
    except Exception as e:
        print(f"  ⚠ 漏斗統計收集失敗（news_char_budget，不影響資料生成）：{e}")
    weekly_rule = (
        (CATEGORY_WEEKLY_RULE[category]
         + (WEEKLY_RULE_NOTES_SUFFIX if (category == 'app' and notes_text) else '')
         + '; every number in it must appear in today\'s NEWS above; write a line break as a single \\n '
         'escape inside the JSON string (never a double-escaped \\\\n)')
        if IS_SUNDAY else
        'MUST be null — today is NOT Sunday; outputting any non-null value is an error'
    )
    category_desc = CATEGORY_DESC[category]
    cross_block = build_cross_bucket_block(avoid_items) if (CROSS_BUCKET_AVOID and category == 'corp') else ""

    prompt = f"""You are an AI supply chain analyst. Analyze the news below and output pure JSON (start directly with {{).

NEWS:
{news_short}

TARGET CATEGORY (generate items for THIS category only):
- {category_desc}

OUTPUT FORMAT:
{{"date":"{DATE_STR}","is_sunday":{str(IS_SUNDAY).lower()},"{category}":[ITEMS],"glossary_new":[{{"term":"","full":"","def":"2-3 sentences","why":"why it matters","category":"semiconductor|ai_technique|hardware|role"}}],"weekly_summary":{weekly_val}}}

Each ITEM: {{"title":"Traditional Chinese title","layer":"sublayer","body":"1 to 3 sentences, ALL about the SAME single news event. Write ONLY as many sentences as the source material actually supports with a distinct fact — every sentence must contain at least one specific number, date, or named entity from the source AND must state a fact NOT already stated in a previous sentence. HARD MINIMUM BAR: the total body must be at least 60 Traditional Chinese characters long AND must contain at least 1 distinct concrete fact (pick one: specific numbers/quantities, technical specs, monetary amounts, timelines/dates, or place names/locations) PLUS at least 1 clearly named entity (a specific company, institution, or product name) — a sentence with neither a concrete fact nor a named entity (pure vague restatement of the headline) is NOT acceptable and must not be treated as done. A single honest, fact-dense sentence is ALWAYS better than 3 sentences where sentences 2-3 restate sentence 1 in different words or add generic unsourced reasoning (e.g. speculation about competitors, job creation, timelines like 'will launch soon', or vague ambition/expansion framing) — that kind of padding gets the whole item rejected as noise, so never do it. If the source material genuinely cannot support 60 characters with 1 concrete fact and 1 named entity, do NOT pad with generic reasoning to reach the length — rate the item noise instead.","impact":"2-3 sentences in zh-TW tracing the upstream/downstream ripple effects on the SUPPLY CHAIN. Structure: (1) direct effect on the closest supply chain tier (e.g. TSMC capacity, HBM ASP, OSAT utilization); (2) second-order effect on the next tier; (3) if applicable, end-market or competitor implication. Every claim must be traceable to the news source — do NOT just rephrase the body. NEVER say '可能會影響X' without stating the direction (↑/↓) and mechanism. NEVER mention stock prices.","rating":"core|opp|noise","insight":"1-sentence investor takeaway","source_label":"source name","source":"use SOURCE_URL value or empty string"}}

RULES:
- ITEM COUNT: if the NEWS above contains 2 or more distinct, concrete events that fit the TARGET CATEGORY, output 2-4 items (one item per distinct event, most material first); output exactly 1 real item only when there is exactly one such event; NEVER pad the count by splitting one event into several items, by repeating an event, or by inventing anything — every item must still obey all source-fidelity rules below
- if no relevant news → 1 noise item only, and that item's title/body must PLAINLY say so (e.g. title:"今日無相關新聞", body:"今日該分類無足夠具體新聞素材可供分析。") — do NOT invent a vague-sounding pseudo-headline like "AI 應用進步" or "產業趨勢觀察" with circular reasoning; a fake generic title is worse than admitting there is no news
- RATING PHILOSOPHY (materiality over phrasing strictness): weigh real-world significance over how strictly the wording matches a checklist — a story that is clearly a big deal should not be rated noise just because its phrasing is informal. CORE examples: a major or challenger company reducing supplier dependency via a self-developed chip (e.g. DeepSeek developing its own AI chip to cut reliance on Nvidia) -> core; a billion-dollar-scale financing round or acquisition (e.g. SambaNova closing an approximately 1.1 billion USD Series F) -> core; a 100MW-plus data-center build or firm commitment -> core or opp. NOISE examples: a small regional project that was abandoned or rejected (e.g. a 29MW project dropped in a Montana county, a Texas county rejecting a bitcoin-mining data center) -> noise; an intent-only announcement with no concrete scale or timeline -> noise. When a story clearly matches a CORE example, write the body around whichever facts ARE available (named entity plus amount/spec) rather than defaulting to noise merely because the wording is informal — the 60-character / named-entity / concrete-fact bar below still applies, but do not let phrasing strictness override obvious real-world materiality
- ONE STORY PER CARD (critical): each item covers exactly one news event or announcement; if the source contains 2 unrelated stories, create 2 separate items; NEVER mix multiple unrelated events into one body — doing so is a format error
- body LENGTH IS VARIABLE (1-3 sentences), NOT FIXED: write only as many sentences as you have distinct facts for; a true 1-sentence body outranks a padded 3-sentence one — padding is treated as noise regardless of sentence count, so there is no benefit to reaching 3
- body MINIMUM BAR (hard requirement): body must total at least 60 characters AND include at least 1 concrete fact (numbers/quantities, specs, monetary amounts, timelines/dates, or place names) PLUS at least 1 named entity (a specific company, institution, or product name) — e.g. "X簽署合約，金額N美元" DOES pass this bar (X = named entity, N美元 = concrete fact); a body with neither a concrete fact nor a named entity fails this bar; if the source truly supports neither, rate the item noise rather than stretching it to 60 characters with filler
- body FORBIDDEN: never write "這是X的重要趨勢" / "這將推動X發展" / "可能會帶來新的機會" — these add no facts; never combine two unrelated companies or events in the same body
- body GENERIC REASONING FORBIDDEN: if you only have ONE concrete fact from the source, do NOT pad the remaining sentences with generic economic-impact reasoning that could apply to any company ("將迫使競爭對手提高服務質量和降低價格", "預計將創造大量的就業機會", "投資者應該關注...的發展", "對使用者/企業產生競爭壓力") — these are template filler, not facts; sentence 2 and 3 MUST cite a different concrete detail from the source text (another number, date, named entity, or quote) — if the source genuinely offers only one fact and no named entity to pair with it, rate the item noise instead of padding
- SOURCE REQUIREMENT: every core or opp item MUST have a SOURCE_URL from the news; if no SOURCE_URL exists for a story, you MUST rate it noise — never assign core/opp to unsourced items
- SOURCE_URL LOCATION: for each news entry, "SOURCE_URL:" appears right after the headline, BEFORE the long article text that follows — do not skip past it; before writing each item's "source" field, look back at the SOURCE_URL that immediately followed that entry's headline and copy it exactly; leaving "source" empty for a story that actually has a SOURCE_URL is a mistake, not a valid choice
- HALLUCINATION IS FORBIDDEN: do not combine unrelated companies or technologies; every company-technology pairing must come directly from the news text
- FORBIDDEN ADOPTION CLAIM: NEVER write that a major platform company (Google/Microsoft/Amazon/Meta/Apple/Nvidia) "has adopted", "is using", or "already uses" technology from a smaller/startup company unless the source article EXPLICITLY names that platform company as a confirmed customer, partner, or evaluator — inference-based adoption claims are hallucinations and will be rejected
- FORBIDDEN CAPACITY TEMPLATE: NEVER write production/manufacturing capacity figures ("每月X個單位的產能", "月產X萬晶圓", "產能達每月X") for software, IP licensing, or startup companies that do not operate physical fabs — this phrasing belongs only to foundry/memory manufacturers (TSMC, Samsung, SK Hynix, Micron, OSAT); applying it to non-fab entities is a hallucination regardless of what numbers appear in other articles
- FORBIDDEN GROWTH FORECAST: NEVER invent a specific revenue-growth percentage or forecast ("該公司預計在20XX年底前將其X業務收入增加N%") unless that exact number appears verbatim in the source text — a company's real news being reported does NOT license you to guess a plausible-sounding percentage
- impact: write a genuine supply chain analysis — identify upstream suppliers, downstream customers, and competing alternatives affected by this event; state direction (↑/↓) and mechanism for each; do NOT rephrase the body; NEVER use vague phrases like "可能會影響X" without specifying direction and reason; NEVER mention stock prices
- glossary_new: required, 1-3 terms from today's news that readers may not know
- source: copy verbatim from SOURCE_URL in the news; never fabricate URLs
{SOURCE_FIDELITY_RULES}{person_name_rule_text()}
{HEADLINE_ONLY_RULES}{DESC_ONLY_RULES}- All titles, body, impact, insight in Traditional Chinese (zh-TW)
- weekly_summary: {weekly_rule}
{no_repeat_str}
{notes_ctx}{cross_block}"""
    return apply_strict_source_prompt(prompt) if STRICT_SOURCE_PROMPT else prompt

# ══════════════════════════════════════════════════════════════════
#  3. GROQ API + CHAIN QUALITY FIX
# ══════════════════════════════════════════════════════════════════
# ── Groq 呼叫間隔（11-09）：生成後的 validate_body／fix_chains／validate_impact／
# dedup_and_freshness_check 原本緊接著 app 主呼叫或補深重寫背靠背送出，同一個 60 秒窗
# 內最多 5 個請求疊加（試算見 11-09 修正報告）。改成每個生成後 LLM 請求都距離上一個
# Groq 請求結束 ≥ GROQ_WINDOW_GAP 秒；主迴圈與補深原本的固定 65 秒照舊。
GROQ_WINDOW_GAP = 65
_GROQ_STATE = {'last_end': 0.0, 'last_usage': None}

def _groq_mark(resp=None):
    """記錄一次 Groq 請求結束時間與 usage；任何錯誤都吞掉（統計不可影響主流程）。"""
    try:
        _GROQ_STATE['last_end'] = time.time()
        u = getattr(resp, 'usage', None) if resp is not None else None
        _GROQ_STATE['last_usage'] = ({'prompt_tokens': getattr(u, 'prompt_tokens', None),
                                      'completion_tokens': getattr(u, 'completion_tokens', None),
                                      'total_tokens': getattr(u, 'total_tokens', None)}
                                     if u is not None else None)
    except Exception as e:
        print(f"  ⚠ Groq 呼叫紀錄失敗（不影響資料生成）：{e}")

def _groq_wait_window():
    """距離上一個 Groq 請求結束未滿 GROQ_WINDOW_GAP 秒就補睡到滿；本次執行還沒呼叫過就不等。"""
    try:
        last = _GROQ_STATE.get('last_end') or 0.0
        if last <= 0:
            return
        wait = GROQ_WINDOW_GAP - (time.time() - last)
        if wait > 0:
            print(f"  → 避開 TPM 限流，距上一個 Groq 請求未滿 {GROQ_WINDOW_GAP} 秒，休息 {wait:.0f} 秒...")
            time.sleep(wait)
    except Exception as e:
        print(f"  ⚠ Groq 間隔計算失敗（略過等待）：{e}")

_GENERIC_LABELS = {
    '受益','受損','受壓','獲益','利好','利空','影響','波及','受害',
    '上漲','下跌','提升','下降','增加','減少','擴大','縮小'
}

def _is_bad_chain(item):
    chain = item.get('chain', [])
    if len(chain) < 2:
        return True
    for node in chain:
        label = re.sub(r'[↑↓⚠️\s]+$', '', node.get('label', '')).strip()
        if label in _GENERIC_LABELS or len(label) <= 2:
            return True
    return False

def fix_chains(data):
    bad = [(sec, item) for sec in ['hw','corp','app']
           for item in data.get(sec,[]) if _is_bad_chain(item)]
    if not bad:
        print("  → chain 品質檢核通過"); return

    print(f"  → {len(bad)} 條 chain 不合格，重新生成…")
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    # id 用 bad 列表的索引，回填時以 id 對應而非標題字串比對，
    # 避免 LLM 在輸出中改寫標題導致回填時靜默漏掉該筆
    items_json = json.dumps(
        [{"id":i,"title":item["title"],"body":item.get("body","")[:1500]}
         for i,(sec,item) in enumerate(bad)], ensure_ascii=False)

    prompt = (
        "以下新聞的 chain 使用了「受益↑」「受損↓」等泛稱或節點不足2個，請重新生成。\n"
        "要求：每條 chain 2–4 個節點；label 必須含具體公司名/產品+方向詞，"
        "例如「TSMC CoWoS 產能↑」「SK Hynix ASP↑」「Azure GPU 交期↓」「AMD 市占↓」；"
        "嚴禁使用「受益」「受損」「受壓」等泛稱。\n"
        f"條目：{items_json}\n"
        '輸出純JSON陣列（直接從[開始，id 必須原樣照抄輸入的 id，不得省略或改寫）：'
        '[{"id":0,"chain":[{"label":"具體公司+方向","type":"up"}]}]'
    )
    try:
        _groq_wait_window()
        resp = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            reasoning_effort="low",
            messages=[
                {"role":"system","content":"只輸出純JSON陣列，不加任何說明或markdown。"},
                {"role":"user","content":prompt}
            ],
            temperature=0.3, max_tokens=800,
        )
        _groq_mark(resp)
        raw = resp.choices[0].message.content.strip()
        if raw.startswith('```'):
            raw = raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
        raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
        fixes = json.loads(raw)
        sent_n, got_n = len(bad), len(fixes)
        if sent_n != got_n:
            print(f"  ⚠ 送出 {sent_n} 筆，LLM 回傳 {got_n} 筆，數量不符，僅回填可對應的 id，不整批失敗")
        fixed = 0
        for fix in fixes:
            idx = fix.get('id')
            if not isinstance(idx, int) or idx < 0 or idx >= len(bad):
                print(f"  ⚠ 跳過無效/未知 id：{fix.get('id')!r}")
                continue
            if not fix.get('chain'):
                continue
            _, item = bad[idx]
            item['chain'] = fix['chain']
            fixed += 1
            print(f"  ✓ {item['title'][:45]}")
        if fixed < sent_n:
            print(f"  ⚠ 仍有 {sent_n - fixed} 條未成功回填 chain")
        print(f"  → 共修正 {fixed} 條")
    except Exception as e:
        print(f"  → chain 修正失敗：{e}")

def validate_impact(data):
    """二次 API 驗證：impact 每句是否有 body/title 的明確因果依據，無依據則改寫或刪除"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    items = [
        {"sec": sec, "title": item["title"],
         "body": item.get("body", "")[:400],
         "impact": item.get("impact", "")}
        for sec in ['hw', 'corp', 'app']
        for item in data.get(sec, [])
        if item.get("impact") and item.get("rating") in ("core", "opp")
    ]
    if not items:
        print("  → impact 驗證：無需處理"); return

    items_json = json.dumps(items, ensure_ascii=False)
    prompt = (
        "以下每條新聞包含 title、body、impact。"
        "請逐句審查 impact：若某句提及的公司或效果在 body/title 中沒有明確的因果依據（僅因常識推測而非原文支撐），"
        "請刪除該句或改寫為只保留有依據的部分。"
        "不得因為『投資增加→晶片需求↑→TSMC訂單↑』這類多步推論而引入 body 未提及的公司。"
        "若 impact 整體無因果依據，改為空字串。\n"
        "【主角矛盾規則】若某公司已在 title 或 body 中被明確列為投資方、受益方、或主要行動者，"
        "則 impact 中禁止將該公司列為「競爭對手」或「受損方」——這是邏輯矛盾，必須刪除或改寫該句。"
        "例如：title 提到「三星和SK Hynix投資…」，impact 就不得寫「競爭對手如SK Hynix…」。\n"
        f"條目：{items_json}\n"
        '輸出純JSON陣列（直接從[開始）：[{"title":"原標題","impact":"修正後impact或空字串"}]'
    )
    try:
        _groq_wait_window()
        resp = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            reasoning_effort="low",
            messages=[
                {"role": "system", "content": "只輸出純JSON陣列，不加任何說明或markdown。全程繁體中文：合併（非合並）、合約（非合同）、晶片（非芯片）、記憶體（非内存）、硬體（非硬件）、軟體（非軟件）、影片（非視頻）、程式（非程序，例如「應用程式」不可寫「應用程序」）、智慧（非智能，例如「人工智慧」不可寫「人工智能」）、範圍（非范圍）、電腦（非計算機）、網路（非網絡）、品質（非質量，例如「服務品質」不可寫「服務質量」）、資訊（非信息，例如「地理資訊」不可寫「地理信息」）。禁止出現任何非繁體中文、非英文專有名詞的字詞（如越南文、日文、韓文等語言片語混入句子）。公司名稱一律用原文品牌名（Google、Apple、Meta、Microsoft、Amazon、Qualcomm等），不可翻譯成中文（禁止「谷歌」「蘋果」「高通」等譯名）；台灣上市公司既有中文全名（如台積電、日月光）不受影響。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1, max_tokens=1200,
        )
        _groq_mark(resp)
        raw = resp.choices[0].message.content.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
        fixes = json.loads(raw)
        fixed = 0
        for fix in fixes:
            for sec in ['hw', 'corp', 'app']:
                for item in data.get(sec, []):
                    if item['title'] == fix['title'] and 'impact' in fix:
                        old = item.get('impact', '')
                        new = fix['impact']
                        if new != old:
                            item['impact'] = new
                            fixed += 1
                            print(f"  ✓ impact 修正：{item['title'][:40]}")
        if fixed == 0:
            print("  → impact 因果驗證通過")
        else:
            print(f"  → 共修正 {fixed} 筆 impact")
    except Exception as e:
        print(f"  → impact 驗證失敗：{e}")

# 2026-08-24 新增：語意層跨日去重 + 新鮮度判斷。動機——8/22 案例：
# 「OpenAI 零資料保留承諾」8/21 已用 TechCrunch 來源報導過一次，8/22 換
# The Register 來源、換標題角度（服務名稱→效果描述）重報一次，remove_
# repeated_stories() 的標題 bigram 重疊率只有 18%（門檻 50%），完全沒
# 攔下來；同一天「阿里雲加速使用自研晶片」則是財報電話會議引用既有數據
# 的背景陳述，_body_is_low_quality 只查「有沒有具體數字/具名實體」，
# 查得到就放行，抓不到「是不是新決策」這種語意層次。兩者都不是字面
# pattern 能攔的問題，改用 LLM 語意判斷取代/補強字面比對。
def dedup_and_freshness_check(data, recent_items):
    """單一次 LLM 呼叫同時做兩件事（省 TPM，比照 validate_impact 的做法）：
    1. 語意去重：目前條目是否與近三日條目描述同一真實世界事件/決策/宣布
       （即使來源、標題、角度不同）——是則整筆移除（比照 remove_repeated_
       stories 的「同一事件只分析一次」規則）。
    2. 新鮮度判斷：目前 core 條目的 body 是否只是重述既有/持續中狀態、
       背景說明，沒有本次陳述時間點的新決策/新動作——是則降級為 opp
       （比照 downgrade_shallow_impact 的「降評不刪除」模式）。
    只對 core/opp 條目跑（noise 佔位卡不需要）；items 或 recent_items
    任一為空就跳過，失敗不影響主流程。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    current = [
        {"sec": sec, "title": item["title"], "body": item.get("body", "")[:400]}
        for sec in ['hw', 'corp', 'app']
        for item in data.get(sec, [])
        if item.get('rating') in ('core', 'opp') and not _is_placeholder(item)
    ]
    if not current or not recent_items:
        print("  → 語意去重/新鮮度檢核：無需處理"); return

    prompt = (
        "以下是「目前條目」與「近三日已報導條目」，每條包含 title、body。\n"
        "任務1（語意重複）：找出「目前條目」中，是否有任何一筆與「近三日已報導條目」"
        "描述的是同一個真實世界事件、決策、或宣布——即使來源、標題措辭、切入角度不同"
        "（例如一篇聚焦服務名稱、另一篇聚焦效果描述，但講的是同一次宣布），仍算同一事件。"
        "純粹同公司同領域但不同事件不算重複。\n"
        "任務2（新鮮度）：對「目前條目」中 rating 為 core 的每一筆，判斷 body 描述的是"
        "『本次陳述時間點的新決策/新動作/新宣布』，還是『單純重述既有或持續中的狀態、"
        "背景說明，沒有新決策內容』（例如財報電話會議引用過去已發生的數據或既有政策）。"
        "後者才算 stale_background。\n"
        f"目前條目：{json.dumps(current, ensure_ascii=False)}\n"
        f"近三日已報導條目：{json.dumps(recent_items, ensure_ascii=False)}\n"
        '輸出純JSON（直接從{開始）：{"duplicates":[{"title":"目前條目中重複的title",'
        '"matched_title":"近三日條目中對應到同一事件的那篇title","reason":"一句話說明為何判定同一事件"}],'
        '"stale_background":[{"title":"目前條目中屬於背景陳述的core title","reason":"一句話說明為何是背景陳述"}]}'
    )
    try:
        _groq_wait_window()
        resp = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            reasoning_effort="low",
            messages=[
                {"role": "system", "content": "只輸出純JSON物件，不加任何說明或markdown。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1, max_tokens=1000,
        )
        _groq_mark(resp)
        raw = resp.choices[0].message.content.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
        result = json.loads(raw)

        def _normalize_entries(raw_list):
            """把 LLM 回傳的 duplicates/stale_background 陣列正規化：
            元素可能是新schema的dict（含title/matched_title/reason），也可能是
            舊schema的純字串（向下相容）。回傳 {title: {matched_title, reason}}。"""
            out = {}
            for entry in (raw_list or []):
                if isinstance(entry, dict):
                    title = entry.get('title')
                    if not title:
                        continue
                    out[title] = {
                        'matched_title': entry.get('matched_title'),
                        'reason': entry.get('reason') or '(LLM未提供理由)',
                    }
                elif isinstance(entry, str):
                    out[entry] = {'matched_title': None, 'reason': '(LLM未提供理由)'}
            return out

        dup_info = _normalize_entries(result.get('duplicates'))
        stale_info = _normalize_entries(result.get('stale_background'))
        dup_titles = set(dup_info.keys())
        stale_titles = set(stale_info.keys())
        audit = []

        removed = 0
        for section in ['hw', 'corp', 'app']:
            if section not in data:
                continue
            kept = []
            for item in data.get(section, []):
                if not _is_placeholder(item) and item.get('title') in dup_titles:
                    removed += 1
                    print(f"  ✂ 語意重複移除：{item['title'][:60]}")
                    info = dup_info.get(item['title'], {})
                    audit.append({
                        'title': item['title'],
                        'section': section,
                        'action': 'removed',
                        'matched_title': info.get('matched_title'),
                        'reason': info.get('reason'),
                    })
                    continue
                kept.append(item)
            if not kept and data.get(section):
                kept.append(make_placeholder_item(section))
                print(f"  ＋ {section} 分類語意去重後全空，補標準佔位卡")
            data[section] = kept

        downgraded = 0
        for section in ['hw', 'corp', 'app']:
            for item in data.get(section, []):
                if item.get('rating') == 'core' and item.get('title') in stale_titles:
                    item['rating'] = 'opp'
                    downgraded += 1
                    print(f"  ↓ 背景陳述降評→opp：{item['title'][:60]}")
                    info = stale_info.get(item['title'], {})
                    audit.append({
                        'title': item['title'],
                        'section': section,
                        'action': 'downgraded',
                        'matched_title': info.get('matched_title'),
                        'reason': info.get('reason'),
                    })

        RUN_STATS['dedup_audit'] = audit

        if not removed and not downgraded:
            print("  → 語意去重/新鮮度檢核通過")
    except Exception as e:
        print(f"  → 語意去重/新鮮度檢核失敗：{e}")

def main_system_message():
    """分類主呼叫的 system 訊息（11-16 抽出成函式，make_prompt 估算 token 時共用）。"""
    return (
        "你是AI供應鏈分析師。只輸出純JSON，不加說明。全程繁體中文：晶片（非芯片）、記憶體（非内存）、當機（非宕機）、合併（非合並，例如「合併了一個拉取請求」不可寫成「合並」）、合約（非合同，例如「國防部合約」不可寫成「國防部合同」）、硬體（非硬件）、軟體（非軟件）、影片（非視頻）、程式（非程序，例如「應用程式」不可寫「應用程序」）、智慧（非智能，例如「人工智慧」不可寫「人工智能」）、範圍（非范圍）、電腦（非計算機）、網路（非網絡）、品質（非質量，例如「服務品質」不可寫「服務質量」）、資訊（非信息，例如「地理資訊」不可寫「地理信息」、「虛假資訊」不可寫「虛假信息」）。"
        "【語言鐵律】title/body/impact/insight 全部欄位只准繁體中文（含允許保留原文的英文專有名詞、公司名、產品名），絕對禁止出現任何其他語言的字詞或片語——包括但不限於越南文、日文、韓文、泰文、西班牙文——連接詞、助詞、單字都不例外（例如絕不可寫成「A nhưng B」這種中越文混雜的句子）。若不確定某詞彙屬於哪種語言，一律改寫成純繁體中文表達，不要照抄原文語言。"
        "【公司名稱】一律用原文品牌名（Google、Apple、Meta、Microsoft、Amazon、Qualcomm等），不可翻譯成中文（禁止「谷歌」「蘋果」「高通」等譯名）；台灣上市公司既有中文全名（如台積電、日月光）不受影響。"
        "【noise 鐵律】某分區若已有任何 core 或 opp 條目，該分區嚴禁再加 noise 條目。"
        "noise 條目的 title 必須是『本日無相關[分區名稱]新聞』，不得使用任何具體或看似具體的新聞標題（例如「AI 應用進步」「產業趨勢觀察」皆為違規）。"
        "【body 句數】body 是 1 到 3 句，句數視你實際掌握的具體事實數量而定，不得為了湊句數而重述前一句或加入未經證實的推論；"
        "湊出來的句子無論句數多寡都會被視為空洞輸出。"
        + system_fidelity_text()
    )

def call_groq(prompt):
    from groq import APIStatusError as GroqAPIStatusError
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = main_system_message()
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    response = None
    for model in models:
        try:
            response = client.chat.completions.create(
                model=model,
                reasoning_effort="low",
                messages=[
                    {"role":"system","content":sys_msg},
                    {"role":"user","content":prompt}
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            _groq_mark(response)
            if model != models[0]:
                print(f"  → 使用備用模型 {model}")
            break
        except GroqAPIStatusError as e:
            _groq_mark(None)
            if e.status_code == 413 and model != models[-1]:
                print(f"  → {model} 超出 TPM，切換備用模型…")
                continue
            raise
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
    finish_reason = response.choices[0].finish_reason
    if finish_reason == 'length':
        raise ValueError(f"Groq response truncated (finish_reason=length, {len(raw)} chars). Increase max_tokens.")
    # 移除 JSON 字串值內不合法的控制字元（保留 \t \n \r）
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)

# ══════════════════════════════════════════════════════════════════
#  4. URL VALIDATION
# ══════════════════════════════════════════════════════════════════
def check_url(url, timeout=6):
    """驗證 URL 是否存在；HEAD 被拒（403/405）時改用 GET 確認"""
    if not url or not url.startswith('http'):
        return False
    headers = {'User-Agent': 'Mozilla/5.0'}
    for method in ('HEAD', 'GET'):
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status < 400
        except urllib.error.HTTPError as e:
            _CHECK_URL_STATE['last_code'] = e.code
            if method == 'HEAD' and e.code in (403, 405):
                continue  # HEAD 被拒，試 GET
            return False
        except Exception:
            _CHECK_URL_STATE['last_code'] = None
            return False
    return False

_CHECK_URL_STATE = {'last_code': None}
# 11-16：1116 試跑 FT、Tech in Asia 等站對機器人回 401/403，validate_sources 把來源清空、
# downgrade_unsourced 再降 noise，畫面出現「真實標題的 noise 卡、來源—」。網址若原封不動出現在
# 當天素材（來自 RSS 或 Google News 解碼），代表是真實文章，只是擋爬蟲 → 保留來源。
BOT_BLOCK_CODES = (401, 403, 429, 451)
_MATERIAL_URLS = set()

def validate_sources(data):
    """驗證所有 NewsItem 的 source URL；失效者清空 source/source_label。
    例外：網址在當天素材中（_MATERIAL_URLS）且失敗原因是 401/403/429/451（擋爬蟲）時保留。"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            url = item.get('source', '')
            if not url or not isinstance(url, str):
                continue
            _CHECK_URL_STATE['last_code'] = None
            if check_url(url):
                print(f"  ✓ {url[:60]}")
            elif _CHECK_URL_STATE.get('last_code') in BOT_BLOCK_CODES and _norm_url(url) in _MATERIAL_URLS:
                print(f"  ✓ 擋爬蟲（HTTP {_CHECK_URL_STATE['last_code']}）但網址出自當天素材，保留：{url[:60]}")
            else:
                print(f"  ✗ 無效 URL，已清空：{url[:60]}")
                item['source'] = ''
                item['source_label'] = '—'

# 日期幻覺：body 聲稱發表/發布年份比當前年份早 2 年以上 → 舊論文/舊事件混入，降評
_STALE_YEAR_THRESHOLD = NOW.year - 1  # e.g. 2026 → 禁止 2024 以前的「發表於」年份
_STALE_DATE_PAT = re.compile(
    r'(?:發表於|發布於|公布於|於)\s*(20\d{2})\s*年',
    re.IGNORECASE
)

FORBIDDEN_PATS = [
    # 只攔「根據報導，」這種無具名來源的空泛歸因；「根據 Tobias Mann 的報導」
    # 「根據MarketScale的報導」等具名記者/媒體屬正常新聞寫法，不得誤殺
    # （2026-07-13 診斷：舊 pattern 根據.{0,25}報導 連具名歸因一起翻殺 core 卡）
    re.compile(r'根據報導[，,。]'),
    re.compile(r'已.{0,4}被多家.{0,15}公司採用.{0,10}包括'),
    re.compile(r'預計在20\d\d年底前將達到每月'),
    re.compile(r'正在助力.{0,20}的發展'),
    re.compile(r'將繼續增加'),
    re.compile(r'例如.{0,8}客戶將可以使用'),
    # 跨條目模板句（換湯不換藥）
    re.compile(r'業界第一個全堆棧安全系統'),
    re.compile(r'提供高性能和低延遲的.{0,10}解決方案'),
    re.compile(r'截至20\d\d年\d+月.{0,20}已經與超過\d+家公司合作'),
    re.compile(r'將在未來繼續推出更多的.{0,20}產品'),
    re.compile(r'表明了其在.{0,20}的重視'),
    # impact 欄位模板偵測
    re.compile(r'的供應鏈影響是正面的'),
    re.compile(r'它可以增加.{1,20}的.{1,20}能力和市場份額'),
    # 放寬：「需求」二字改為可有可無，「的」在「正面(的)影響」中改為可有可無
    re.compile(r'對下游的.{1,30}(?:需求)?產生正面的?影響'),
    re.compile(r'競爭對手.{1,30}難以跟上.{1,20}的技術進步'),
    # 「可能會對…產生重大(的)影響」骨架（零資訊萬用句，任何新聞都套得上）
    re.compile(r'可能會對.{1,30}產生重大的?影響'),
    # 空洞能力描述套話（無具體新聞事件）
    re.compile(r'人工智慧(?:可以|能夠|將可以).{0,20}(?:提高|改善|增強|優化).{0,20}(?:性能|效率|可靠性|安全性)'),
    re.compile(r'AI(?:可以|能夠|將可以).{0,20}(?:提高|改善|增強|優化).{0,20}(?:性能|效率|可靠性|安全性)'),
    # 廣泛採用預測套話（無具體公司/數字）
    re.compile(r'預計在20\d\d年.{0,10}前.{0,10}(?:被廣泛|大規模)(?:採用|应用|应用)'),
    re.compile(r'(?:廣泛|大規模)採用.{0,20}預計在20\d\d'),
    # 「等公司已經開始使用/採用 AI 技術」幻覺採用聲明
    re.compile(r'等(?:公司|企業).{0,10}已(?:經|).{0,10}(?:開始使用|採用|导入|導入).{0,20}(?:人工智慧|AI|技術)'),
    re.compile(r'(?:已|開始).{0,5}(?:廣泛|大量).{0,10}(?:使用|採用|部署).{0,10}(?:人工智慧|AI)技術'),
    # 通用經濟影響套話（任何公司新聞都套得上，非具體事實）
    re.compile(r'提高.{0,4}(?:服務)?(?:質量|品質).{0,4}和降低.{0,4}(?:服務)?價格'),
    re.compile(r'創造大量的?就業機會'),
    re.compile(r'投資者應該關注.{0,20}(?:市場的)?發展'),
    re.compile(r'產生競爭壓力.{0,10}迫使'),
    # 幻覺成長率套話（「X業務正在快速增長，該公司預計在20XX年底前收入增加N%」，數字通常無來源）
    re.compile(r'業務.{0,4}正在(?:快速)?增長.{0,15}(?:預計|预计).{0,15}(?:在)?20\d\d年.{0,4}底?前.{0,20}(?:收入|營收).{0,4}增加\d+%'),
    # insight/「注意方向」萬用競爭對手負面影響套話（任何公司都套得上，非具體推論）
    re.compile(r'對其競爭對手的.{0,15}(?:業務)?產生(?:負面)?影響'),
    re.compile(r'可能會(?:增加|提升).{0,15}業務收入.{0,10}並且可能會對'),
]

def _contains_stale_date(text: str) -> bool:
    """body 中「發表於 20XX 年」的 XX 早於去年 → 日期幻覺"""
    for m in _STALE_DATE_PAT.finditer(text):
        if int(m.group(1)) < _STALE_YEAR_THRESHOLD:
            return True
    return False

def _drop_matching_sentences(text, pats):
    """把 text 中命中任一 pat 的句子刪除，保留其餘句子。
    回傳 (清理後文字, 是否有刪除)。用 (?<=[。！？]) 保留句尾標點切句，
    比照 fix_protagonist_as_competitor 的切句寫法。無句尾標點時整段視為一句。"""
    if not text:
        return text, False
    sentences = re.split(r'(?<=[。！？])', text)
    kept = []
    dropped = False
    for sent in sentences:
        if sent and any(pat.search(sent) for pat in pats):
            dropped = True
            continue
        kept.append(sent)
    return ''.join(kept).strip(), dropped

def downgrade_forbidden_phrases(data):
    """body/insight 含禁句 → 只刪除命中的句子、保留卡片（套話是生成端習慣，不代表新聞為假）；
    刪句後 body 若過短/低品質才降級（→opp，非 noise）。日期幻覺 → 降 opp。
    impact 含模板 → 清除 impact（無論 rating）。"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            body = item.get('body', '')
            impact = item.get('impact') or ''
            insight = item.get('insight') or ''
            if item.get('rating') != 'noise':
                # 日期幻覺：body 聲稱 2 年前的「發表於」年份 → 降 opp（不再整卡 noise）
                if _contains_stale_date(body):
                    print(f"  ↓ 日期幻覺→opp：{item.get('title','')[:50]}")
                    item['rating'] = 'opp'
                else:
                    # 禁句：只刪除命中的句子，保留卡片
                    new_body, body_dropped = _drop_matching_sentences(body, FORBIDDEN_PATS)
                    if body_dropped:
                        item['body'] = new_body
                        print(f"  ✂ 禁句刪句（保留卡片）：{item.get('title','')[:50]}")
                    # insight（注意方向）只有1句（LLM prompt規定），跟body不同——body刪掉1句
                    # 套話還有其他句子撐住，insight刪掉那1句就整欄變空字串，前端會判定「沒有insight」
                    # 而整塊不顯示，信件模板卻沒做同樣判斷，兩邊顯示不一致，使用者才會看到內容缺失。
                    # 修法：只有在刪句後insight還有剩才套用，刪空就保留原句——寧可套話也不要真空
                    new_insight, insight_dropped = _drop_matching_sentences(insight, FORBIDDEN_PATS)
                    if insight_dropped and new_insight.strip():
                        item['insight'] = new_insight
                        print(f"  ✂ insight 禁句刪句：{item.get('title','')[:50]}")
                    elif insight_dropped:
                        print(f"  ⚠ insight 禁句刪句後會變空，保留原句：{item.get('title','')[:50]}")
                    # 刪句後 body 若變得過短/低品質，才走降級（→opp，非 noise）
                    if (body_dropped or insight_dropped) and _body_is_low_quality(item.get('body', '')):
                        print(f"  ↓ 禁句刪句後 body 過短→opp：{item.get('title','')[:50]}")
                        item['rating'] = 'opp'
            if impact:
                for pat in FORBIDDEN_PATS:
                    if pat.search(impact):
                        print(f"  ✕ 模板 impact 清除：{item.get('title','')[:50]}")
                        item['impact'] = None
                        break

def fix_protagonist_as_competitor(data):
    """主角矛盾修正：title/body 中明確列為投資方或受益方的公司，不得在 impact 中被稱為競爭對手。
    偵測到矛盾時清除 impact 中包含該公司的那句話（以句號切割逐句判斷）。"""
    COMPETITOR_PATS = re.compile(r'競爭對手|對手如|對手包括|競爭者如|競爭者包括')
    MAJOR_COS = ['三星','samsung','sk hynix','海力士','hynix','micron','美光',
                 'nvidia','tsmc','台積電','intel','amd','arm','qualcomm','高通',
                 'microsoft','google','meta','amazon','apple','openai','anthropic']
    count = 0
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            impact = item.get('impact') or ''
            if not impact or not COMPETITOR_PATS.search(impact):
                continue
            title_body = (item.get('title','') + ' ' + item.get('body','')).lower()
            sentences = re.split(r'(?<=[。！？])', impact)
            new_sentences = []
            changed = False
            for sent in sentences:
                if not COMPETITOR_PATS.search(sent):
                    new_sentences.append(sent)
                    continue
                # 找 impact 這句裡的公司名
                conflict = False
                for co in MAJOR_COS:
                    if co in sent.lower() and co in title_body:
                        # 這間公司在 title/body 是主角，卻在 impact 被當競爭對手
                        conflict = True
                        print(f"  ✕ 主角矛盾移除：'{sent.strip()[:60]}' ({co} 是主角)")
                        break
                if not conflict:
                    new_sentences.append(sent)
                else:
                    changed = True
                    count += 1
            if changed:
                item['impact'] = ''.join(new_sentences).strip() or None
    if count == 0:
        print("  → 主角矛盾檢核通過")
    else:
        print(f"  → 共修正 {count} 筆主角矛盾 impact")

def strip_noise_impact(data):
    """所有 noise 條目的 impact 欄位設為 None，避免模板框出現在前端"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') == 'noise' and item.get('impact'):
                item['impact'] = None

def remove_cross_item_url_duplicates(data):
    """跨分類同一來源網址（正規化後）完全相同 → 只保留一張，其餘整筆移除
    （使用者拍板：重複內容完全不再出現，不只是降評，比照 remove_repeated_stories）。
    保留優先序：rating 較高者優先（core > opp > noise）；同 rating 時，
    若某張在 hw 分類且 title+body 命中 HW_MUST_CONTAIN 則 hw 優先，
    否則保留先出現者（依走訪順序 hw→corp→app，同分類內依原順序，判準明確不留裁量）。
    僅處理「同 URL」完全重複；body 相似但 URL 不同者維持
    downgrade_cross_item_duplicates 的降評行為，不整筆移除，避免誤殺不同事件。
    移除後若分類全空，補標準佔位卡（沿用 make_placeholder_item）。"""
    RATING_ORDER = {'core': 2, 'opp': 1, 'noise': 0}
    groups = {}
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            u = _norm_url(item.get('source', ''))
            if u:
                groups.setdefault(u, []).append((section, item))

    keep_id = {}
    for u, entries in groups.items():
        if len(entries) < 2:
            continue
        best = entries[0]
        best_score = None
        for entry in entries:
            section, item = entry
            rating_score = RATING_ORDER.get(item.get('rating'), 0)
            combined = item.get('title', '') + item.get('body', '')
            hw_fit = 1 if (section == 'hw' and HW_MUST_CONTAIN.search(combined)) else 0
            score = (rating_score, hw_fit)
            if best_score is None or score > best_score:
                best_score = score
                best = entry
        keep_id[u] = id(best[1])

    removed_count = 0
    for section in ['hw', 'corp', 'app']:
        items = data.get(section, [])
        kept = []
        for item in items:
            u = _norm_url(item.get('source', ''))
            if u and u in keep_id and id(item) != keep_id[u]:
                print(f"  ✂ 跨分類同網址重複移除：[{section}] {item.get('title','')[:50]}")
                removed_count += 1
                continue
            kept.append(item)
        if not kept and items:
            kept.append(make_placeholder_item(section))
            print(f"  ＋ {section} 分類移除後全空，補標準佔位卡")
        data[section] = kept
    if removed_count == 0:
        print("  → 跨分類同網址重複檢核通過")

def downgrade_cross_item_duplicates(data):
    """跨條目 body+insight bigram 相似度 > 55% → 後者降評→noise"""
    all_items = []
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            all_items.append(item)

    for i in range(len(all_items)):
        if all_items[i].get('rating') == 'noise':
            continue
        text_i = all_items[i].get('body', '') + (all_items[i].get('insight') or '')
        bi_i = _cjk_bigrams(text_i)
        for j in range(i + 1, len(all_items)):
            if all_items[j].get('rating') == 'noise':
                continue
            text_j = all_items[j].get('body', '') + (all_items[j].get('insight') or '')
            bi_j = _cjk_bigrams(text_j)
            if bi_i and bi_j and len(bi_i) > 5 and len(bi_j) > 5:
                overlap = len(bi_i & bi_j) / min(len(bi_i), len(bi_j))
                if overlap > 0.55:
                    print(f"  ↓ 跨條目重複降評→opp：{all_items[j].get('title','')[:50]} (overlap {overlap:.0%})")
                    all_items[j]['rating'] = 'opp'

PLACEHOLDER_SECTION_NAMES = {'hw': '晶片', 'corp': '巨頭', 'app': '應用'}

def make_placeholder_item(section):
    """程式生成標準佔位卡，結構仿照 LLM 版「今日無相關…新聞」noise 卡"""
    name = PLACEHOLDER_SECTION_NAMES.get(section, '')
    return {
        "title": f"今日無相關{name}新聞",
        "layer": "",
        "body": f"今日無相關{name}新聞。",
        "impact": "",
        "rating": "noise",
        "insight": "",
        "source_label": "",
        "source": "",
    }

def _is_placeholder(item):
    """無來源網址且標題為「今日無…」「本日無…」句式的 noise 佔位卡"""
    title = item.get('title', '')
    return (not (item.get('source') or '').strip()
            and ('今日無' in title or '本日無' in title))

def remove_repeated_stories(data, recent_titles):
    """生成後 title-level 去重：LLM 改寫標題繞過 NO-REPEAT 指令時的最後一道 guard。
    用 CJK bigram + 英文詞混合 token 比對（修正中文無空格全盲問題），
    重疊率 ≥50% → 直接自當日資料移除（使用者拍板：
    同一篇文章只分析一次，重複的完全不再出現）。分類被移除到全空時補標準佔位卡。
    絕對重疊門檻動態化：短標題（雙方 token 數較小者 <6）overlap≥3 時重疊率
    輕易衝到 75% 而結構性偏鬆，故短標題要求 overlap≥4；token 數 ≥6 維持原本 overlap≥3。
    另外，標題正規化後完全相同（去空白/大小寫）一律視為重複，避免短標題因 token
    數過少而永遠無法達到 overlap≥4 的邊界情況漏放重複稿。"""
    if not recent_titles:
        return
    recent_norm = [_title_tokens(rt) for rt in recent_titles]
    recent_exact = {re.sub(r'\s+', '', rt).lower() for rt in recent_titles if rt}
    for section in ['hw', 'corp', 'app']:
        if section not in data:
            continue
        kept = []
        for item in data.get(section, []):
            if _is_placeholder(item):
                kept.append(item)  # 佔位卡天天同句式，不參與跨日比對
                continue
            title = item.get('title', '')
            title_exact = re.sub(r'\s+', '', title).lower()
            t_tokens = _title_tokens(title)
            is_dup = False
            if title_exact and title_exact in recent_exact:
                is_dup = True
            elif t_tokens:
                for rn in recent_norm:
                    if not rn:
                        continue
                    overlap = len(t_tokens & rn)
                    min_len = min(len(t_tokens), len(rn))
                    if min_len == 0:
                        continue
                    required_overlap = 4 if min_len < 6 else 3
                    if overlap >= required_overlap and overlap / min_len >= 0.5:
                        is_dup = True
                        break
            if is_dup:
                print(f"  ✂ 跨日重複移除：{title[:60]}")
            else:
                kept.append(item)
        if not kept and data.get(section):
            kept.append(make_placeholder_item(section))
            print(f"  ＋ {section} 分類移除後全空，補標準佔位卡")
        data[section] = kept

def drop_stale_placeholders(data):
    """佔位卡攔截：分類內已有至少一張有來源網址的真實卡片時，
    移除該分類所有無來源網址的 noise 佔位卡（防 LLM 佔位卡與真實卡並存）"""
    for section in ['hw', 'corp', 'app']:
        items = data.get(section, [])
        if not any((it.get('source') or '').strip() for it in items):
            continue  # 無真實卡，佔位卡保留
        kept = [it for it in items
                if not (it.get('rating') == 'noise'
                        and not (it.get('source') or '').strip())]
        removed = len(items) - len(kept)
        if removed:
            print(f"  ✂ {section} 移除 {removed} 張與真實卡並存的無來源 noise 佔位卡")
            data[section] = kept


# 確定性幻覺 pattern：兩類最常見的 Llama 幻覺結構（模組層級，供 downgrade_hallucination_patterns
# 與 validate_body 的二次佐證共用，避免同一份判準複製兩份導致日後改一邊漏一邊）
HALLUCINATION_PATTERNS = [
    # 跨公司採用幻覺：「已被 Google/Microsoft/… 採用」
    (r'已(?:被|經).{0,25}(?:Google|Microsoft|Amazon|Meta|Apple|Nvidia|AMD).{0,25}採用',
     '跨公司採用幻覺'),
    (r'(?:Google|Microsoft|Amazon|Meta|Apple|Nvidia|AMD).{0,25}已(?:採用|導入|使用)',
     '跨公司採用幻覺'),
    # 產能模板幻覺：「每月X個單位/晶圓的產能」套用在非晶圓廠公司上
    (r'每月\d[\d,]*\s*(?:個|萬|千)\s*(?:單位|晶圓)',
     '非晶圓廠產能模板幻覺'),
]

def downgrade_hallucination_patterns(data):
    """pattern-based 幻覺攔截：兩類最常見的 Llama 幻覺結構"""
    count = 0
    for sec in ['hw', 'corp', 'app']:
        for item in data.get(sec, []):
            if item.get('rating') not in ('core', 'opp'):
                continue
            body = item.get('body', '')
            for pat, reason in HALLUCINATION_PATTERNS:
                if re.search(pat, body):
                    item['rating'] = 'noise'
                    count += 1
                    print(f"  ↓ 幻覺pattern→noise：{item['title'][:50]}（{reason}）")
                    break
    if count == 0:
        print("  → 幻覺 pattern 檢核通過")
    else:
        print(f"  → 共攔截 {count} 筆")


# HW 區段不屬半導體/GPU/HBM/CoWoS/封裝/晶圓廠相關 → 降噪（自舊副本 v2.x 移植）
HW_MUST_CONTAIN = re.compile(
    r'TSMC|台積|CoWoS|HBM|OSAT|封裝|晶圓|半導體|GPU|ASIC|AI.?chip|Nvidia|AMD|晶片|'
    r'HBM|Micron|SK.?Hynix|三星|Samsung|chiplet|wafer|fab|foundry|N\d[nN]|先進封裝|'
    r'資料中心|data.?center|datacenter|電力|算力|GW|MW|AI.?infrastructure|基礎設施',
    re.IGNORECASE
)
def downgrade_hw_offtopic(data):
    """hw 區若 title+body 不含半導體/封裝/HBM/GPU 關鍵字 → noise"""
    for item in data.get('hw', []):
        if item.get('rating') == 'noise':
            continue
        combined = item.get('title','') + item.get('body','')
        if not HW_MUST_CONTAIN.search(combined):
            print(f"  ↓ HW 跑偏降評→opp：{item.get('title','')[:60]}")
            item['rating'] = 'opp'

# body 幻覺偵測：無來源預測數字（未來+%）且無財報錨點 → noise（自舊副本 v2.x 移植）
HALLUC_PAT = re.compile(r'(預計|估計|預期|將在未來).{0,15}(增加|成長|上升|達到).{0,8}\d+%')
ANCHOR_PAT = re.compile(r'(財報|報告|Q[1-4]|法說|Earnings|季報|年報|白皮書|聲明|宣布)')
def downgrade_hallucinated(data):
    """body 含無來源預測數字（未來+%）且無財報錨點 → noise"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') == 'noise':
                continue
            body = item.get('body', '')
            if HALLUC_PAT.search(body) and not ANCHOR_PAT.search(body):
                print(f"  ↓ 幻覺預測降評→noise：{item.get('title','')[:60]}")
                item['rating'] = 'noise'

def _has_deterministic_hallucination_signal(body: str) -> bool:
    """重跑 HALLUCINATION_PATTERNS／HALLUC_PAT+ANCHOR_PAT 這兩組確定性 pattern，
    供 validate_body 二次佐證用——LLM 單方面說「摘要裡找不到對應」不足以砍掉有來源
    URL 的核心新聞（摘要被截斷是常態，找不到≠捏造），需同時命中確定性紅旗才降評。"""
    if not body:
        return False
    for pat, _reason in HALLUCINATION_PATTERNS:
        if re.search(pat, body):
            return True
    if HALLUC_PAT.search(body) and not ANCHOR_PAT.search(body):
        return True
    return False


def validate_body(data, news_by_cat):
    """LLM 二次驗證：body 聲明是否能在原始 RSS 摘要中找到支撐。
    ⚠ 2026-07-13 修正：入參從合併字串改為 {'hw':str,'corp':str,'app':str} 分類字典。
    舊版把三桶合併後才截 [:2500]，corp/app 素材幾乎必落在截點之外——LLM 驗證時
    實際上只看得到 hw 的摘要，corp/app 的 core 卡在其眼中「摘要中找不到對應文字」
    而被系統性誤判幻覺翻 noise（7/10 僅 hw 倖存、7/11-7/13 全滅的主因之一）。
    改為：只擷取「有 core/opp 待驗條目」的分類，各分類均分 3000 字預算，
    保證每個受驗分類的素材都出現在 LLM 眼前。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    items = [
        {"sec": sec, "title": item["title"], "body": item.get("body", "")}
        for sec in ['hw', 'corp', 'app']
        for item in data.get(sec, [])
        if item.get("rating") in ("core", "opp")
    ]
    if not items:
        print("  → body 聲明驗證：無 core/opp 條目"); return

    cats_present = [c for c in ['hw', 'corp', 'app'] if any(it['sec'] == c for it in items)]
    per_cat_budget = 3000 // max(len(cats_present), 1)
    news_short = "\n\n".join(
        f"【{c} 分類摘要】\n{(news_by_cat.get(c) or '')[:per_cat_budget]}"
        for c in cats_present
    )
    items_json = json.dumps(items, ensure_ascii=False)
    prompt = (
        "以下是今日 RSS 新聞摘要（原始來源）：\n"
        f"---\n{news_short}\n---\n\n"
        "以下是根據這些摘要生成的新聞條目。請逐一檢查每條的 body，"
        "只針對下列兩種「最明確的捏造型態」判斷，其餘一律放行（避免誤殺紮實內容）：\n"
        "1. body 中是否聲稱 Google/Microsoft/Amazon/Meta/Apple 等大公司「已採用/已使用」某技術，"
        "但摘要中完全未提及該公司作為客戶或夥伴（跨公司採用聲稱）？→ downgrade=true\n"
        "2. body 中是否出現「每月X個單位/晶圓的產能」或明顯捏造的產能/財務數字，"
        "且摘要中未出現此數字、或該公司並非晶圓廠？→ downgrade=true\n"
        "重要：只要 body 有具體事實但摘要（已被截斷）中找不到逐字對應，"
        "不算幻覺，一律 downgrade=false——摘要是節錄，內容比摘要更詳細屬正常。\n"
        f"條目：{items_json}\n"
        '只輸出純JSON陣列：[{"title":"原標題","downgrade":true或false}]'
    )
    try:
        _groq_wait_window()
        resp = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            reasoning_effort="low",
            messages=[
                {"role": "system", "content": "只輸出純JSON陣列，不加說明或markdown。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1, max_tokens=500,
        )
        _groq_mark(resp)
        raw = resp.choices[0].message.content.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
        fixes = json.loads(raw)
        count = 0
        for fix in fixes:
            if fix.get('downgrade'):
                for sec in ['hw', 'corp', 'app']:
                    for item in data.get(sec, []):
                        if item['title'] == fix['title']:
                            # LLM 單方面說捏造不夠：摘要被截斷是常態，找不到逐字對應
                            # ≠捏造。需同時命中確定性幻覺 pattern 才真的降評，否則只記錄
                            # LLM 的懷疑、保留原評級（07-23 core 0 則事件的修法）。
                            if _has_deterministic_hallucination_signal(item.get('body', '')):
                                item['rating'] = 'opp'
                                count += 1
                                print(f"  ↓ body聲明疑似捏造→opp：{item['title'][:50]}")
                            else:
                                print(f"  ⚠ LLM懷疑捏造但無確定性佐證，保留原評級：{item['title'][:50]}")
        if count == 0:
            print("  → body 聲明驗證通過")
        else:
            print(f"  → 共降評 {count} 筆")
    except Exception as e:
        print(f"  → body 驗證失敗（略過）：{e}")


def downgrade_unsourced(data):
    """沒有 source URL 但評為 core/opp 的條目降級為 noise，防止幻覺混入"""
    count = 0
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') in ('core', 'opp') and not item.get('source', '').strip():
                item['rating'] = 'noise'
                count += 1
    if count:
        print(f"  → {count} 筆無來源條目已降級為 noise")

def _cjk_bigrams(text):
    cjk = [c for c in text if '一' <= c <= '鿿']
    return {(cjk[i], cjk[i+1]) for i in range(len(cjk)-1)}

def _cjk_prefix(text, n=5):
    return ''.join(c for c in text if '一' <= c <= '鿿')[:n]

# 具名事實偵測：兩個連續大寫開頭英文單字（如 Google Cloud / Intesa Sanpaolo）
# 或命中精簡常見科技新聞地名清單 → 視為具備具名地名/機構的具體事實
# （對齊 make_prompt RULES：1 個具體事實(含地名) + 1 個具名主體即達標，
#  不應只認阿拉伯數字，避免合格內容被 _body_is_low_quality 誤殺）
# ⚠ 2026-07-08 補強：排除英文常見冠詞/代名詞開頭（The/This/That/A/An/Our/Their/Its等）
# 起首的兩字組——這類是句子語法產物（如 "The Company"／"The Google Team"），
# 不代表真的具名實體，若不排除會讓空泛英文句子誤判為「已有具體事實」而放行。
# ⚠ 2026-07-13 修正：開頭錨點 \b 改為 (?<![A-Za-z])——Python unicode regex 下
# CJK 字元也算 \w，「根據Shoppe Black的報導」這種中文緊貼英文專有名詞的寫法
# \b 不成立導致完全匹配失敗（連 07-08 動機案例「由Intesa Sanpaolo遷移」都漏抓），
# 造成 7/11-7/13 合格 core 卡被 _body_is_low_quality 連環誤殺。
# 另補 CamelCase 單字品牌名（OpenAI/ChatGPT/MarketScale/DeepSeek），
# 這類實體只有一個「單字」，舊的「兩個連續大寫詞」判準永遠抓不到。
NAMED_ENTITY_PAT = re.compile(
    r'(?<![A-Za-z])(?!(?:The|This|That|These|Those|A|An|Our|Their|Its|His|Her|My|Your)\s)'
    r'[A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+|'
    r'(?<![A-Za-z])[A-Z][a-z]+[A-Z][a-zA-Z]*|'
    r'都靈|米蘭|東京|首爾|矽谷|新加坡|深圳|北京|上海|香港|台北|倫敦|紐約|柏林|巴黎|'
    r'阿姆斯特丹|杜拜|雪梨|多倫多|奧斯汀|班加羅爾|不丹|'
    r'Turin|Milan|Tokyo|Seoul|Silicon Valley|Beijing|Shenzhen|Shanghai|Hong Kong|'
    r'Taipei|London|New York|Berlin|Amsterdam|Dubai|Singapore|Sydney|Toronto|'
    r'Austin|Bangalore|Bhutan'
)

# 具體數字判準（%, $, 億, 倍, 具體數量；含中文數字如「四個」「第一」）——
# 抽成共用常數，供 _body_is_low_quality 與 _impact_is_shallow 共用同一套
# 「重疊率+具體性」判準，不另開一套邏輯（2026-08-03）。
NUMBER_PAT = re.compile(
    r'\d|%|億|兆|倍|萬|百億|千億|[一二三四五六七八九十兩]+[個家款次項座台支波批輪席人年月日]|第[一二三四五六七八九十]'
)

def _body_is_low_quality(body: str) -> bool:
    """True = body 不達標（重複句 or 無具體數字/具名地名機構 or 過短無分析）"""
    if not body or len(body) < 40:
        return True
    sentences = [s.strip() for s in re.split(r'[。！？]', body) if len(s.strip()) > 8]
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            # 字元集重疊率 > 65%（原有）
            s1, s2 = set(sentences[i]), set(sentences[j])
            if len(s1 & s2) / max(len(s1), len(s2), 1) > 0.65:
                return True
            # CJK bigram 重疊率 > 55%（相同主題換句話說；07-23 前為 0.45，
            # 「同主題不同事實」如營收句vs利潤句常在 45-50% 踩線誤殺，故放寬）
            b1, b2 = _cjk_bigrams(sentences[i]), _cjk_bigrams(sentences[j])
            if b1 and b2 and len(b1 & b2) / max(len(b1), len(b2), 1) > 0.55:
                return True
    # 2+ 句共用相同前 5 個中文字 → 主語重複
    prefixes = [_cjk_prefix(s) for s in sentences if len(_cjk_prefix(s)) >= 5]
    if len(prefixes) != len(set(prefixes)):
        return True
    # core/opp body 必須含數字（%, $, 億, 倍, 具體數量；含中文數字如「四個」「第一」）
    # 或具名地名/機構（NAMED_ENTITY_PAT）——兩者擇一即達標，對齊 make_prompt RULES
    if not NUMBER_PAT.search(body) and not NAMED_ENTITY_PAT.search(body):
        return True
    return False

# 2026-08-03 新增：impact 品質關卡。動機——Oracle/Gemini 案例（body 講
# 「為客戶提供了更多的AI選擇」，impact 只把「選擇」換句話說成「能力」「選擇」
# 重講一次）被評為 core，但 impact 沒有任何供應鏈影響分析的新資訊，導致
# 「冷」判定（前端 coreCount）失真——看起來不冷，實際上沒有分析價值。
# 判準沿用 _body_is_low_quality 同一套「重疊率+具體性」思路，但分母改用
# impact 自身的 bigram 數（而非 _body_is_low_quality 句對句用的對稱 max
# 分母）：impact 通常遠短於 body，若用對稱分母，重疊會被 body 的長度稀釋
# 到抓不到（Oracle 案例對稱比僅 0.14，但 impact 有 24% 的內容字面重複自
# body，用單向 containment 分母才抓得到）。門檻 0.20 是拿
# data/history.json 全量 core/opp 回測校準出來的，見
# scripts/README 或 dev log；不是憑感覺調的。
IMPACT_OVERLAP_THRESHOLD = 0.20

def _impact_is_shallow(body: str, impact: str) -> bool:
    """True = impact 只是 body 的換句話說，沒有獨立供應鏈影響分析價值。
    條件：(a) impact 與 body 的 CJK bigram 單向包含率（重疊 bigram數 /
    impact 自身 bigram 數）> IMPACT_OVERLAP_THRESHOLD，且 (b) impact 本身
    沒有補充具體數字（NUMBER_PAT）也沒有具名實體/地名（NAMED_ENTITY_PAT）
    ——兩者都缺，代表 impact 沒有跳脫 body 既有內容，純屬重講一次。"""
    if not impact or len(impact) < 10:
        return False
    b1, b2 = _cjk_bigrams(body), _cjk_bigrams(impact)
    if not b2:
        return False
    containment = len(b1 & b2) / len(b2)
    if containment <= IMPACT_OVERLAP_THRESHOLD:
        return False
    if NUMBER_PAT.search(impact) or NAMED_ENTITY_PAT.search(impact):
        return False
    return True

def downgrade_shallow_impact(data):
    """impact 只是 body 換句話說（無獨立分析價值）的 core 條目降級為 opp。
    比照 downgrade_low_quality 的降級模式：只降評級，不刪除、不清空 impact，
    讓「冷」判定的 coreCount 反映真實分析價值。"""
    count = 0
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') == 'core' and _impact_is_shallow(item.get('body', ''), item.get('impact', '')):
                item['rating'] = 'opp'
                count += 1
    if count:
        print(f"  → {count} 筆 impact 只是 body 換句話說（無獨立分析）已降級為 opp")
    else:
        print("  → impact 品質檢核通過")

def downgrade_low_quality(data):
    """body 重複或無具體數字的 core/opp 條目降級為 noise"""
    count = 0
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') == 'core' and _body_is_low_quality(item.get('body', '')):
                item['rating'] = 'opp'
                count += 1
    if count:
        print(f"  → {count} 筆低品質 body（重複句/無數字）已降級為 opp")
    else:
        print("  → body 品質檢核通過")

# ══════════════════════════════════════════════════════════════════
#  4b. FACT AUDIT（方案 B，FACT_CHECK_MODE）
#  卡片 body/insight 的數字＋單位、年份、日期、型號，逐一到該卡 source 網址
#  對應的素材原文（news_by_cat 裡同一條，含標題）找出處。找不到＝疑似補寫；
#  格式換算做不到（例如原文數字在但量級寫法不同）＝無法判定，不算違規。
#  白名單：今天日期、今年年份、本月月份、素材標題（標題本來就在素材內）；近 7 天日期列無法判定。
#  log 模式只寫 RUN_STATS['fact_audit']，絕不改卡片與評級。
# ══════════════════════════════════════════════════════════════════
_FA_NUM_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'fifteen': 15,
    'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'hundred': 100,
    'dozen': 12, 'half': 0.5, 'single': 1, 'double': 2, 'triple': 3,
}
_FA_EN_SCALE = {'thousand': 1e3, 'k': 1e3, 'million': 1e6, 'mn': 1e6, 'm': 1e6,
                'billion': 1e9, 'bn': 1e9, 'b': 1e9, 'trillion': 1e12, 'tn': 1e12, 't': 1e12}
_FA_ZH_SCALE = {'千': 1e3, '萬': 1e4, '億': 1e8, '兆': 1e12}
_FA_MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
_FA_WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
# 卡片單位 → 原文可能寫法（小寫、比對時忽略空白與連字號）
_FA_UNITS = [
    (r'%|％|個百分點', ['%', 'percent', 'per cent', 'percentage point', 'pct']),
    (r'TOPS', ['tops']),
    (r'[TPE]?FLOPS', ['flops', 'tflops', 'pflops', 'eflops']),
    (r'奈米|nm', ['nm', 'nanometer', 'nanometre', 'nano']),
    (r'英吋|吋|英寸|寸', ['inch', 'in.', '"', '”', "''"]),
    (r'[KMGTP]B', None),     # None：原文寫同一個英文單位
    (r'[kMG]W|瓦', ['w', 'watt', 'kw', 'mw', 'gw', 'kilowatt', 'megawatt', 'gigawatt']),
    (r'[GM]Hz', None),
    (r'倍', ['x', 'times', 'fold', '×']),
    (r'核心', ['core', 'cores']),
    (r'GPU|顆GPU|張GPU', ['gpu', 'gpus']),
]
_FA_UNIT_PAT = '|'.join('(?:' + u + ')' for u, _ in _FA_UNITS)
_FA_CURRENCY = r'美元|美金|元|台幣|新台幣|歐元|日圓|日元|英鎊|人民幣|韓元|港元'
_FA_HEDGE_SRC = re.compile(r'\b(will|plans? to|planning to|is expected to|are expected to|expected to|reportedly|'
                           r'according to|rumou?red|sources? (?:say|said)|may|could|might|set to|poised to)\b', re.I)
_FA_ASSERTIVE_BODY = re.compile(r'正式(?:發表|推出|發布|發佈|宣布|宣佈|上市)|已(?:經)?(?:推出|發表|發布|上市|開賣|量產)|已於')
_FA_HEDGE_BODY = re.compile(r'預計|預期|據.{0,12}報導|傳出|傳聞|消息人士|計畫|計劃|擬|可能|有望|將')

def _fa_norm_source(text):
    t = _html_unescape(text or '').lower()
    t = re.sub(r'(\d),(\d{3})', r'\1\2', t)
    t = re.sub(r'(\d),(\d{3})', r'\1\2', t)
    for w, n in _FA_NUM_WORDS.items():
        t = re.sub(r'\b' + w + r'\b', str(n), t)
    return t

def _fa_source_numbers(src):
    """原文每個數字出現：(數值, 量級換算後數值, 後接 40 字)。
    區間（$135-145 billion、10 to 20 million）共用單位：首數字也套用後面的量級。"""
    out = []
    for m in re.finditer(r'(?<![\w.])\$?(\d+(?:\.\d+)?)', src):
        v = float(m.group(1))
        tail = src[m.end():m.end() + 40]
        sm = re.match(r'\s*-?\s*(thousand|million|billion|trillion|bn|mn|tn|k|m|b|t)\b', tail)
        if not sm:
            sm = re.match(r'\s*(?:[-–—~～]|to|and)\s*\$?\d+(?:\.\d+)?\s*-?\s*'
                          r'(thousand|million|billion|trillion|bn|mn|tn)\b', tail)
        scaled = v * _FA_EN_SCALE[sm.group(1)] if sm else v
        out.append((v, scaled, tail))
    return out

def _fa_num_in(src_nums, v):
    return any(abs(sv - v) < 1e-9 for sv, _, _ in src_nums)

_FA_UNIT_WORDS = {'gb', 'mb', 'tb', 'kb', 'pb', 'nm', 'mm', 'cm', 'km', 'w', 'kw', 'mw', 'gw', 'hz', 'khz',
                  'mhz', 'ghz', 'v', 'mv', 'kv', 'ma', 'mah', 'wh', 'kwh', 'mwh', 'gbps', 'mbps',
                  'tops', 'flops', 'tflops', 'pflops', 'inch', 'x', 'k', 'm', 'b', 't', 'g'}

def _fa_unit_window(t):
    """數字後可找單位的範圍：預設 15 字；開頭是 of 放寬到 30 字（32,768 of AMD's "Zen 6" CPU cores）。
    引號內字串與「字母＋1~2 位數」型號（Zen 6）不算另一個數字；遇其他數字、逗號、分號、句點、
    and／or／with 即截斷（防 32 GB memory and 128 cores 誤配 32 核心）。"""
    t = t[:30 if t.startswith('of ') else 15]
    t = re.sub(r'"[^"]*"|“[^”]*”', ' ', t)
    t = re.sub(r'([a-z]+) \d{1,2}(?![\d.])',
               lambda m: m.group(0) if m.group(1) in _FA_UNIT_WORDS else m.group(1) + ' ', t)
    m = re.search(r'[\d,;]|\.(?:\s|$)|\b(?:and|or|with)\b', t)
    return t[:m.start()] if m else t

def _fa_unit_supported(src_nums, v, unit):
    alts = None
    for upat, ualts in _FA_UNITS:
        if re.fullmatch(upat, unit):
            alts = ualts if ualts is not None else [unit.lower()]
            break
    alts = alts or [unit.lower()]
    for sv, _, tail in src_nums:
        if abs(sv - v) > 1e-9:
            continue
        t = _fa_unit_window(re.sub(r'^[\s\-]+', '', tail))
        for a in alts:
            if len(re.sub(r'[^a-z]', '', a)) >= 3:
                # 長單位詞：數字後視窗內以單字邊界出現即可（視窗規則見 _fa_unit_window）
                if re.search(r'(?<![a-z0-9])' + re.escape(a) + r'(?:s|es)?(?![a-z])', t):
                    return True
            elif t.startswith(a):   # x、w、%、nm、in. 等短單位維持必須緊接
                return True
    return False

def _fa_date_supported(src, month, day):
    mon = _FA_MONTHS[month - 1]
    if re.search(r'\b' + mon + r'[a-z]*\.?\s+0?' + str(day) + r'\b', src):
        return True
    if re.search(r'(?<!\d)0?' + str(month) + r'[/.-]0?' + str(day) + r'(?!\d)', src):
        return True
    if re.search(r'\b' + str(day) + r'(?:st|nd|rd|th)?\s+(?:of\s+)?' + mon, src):
        return True
    # 相對日期：today/yesterday/tomorrow 與星期幾換算成實際日期（±7 天內）
    try:
        target = NOW.replace(month=month, day=day)
    except ValueError:
        return False
    delta = (target.date() - NOW.date()).days
    if 'today' in src and delta in (0, -1):
        return True
    if 'yesterday' in src and delta in (-1, -2):
        return True
    if 'tomorrow' in src and delta in (0, 1):
        return True
    if -7 <= delta <= 7 and re.search(r'\b' + _FA_WEEKDAYS[target.weekday()] + r'\b', src):
        return True
    return False

def _fa_norm_body(text):
    """卡片文字正規化：去千分位逗號、去數字與年月日之間的空白（疑似字串以此為準）。"""
    body = re.sub(r'(?<=\d),(?=\d{3})', '', text or '')
    return re.sub(r'(?<=\d)\s+(?=[年月日])|(?<=[年月])\s+(?=\d)', '', body)

def audit_text_facts(text, source_text):
    """回傳 (suspects, undetermined)：皆為 [{'token':原字串,'kind':類別}]。"""
    src = _fa_norm_source(source_text)
    src_compact = re.sub(r'[\s\-_]', '', src)
    src_nums = _fa_source_numbers(src)
    body = _fa_norm_body(text)
    taken = [False] * len(body)
    suspects, undetermined = [], []

    def claim(m):
        if any(taken[m.start():m.end()]):
            return False
        for i in range(m.start(), m.end()):
            taken[i] = True
        return True

    def flag(lst, m, kind):
        lst.append({'token': m.group(0).strip(), 'kind': kind})

    today_md = (NOW.month, NOW.day)
    # 1) 完整日期 M月D日 / M/D
    for m in re.finditer(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日|(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])', body):
        if not claim(m):
            continue
        mo, dy = (int(m.group(2)), int(m.group(3))) if m.group(2) else (int(m.group(4)), int(m.group(5)))
        if not (1 <= mo <= 12 and 1 <= dy <= 31):
            continue
        if (mo, dy) == today_md or _fa_date_supported(src, mo, dy):
            if m.group(1) and m.group(1) not in src and m.group(1) != str(NOW.year):
                flag(suspects, m, 'year')
            continue
        # 近 7 天內的日期多半是文章發布日（RSS 素材不帶發布日，模型從別處得知），
        # 對不上也無法判定是補寫，列無法判定不算違規
        try:
            back = (NOW.date() - NOW.replace(month=mo, day=dy).date()).days
        except ValueError:
            back = -1
        if 0 < back <= 7:
            flag(undetermined, m, 'recent_date')
            continue
        flag(suspects, m, 'date')
    # 2) 型號：英文字母開頭緊接數字（Maia 300、A18、GPT-5、H100、Q3）
    for m in re.finditer(r'(?<![A-Za-z0-9])([A-Za-z][A-Za-z]*)[ \-]?(\d+(?:\.\d+)?[A-Za-z0-9]*)(?![A-Za-z0-9])', body):
        word = m.group(1)
        if re.fullmatch(_FA_UNIT_PAT, word):
            continue
        if not claim(m):
            continue
        key = re.sub(r'[\s\-_]', '', m.group(0).lower())
        if key in src_compact:
            continue
        qm = re.fullmatch(r'q([1-4])', key)
        if qm and re.search(['first', 'second', 'third', 'fourth'][int(qm.group(1)) - 1] + r'[\s-]+quarter', src):
            continue
        flag(suspects, m, 'model')
    # 3) 金額/量級：數字＋兆億萬千 或 ＋幣別 或 ＋英文量級
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(兆|億|萬|千)?\s*(billion|million|trillion)?\s*(' + _FA_CURRENCY + r')?', body):
        zh_u, en_u = m.group(2), m.group(3)
        if not (zh_u or en_u or m.group(4)):
            # 區間共用單位：「1350–1450 億」首數字沒有單位，借用區間末尾的量級
            rm = re.match(r'\s*(?:[-–—~～至到]|to)\s*\d+(?:\.\d+)?\s*(兆|億|萬|千|billion|million|trillion)', body[m.end():], re.I)
            if not rm:
                continue
            if rm.group(1) in _FA_ZH_SCALE:
                zh_u = rm.group(1)
            else:
                en_u = rm.group(1).lower()
        if not claim(m):
            continue
        v = float(m.group(1))
        scale = _FA_ZH_SCALE.get(zh_u or '', 1) * _FA_EN_SCALE.get((en_u or '').lower(), 1)
        target = v * scale
        if any(abs(sc - target) <= 0.015 * max(target, 1) for _, sc, _ in src_nums):
            continue
        if _fa_num_in(src_nums, v):
            flag(undetermined, m, 'amount')   # 數字在、量級對不上：可能是換算寫法差異
        else:
            flag(suspects, m, 'amount')
    # 4) 數字＋規格單位
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(' + _FA_UNIT_PAT + r')', body):
        if not claim(m):
            continue
        if _fa_unit_supported(src_nums, float(m.group(1)), m.group(2)):
            continue
        flag(suspects, m, 'spec')
    # 5) 年份
    for m in re.finditer(r'(?<!\d)((?:19|20)\d{2})(?:\s*年)?(?!\d)', body):
        if not claim(m):
            continue
        if m.group(1) in src or m.group(1) == str(NOW.year):
            continue
        flag(suspects, m, 'year')
    # 6) 月份（無日）：本月或原文有月名即可
    for m in re.finditer(r'(?<!\d)(\d{1,2})月(?!\d)', body):
        if not claim(m):
            continue
        mo = int(m.group(1))
        if not 1 <= mo <= 12 or mo == NOW.month or re.search(r'\b' + _FA_MONTHS[mo - 1], src):
            continue
        flag(suspects, m, 'month')
    # 7) 其餘數字（≥10 或含小數；個位數多為「兩家」這類計數，略過）
    for m in re.finditer(r'(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])', body):
        if not claim(m):
            continue
        v = float(m.group(1))
        if v < 10 and '.' not in m.group(1):
            continue
        if _fa_num_in(src_nums, v):
            continue
        flag(suspects, m, 'number')
    return suspects, undetermined

def _fa_index_materials(news_by_cat):
    """{正規化網址: {'text': 標題+內文, 'title': 標題, 'full': 是否為抓到全文的素材}}"""
    idx = {}
    for cat in ('hw', 'corp', 'app'):
        for entry in (news_by_cat.get(cat) or '').split('\n\n'):
            m = re.search(r'SOURCE_URL:(\S+)', entry)
            if not m:
                continue
            full = bool(re.search(r'SOURCE_URL:\S+ — ', entry))
            # 11-16：去掉開頭所有 [label]／[HEADLINE_ONLY] 標記；headline_only＝該素材只有標題
            headline_only = (HEADLINE_ONLY_TAG in entry) and not full
            text = re.sub(r'^(?:\[[^\]]+\]\s*)+', '', entry).replace(m.group(0), ' ')
            title = text.split(' — ', 1)[0].replace(' | ', ' ').strip()
            key = _norm_url(m.group(1))
            if key not in idx or (full and not idx[key]['full']) \
                    or (idx[key].get('headline_only') and not headline_only):
                # 11-17 B：desc_only＝素材不含全文、也不是只有標題（只有 RSS 描述）；desc 為描述原文
                desc = '' if (full or headline_only) else _entry_desc(entry)
                idx[key] = {'text': text, 'title': title, 'full': full, 'headline_only': headline_only,
                            'desc_only': not full and not headline_only, 'desc': desc}
    return idx

def deepen_thin_cards(data, news_by_cat):
    """C 補深：對「core/opp、有 http(s) 來源、但素材只有 RSS 短摘要」的卡片補抓全文並只重寫該卡。
    最多 3 張、core 優先；失敗一律保留原卡並標 thin_source=True。任何錯誤都不中斷主流程。"""
    stats = {'candidates': 0, 'rewritten': 0, 'fetch_failed': 0, 'llm_failed': 0}
    try:
        RUN_STATS['deepen'] = stats
    except Exception:
        pass
    try:
        idx = _fa_index_materials(news_by_cat)
        cands = []
        for sec in ('hw', 'corp', 'app'):
            for item in data.get(sec, []):
                if item.get('rating') not in ('core', 'opp') or _is_placeholder(item):
                    continue
                url = (item.get('source') or '').strip()
                if not re.match(r'https?://', url, re.I):
                    continue
                mat = idx.get(_norm_url(url))
                if mat is not None and mat.get('full'):
                    continue
                cands.append((0 if item.get('rating') == 'core' else 1, sec, item, url,
                              (mat or {}).get('title')))
        cands.sort(key=lambda c: c[0])  # 穩定排序：core 優先，其餘維持原順序
        cands = cands[:3]
        stats['candidates'] = len(cands)
        if not cands:
            print("  → 補深：無需補深的薄卡")
            return
        print(f"  → 補深：{len(cands)} 張卡片只有短摘要，補抓全文重寫…")
        for _, sec, item, url, mat_title in cands:
            try:
                # 11-16：有原文標題就做內文有效性檢查（擋 Google 介紹頁、付費牆廣告）
                text = fetch_article_text(url, max_chars=2500, title=mat_title)
            except Exception as e:
                print(f"  ⚠ 補深抓取例外（保留原卡）：{e}")
                text = None
            if not text or len(text) < 300:
                stats['fetch_failed'] += 1
                item['thin_source'] = True
                print(f"  → 補深：全文抓取失敗，標記僅摘要：{item.get('title','')[:40]}")
                continue
            try:
                time.sleep(65)  # 上一次 Groq 呼叫剛結束，避免 TPM 疊加；之後每次之間也等
                new = call_groq(_make_deepen_prompt(item, url, text))
                body = (new.get('body') or '').strip() if isinstance(new, dict) else ''
                impact = new.get('impact', '') if isinstance(new, dict) else ''
                insight = (new.get('insight') or '').strip() if isinstance(new, dict) else ''
                chain = new.get('chain') if isinstance(new, dict) else None
                if (not isinstance(impact, str) or not insight or len(body) < 40
                        or not isinstance(chain, list) or len(chain) < 2
                        or not all(isinstance(n, dict) and isinstance(n.get('label'), str)
                                   and n['label'].strip()
                                   and n.get('type') in ('up', 'down', 'warn', 'neutral')
                                   for n in chain)):
                    raise ValueError('重寫結果欄位缺漏或 body 過短')
                item['body'] = body
                if impact.strip():  # 重寫沒給 impact 時保留原內容，不洗成空字串
                    item['impact'] = impact.strip()
                item['insight'] = insight
                item['chain'] = chain
                item.pop('thin_source', None)
                item.pop('headline_only', None)   # 已用全文重寫，不再是只有標題的卡
                item.pop('desc_only', None)       # 11-17：同上，不再是只有描述的卡
                _swap_to_full_material(news_by_cat, sec, url, text)
                stats['rewritten'] += 1
                print(f"  ✓ 補深重寫：{item.get('title','')[:40]}")
            except Exception as e:
                stats['llm_failed'] += 1
                item['thin_source'] = True
                print(f"  ⚠ 補深重寫失敗，保留原卡並標記僅摘要：{e}")
    except Exception as e:
        print(f"  ⚠ 補深流程失敗（不影響資料生成）：{e}")

def _make_deepen_prompt(item, url, text):
    return (
        "You are an AI supply chain analyst. Rewrite ONE news card using the full article text below. "
        "Output pure JSON only (start directly with {), no markdown.\n"
        f"Card title (do not change): {item.get('title','')}\n"
        f"Rating: {item.get('rating','')}; source: {item.get('source_label','')} ({url})\n"
        f"FULL ARTICLE TEXT:\n{text}\n\n"
        'Output: {"body":"...","impact":"...","insight":"...","chain":[{"label":"...","type":"up"}]}\n'
        "Rules:\n"
        "- body: 1 to 3 sentences in Traditional Chinese (zh-TW), at least 60 characters if the article supports it; "
        "if the article cannot support that, stay short and do NOT pad with generic reasoning.\n"
        "- SOURCE FIDELITY (numbers, dates, specs): every number, date, spec, model number, process node, "
        "launch/availability timing, and price in body/impact/insight MUST appear in the article text above; "
        "if it does not, leave it out.\n"
        "- SOURCE FIDELITY (tense): if the article reports something as upcoming, planned, reported, or unconfirmed "
        "(will, plans to, is expected to, reportedly, according to, sources say, may, could, rumored), "
        "write it in zh-TW with 預計／據報導／傳出, never as already happened or officially announced.\n"
        + SOURCE_FIDELITY_RULES + person_name_rule_text() + "\n" +
        "- impact: 2-3 sentences in zh-TW tracing supply chain ripple effects (closest tier, next tier, end market), "
        "state direction (↑/↓) and mechanism, every claim traceable to the article, do NOT rephrase the body, "
        "never mention stock prices; use an empty string if the article does not support any supply chain analysis.\n"
        "- insight: exactly 1 sentence investor takeaway in zh-TW.\n"
        "- chain: 2 to 4 nodes; each label must contain a specific company/product plus a direction word or arrow "
        "(e.g. TSMC CoWoS 產能↑); type is up, down or warn; never use generic labels like 受益/受損/受壓.\n"
        "- CONSISTENCY: the direction of insight and chain MUST agree with what body states "
        "(if body says funding is cut or reduced, insight and chain must not read as a funding increase or a bullish outcome).\n"
        "- All text fields in Traditional Chinese only (company and product names may stay in original language); "
        "use Taiwan terms: 晶片, 記憶體, 軟體, 硬體, 影片, 程式, 智慧, 網路, 品質, 資訊.\n"
    )

def _swap_to_full_material(news_by_cat, sec, url, text):
    """把 news_by_cat 中該網址的素材字串換成全文格式；找不到原條目就 append 到該分類。"""
    key = _norm_url(url)
    for cat in ('hw', 'corp', 'app'):
        entries = (news_by_cat.get(cat) or '').split('\n\n')
        for i, entry in enumerate(entries):
            m = re.search(r'SOURCE_URL:(\S+)', entry)
            if m and _norm_url(m.group(1)) == key:
                head = entry.split(' | SOURCE_URL:', 1)[0].split(' — ', 1)[0]
                head = head.replace(HEADLINE_ONLY_TAG + ' ', '').replace(HEADLINE_ONLY_TAG, '')
                entries[i] = f"{head} | SOURCE_URL:{m.group(1)} — {text}"
                news_by_cat[cat] = '\n\n'.join(entries)
                return
    cur = news_by_cat.get(sec) or ''
    news_by_cat[sec] = (cur + '\n\n' if cur else '') + f"[deepen] 補深全文 | SOURCE_URL:{url} — {text}"

def audit_card(item, material, other_text=''):
    """單張卡片稽核結果（不改卡片）。material=None 代表找不到素材。
    other_text＝當天其他素材全文：疑似字串若出現在別條素材，視為張冠李戴或素材截斷，
    改列無法判定（other_source），不算補寫。"""
    if material is None:
        return {'status': 'no_material'}
    joined = (item.get('body') or '') + '\n' + (item.get('insight') or '')
    suspects, undetermined = audit_text_facts(joined, material['text'])
    if suspects and other_text:
        still = {s['token'] for s in audit_text_facts(joined, material['text'] + '\n' + other_text)[0]}
        moved = [dict(s, kind='other_source') for s in suspects if s['token'] not in still]
        suspects = [s for s in suspects if s['token'] in still]
        undetermined = undetermined + moved
    # 標題檢查（只記錄）：標題的數字量級對不上（原文有該數字但量級不同，如 95 億 vs $95 billion）也列入
    title_suspects = []
    try:
        ts, tu = audit_text_facts(item.get('title') or '', material['text'])
        title_suspects = ts + [u for u in tu if u.get('kind') == 'amount']
    except Exception:
        title_suspects = []
    hedged_src = bool(_FA_HEDGE_SRC.search(material['text'][:700])) or material['title'].rstrip().endswith('?')
    body = item.get('body') or ''
    tense_flag = hedged_src and bool(_FA_ASSERTIVE_BODY.search(body))
    return {
        'status': 'checked',
        'coverage': 'full' if material['full'] else 'low_coverage',
        'suspects': suspects,
        'undetermined': undetermined,
        'title_suspects': title_suspects,
        'tense_flag': tense_flag,
    }

_FA_ABBR = re.compile(r'(?:\b(?:Inc|Corp|Ltd|Co|Mr|Mrs|Ms|Dr|vs|St|No)|\b[A-Z]|e\.g|i\.e|U\.S)$')

def _fa_split_sentences(text, use_quotes=True):
    """切句：。！？ 與英文 .!? 後接空白／結尾；引號（「」『』“” 與成對直引號）內不切。"""
    parts, buf, depth, dq, n = [], '', 0, False, len(text)
    i = 0
    while i < n:
        ch = text[i]
        buf += ch
        if use_quotes:
            if ch in '「『“':
                depth += 1
            elif ch in '」』”':
                depth = max(0, depth - 1)
            elif ch == '"':
                dq = not dq
        quoted = use_quotes and (depth > 0 or dq)
        end = False
        if not quoted:
            if ch in '。！？':
                end = True
            elif ch in '.!?' and (i + 1 == n or text[i + 1].isspace()):
                end = not (ch == '.' and _FA_ABBR.search(buf[:-1]))
        if end:
            while i + 1 < n and (text[i + 1] in '」』”"' or text[i + 1].isspace()):
                if text[i + 1] == '"' and not dq:
                    break
                i += 1
                buf += text[i]
                if text[i] == '"':
                    dq = False
            parts.append(buf)
            buf = ''
        i += 1
    if buf:
        parts.append(buf)
    if use_quotes and (depth > 0 or dq):   # 引號沒成對：退回不看引號的切法
        return _fa_split_sentences(text, use_quotes=False)
    return parts

def _fa_token_in(token, norm_sentence):
    """token 比對加數字邊界：token 頭尾是數字時，前後不可再接數字或小數點＋數字（10 不誤中 100、2010）。"""
    pat = re.escape(token)
    if token[:1].isdigit():
        pat = r'(?<![\d])(?<!\d\.)' + pat
    if token[-1:].isdigit():
        pat = pat + r'(?!\d|\.\d)'
    return re.search(pat, norm_sentence) is not None

def _fa_drop_sentences(text, tokens):
    parts = _fa_split_sentences(text or '')
    kept = [p for p in parts if p.strip() and not any(_fa_token_in(t, _fa_norm_body(p)) for t in tokens)]
    return ''.join(kept).strip()

FA_FRAGMENT_MIN_CHARS = 60
_FA_DANGLING_START = re.compile(r'^[，,、；;：:）)」』”]|^(?:此外|另外|同時|並且|而且|但是|然而|不過|因此|因而|所以|但|且|並|也)')
_FA_SENT_END = re.compile(r'[。！？.!?」』”）)]\s*$')

def _fa_body_is_fragment(body):
    """enforce 刪句後的 body 是否只剩殘句：少於 60 字、括號／引號不成對、
    開頭是接續詞或標點（前一句被刪掉）、或結尾沒有句末標點。"""
    b = (body or '').strip()
    if len(b) < FA_FRAGMENT_MIN_CHARS:
        return True
    for o, c in (('（', '）'), ('(', ')'), ('「', '」'), ('『', '』'), ('“', '”')):
        if b.count(o) != b.count(c):
            return True
    if _FA_DANGLING_START.search(b):
        return True
    return not _FA_SENT_END.search(b)

def fact_audit(data, news_by_cat, mode=None):
    """方案 B。log：只記錄到 RUN_STATS['fact_audit']；enforce：刪含疑似補寫字串的句子，
    刪完 body 不達標（_body_is_low_quality）的 core 降 opp；off：不跑。回傳 audit dict。"""
    mode = mode or FACT_CHECK_MODE
    if mode == 'off':
        return None
    idx = _fa_index_materials(news_by_cat)
    all_text = '\n'.join(m['text'] for m in idx.values())
    audit = {'mode': mode, 'checked': 0, 'suspect_cards': 0, 'tense_flags': 0, 'no_material': 0,
             'low_coverage': {'checked': 0, 'suspect_cards': 0, 'skipped_enforce': 0}, 'enforced': 0,
             'title_suspect_cards': 0, 'removed': 0, 'fragment_restored': 0, 'items': []}
    for sec in ('hw', 'corp', 'app'):
        removals = []
        for item in data.get(sec, []):
            if _is_placeholder(item) or item.get('rating') not in ('core', 'opp'):
                continue
            mat = idx.get(_norm_url(item.get('source') or ''))
            try:
                res = audit_card(item, mat, all_text.replace(mat['text'], '') if mat else '')
            except Exception as e:
                print(f"  ⚠ 單張稽核失敗（略過該張）：{e}")
                continue
            if res['status'] != 'checked':
                audit['no_material'] += 1
                continue
            audit['checked'] += 1
            low = res['coverage'] == 'low_coverage'
            if low:
                audit['low_coverage']['checked'] += 1
            if res['tense_flag']:
                audit['tense_flags'] += 1
            if res['suspects']:
                audit['suspect_cards'] += 1
                if low:
                    audit['low_coverage']['suspect_cards'] += 1
            if res.get('title_suspects'):
                audit['title_suspect_cards'] += 1
            if res['suspects'] or res['undetermined'] or res['tense_flag'] or res.get('title_suspects'):
                rec = {'sec': sec, 'title': (item.get('title') or '')[:60], 'source': item.get('source', ''),
                       'rating': item.get('rating'), 'coverage': res['coverage'],
                       'suspects': [s['token'] for s in res['suspects']],
                       'undetermined': [s['token'] for s in res['undetermined']],
                       'tense_flag': res['tense_flag'],
                       'title_suspects': [s['token'] for s in res.get('title_suspects', [])]}
                if mode == 'enforce' and res['suspects'] and low:
                    audit['low_coverage']['skipped_enforce'] += 1
                elif mode == 'enforce' and res['suspects']:
                    # 標題疑似只記錄，不刪改、不降評；單張卡例外不中斷其餘卡片
                    try:
                        toks = [s['token'] for s in res['suspects']]
                        orig_insight = item.get('insight')
                        new_body = _fa_drop_sentences(item.get('body', ''), toks)
                        if not new_body:
                            # 全部句子都疑似：整張移除（網頁與 Email 都不看 rating，noise 仍會顯示原句）。
                            # 不在迭代中刪，先收集，該分類迴圈結束後統一重建清單
                            removals.append(item)
                            rec['action'] = 'all_suspect_removed'
                            rec['removed_body'] = item.get('body', '')
                            audit['removed'] += 1
                            audit['enforced'] += 1
                            new_body = None
                        elif item.get('insight'):
                            item['insight'] = _fa_drop_sentences(item.get('insight'), toks)
                        if new_body and _fa_body_is_fragment(new_body):
                            # 11-09：刪句後只剩殘句（<60 字或句子明顯不完整）→ 還原整張原文
                            # （body 與 insight 都不動），讓讀者看到的是完整句子；疑似字串仍列在
                            # rec['suspects'] 可追查。原文含疑似補寫句，core 比照舊版刪句後降 opp
                            if 'insight' in item:
                                item['insight'] = orig_insight
                            if item.get('rating') == 'core':
                                item['rating'] = 'opp'
                                rec['downgraded'] = True
                            rec['action'] = 'fragment_restored'
                            rec['dropped_body'] = new_body
                            audit['fragment_restored'] += 1
                        elif new_body:
                            item['body'] = new_body
                            if item.get('rating') == 'core' and _body_is_low_quality(new_body):
                                item['rating'] = 'opp'
                                rec['downgraded'] = True
                            audit['enforced'] += 1
                    except Exception as e:
                        print(f"  ⚠ enforce 單張處理失敗（略過該張）：{e}")
                if len(audit['items']) < 30:
                    audit['items'].append(rec)
                if res['suspects'] or res['tense_flag']:
                    print(f"  ⚠ 疑似補寫[{sec}/{res['coverage']}] {rec['title'][:40]}：{rec['suspects']}"
                          + ("（原文為預告/傳聞，卡片寫成已發生）" if res['tense_flag'] else ''))
        if removals:
            # 迭代結束後才重建清單；分類被移到全空時補標準佔位卡（比照 remove_repeated_stories）
            data[sec] = [it for it in data.get(sec, []) if not any(it is r for r in removals)]
            if not data[sec]:
                data[sec] = [make_placeholder_item(sec)]
            print(f"  ✂ {sec} 移除 {len(removals)} 張全句疑似補寫的卡片")
    print(f"  → 原文比對（{mode}）：檢查 {audit['checked']} 張，疑似補寫 {audit['suspect_cards']} 張"
          f"（其中摘要素材 {audit['low_coverage']['suspect_cards']}/{audit['low_coverage']['checked']}），"
          f"語氣疑慮 {audit['tense_flags']} 張，標題疑似 {audit['title_suspect_cards']} 張，"
          f"無素材 {audit['no_material']} 張，整張移除 {audit['removed']} 張")
    try:
        RUN_STATS['fact_audit'] = audit
    except Exception as e:
        print(f"  ⚠ 漏斗統計收集失敗（fact_audit，不影響資料生成）：{e}")
    return audit

# ── 閘門紀錄（11-09）：包住生成後的每一關，比對前後差異寫進 RUN_STATS['gate_removed']，
# 讓「卡片被哪一關刪掉／降評」查得到。只加紀錄、不改任何閘門判斷；閘門本身的例外照舊往外丟
# （行為與直接呼叫相同），只有紀錄邏輯出錯時吞掉並印警告。
GATE_LOG_MAX = 60
_RATING_RANK = {'core': 2, 'opp': 1, 'noise': 0}

def _gate_snapshot(data):
    snap = {}
    for sec in ('hw', 'corp', 'app'):
        for item in data.get(sec, []) or []:
            if isinstance(item, dict):
                snap[id(item)] = (sec, item, item.get('rating'), (item.get('source') or '').strip())
    return snap

def _gate_reason(gate, title, action):
    """盡量帶出該關自己記的理由；沒有就回傳通用說明。"""
    try:
        r = (RUN_STATS.get('gate_reasons') or {}).get(gate, {}).get((title or '')[:60])
        if r:
            return r
        if gate == 'dedup_and_freshness_check':
            for a in RUN_STATS.get('dedup_audit') or []:
                if a.get('title') == title:
                    return (a.get('reason') or '')[:120]
        if gate == 'fact_audit':
            for a in (RUN_STATS.get('fact_audit') or {}).get('items', []):
                if a.get('title') == (title or '')[:60]:
                    return ('suspects=' + ','.join(a.get('suspects') or [])
                            + (('；' + a['action']) if a.get('action') else ''))[:120]
    except Exception:
        pass
    return action

def _run_gate(name, fn, data, *args):
    try:
        before = _gate_snapshot(data)
    except Exception as e:
        print(f"  ⚠ 閘門紀錄失敗（{name}，不影響資料生成）：{e}")
        before = None
    result = fn(data, *args)
    if before is None:
        return result
    try:
        after = _gate_snapshot(data)
        log = RUN_STATS.setdefault('gate_removed', [])
        events = []
        for key, (sec, item, rating, src) in before.items():
            title = (item.get('title') or '')
            if _is_placeholder(item) and key not in after:
                action = 'placeholder_removed'
            elif key not in after:
                action = 'removed'
            else:
                new_rating = after[key][2]
                if new_rating != rating:
                    action = f"rating {rating}→{new_rating}"
                elif src and not after[key][3]:
                    action = 'source_cleared'
                else:
                    continue
            events.append({'section': sec, 'title': title[:60], 'gate': name,
                           'action': action, 'reason': _gate_reason(name, title, action)})
        for ev in events:
            if len(log) < GATE_LOG_MAX:
                log.append(ev)
    except Exception as e:
        print(f"  ⚠ 閘門紀錄失敗（{name}，不影響資料生成）：{e}")
    return result


# ══════════════════════════════════════════════════════════════════
#  4b. 內容正確性閘門（11-16）：HEADLINE_ONLY 強制、數字閘門、LLM 查核、週報數字
# ══════════════════════════════════════════════════════════════════
# validate_sources 會清空失效網址；數字閘門／查核仍需要找回原素材，main 在清空前先記下原 source
_ORIG_SOURCES = {}

def _gate_reason_set(gate, title, reason):
    try:
        RUN_STATS.setdefault('gate_reasons', {}).setdefault(gate, {})[(title or '')[:60]] = (reason or '')[:120]
    except Exception:
        pass

def _material_for(item, idx):
    src = item.get('source') or ''
    src = (src if isinstance(src, str) else '').strip()
    if not src:
        orig = _ORIG_SOURCES.get(id(item)) or ''
        src = (orig if isinstance(orig, str) else '').strip()
    return idx.get(_norm_url(src)) if src else None

def headline_sentence(item):
    """只有標題素材時的保底 body：中文標題（已是原文標題的直譯）加句號。"""
    t = (item.get('title') or '').strip().rstrip('。.！!？?')
    return (t + '。') if t else ''

def scrub_headline_only_item(item):
    """只有標題素材的卡（11-16 決定性處理，不依賴 LLM 自律）：impact、insight、chain 一律清空，body 只留 1 句。"""
    if 'impact' in item:
        item['impact'] = ''
    if 'insight' in item:
        item['insight'] = ''
    if 'chain' in item:
        item['chain'] = []
    # 11-16 最終驗證 nice-to-have：LLM 改寫的 body 仍可能夾帶標題沒有的資訊（試跑「研發與產能擴充」），
    # 一律以標題直譯句取代；標題為空才退回只留 1 句
    hs = headline_sentence(item)
    if hs:
        item['body'] = hs
    else:
        limit_headline_body(item)

def scrub_headline_only(data):
    """生成後管線最後一步：所有 headline_only 卡做決定性清理（前面 fix_chains／validate_impact 可能又補回內容）。"""
    n = 0
    for sec in ('hw', 'corp', 'app'):
        for item in data.get(sec, []) or []:
            if isinstance(item, dict) and item.get('headline_only'):
                scrub_headline_only_item(item)
                n += 1
    try:
        RUN_STATS.setdefault('headline_only_cards', {})['scrubbed'] = n
    except Exception:
        pass
    return n

def limit_headline_body(item):
    """只有標題素材的卡：body 限 1 句，多句只留第一句。回傳是否有截斷。"""
    body = item.get('body') or ''
    parts = [x for x in _fa_split_sentences(body) if x.strip()]
    if len(parts) > 1:
        item['body'] = parts[0].strip()
        return True
    return False

def enforce_headline_only(data, news_by_cat):
    """來源素材只有標題（[HEADLINE_ONLY]）的卡片：寫入 headline_only=true，core 一律降 opp。"""
    idx = _fa_index_materials(news_by_cat)
    stats = {'marked': 0, 'downgraded': 0}
    for sec in ('hw', 'corp', 'app'):
        for item in data.get(sec, []) or []:
            try:
                if not isinstance(item, dict) or _is_placeholder(item):
                    continue
                mat = _material_for(item, idx)
            except Exception as e:
                print(f"  ⚠ HEADLINE_ONLY 單張判斷失敗（略過該張）：{e}")
                continue
            if mat is not None and mat.get('headline_only'):
                item['headline_only'] = True
                stats['marked'] += 1
                if limit_headline_body(item):
                    stats['body_trimmed'] = stats.get('body_trimmed', 0) + 1
                if item.get('rating') == 'core':
                    item['rating'] = 'opp'
                    stats['downgraded'] += 1
                    _gate_reason_set('enforce_headline_only', item.get('title'), '素材只有標題，core 上限 opp')
    if stats['marked']:
        print(f"  → HEADLINE_ONLY：{stats['marked']} 張卡只有標題素材，其中 {stats['downgraded']} 張 core→opp")
    try:
        RUN_STATS['headline_only_cards'] = stats
    except Exception:
        pass
    return stats

# ── 11-17 B：只有 RSS 描述的卡（desc_only）──
# 全文抓取失敗、描述保留（fetch_failed_desc_kept），以及任何最終素材不含全文（也不是只有標題）的卡。
# 比照 headline_only：body 只能寫描述裡有的資訊、最多 DESC_ONLY_MAX_SENTENCES 句；impact／insight／chain 清空；
# 最高 opp；必送 LLM 查核（查核以描述為唯一素材）。
DESC_ONLY_MAX_SENTENCES = 2

def limit_desc_body(item):
    """desc_only 卡：body 最多 DESC_ONLY_MAX_SENTENCES 句，多的截掉。回傳是否有截斷。"""
    body = item.get('body') or ''
    parts = [x.strip() for x in _fa_split_sentences(body) if x.strip()]
    if len(parts) > DESC_ONLY_MAX_SENTENCES:
        item['body'] = ''.join(parts[:DESC_ONLY_MAX_SENTENCES])
        return True
    return False

def scrub_desc_only_item(item):
    """desc_only 卡決定性清理（不依賴 LLM 自律）：impact、insight、chain 清空，body 最多 2 句，core 降 opp。"""
    if 'impact' in item:
        item['impact'] = ''
    if 'insight' in item:
        item['insight'] = ''
    if 'chain' in item:
        item['chain'] = []
    limit_desc_body(item)
    if not (item.get('body') or '').strip():
        item['body'] = headline_sentence(item)
    if item.get('rating') == 'core':
        item['rating'] = 'opp'

def enforce_desc_only(data, news_by_cat):
    """素材只有 RSS 描述的卡：寫入 desc_only=true、body 限 2 句、core 一律降 opp。
    放在 deepen_thin_cards 之後：補深成功的卡素材已換成全文，不會被標。"""
    idx = _fa_index_materials(news_by_cat)
    stats = {'marked': 0, 'downgraded': 0, 'body_trimmed': 0}
    for sec in ('hw', 'corp', 'app'):
        for item in data.get(sec, []) or []:
            try:
                if not isinstance(item, dict) or _is_placeholder(item) or item.get('headline_only'):
                    continue
                mat = _material_for(item, idx)
                if mat is None or not mat.get('desc_only'):
                    item.pop('desc_only', None)
                    continue
                item['desc_only'] = True
                stats['marked'] += 1
                if limit_desc_body(item):
                    stats['body_trimmed'] += 1
                if item.get('rating') == 'core':
                    item['rating'] = 'opp'
                    stats['downgraded'] += 1
                    _gate_reason_set('enforce_desc_only', item.get('title'), '素材只有 RSS 描述，core 上限 opp')
            except Exception as e:
                print(f"  ⚠ DESC_ONLY 單張判斷失敗（略過該張）：{e}")
    if stats['marked']:
        print(f"  → DESC_ONLY：{stats['marked']} 張卡只有 RSS 描述素材，其中 {stats['downgraded']} 張 core→opp")
    try:
        RUN_STATS['desc_only_cards'] = stats
    except Exception:
        pass
    return stats

def scrub_desc_only(data):
    """生成後管線最後一步：所有 desc_only 卡做決定性清理（前面 fix_chains／validate_impact 可能又補回內容）。"""
    n = 0
    for sec in ('hw', 'corp', 'app'):
        for item in data.get(sec, []) or []:
            if isinstance(item, dict) and item.get('desc_only') and not item.get('headline_only'):
                scrub_desc_only_item(item)
                n += 1
    try:
        RUN_STATS.setdefault('desc_only_cards', {})['scrubbed'] = n
    except Exception:
        pass
    return n

# ── 數字閘門 ──
# 數值以「換算後的值＋卡片顯示精度」比對：卡片 68.7 萬＝687,000±500，原文 678,000 → 不符；
# 67.8 萬＝678,000±500 → 相符；150 萬 vs 1.5 million、40.8 億 vs 4.08 billion → 相符。
_NG_ZH_SCALE = {'千萬': 1e7, '百萬': 1e6, '十億': 1e9, '百億': 1e10, '千億': 1e11, '萬億': 1e12,
                '兆': 1e12, '億': 1e8, '萬': 1e4, '千': 1e3}
_NG_EN_SCALE = {'thousand': 1e3, 'million': 1e6, 'billion': 1e9, 'trillion': 1e12,
                'bn': 1e9, 'mn': 1e6, 'tn': 1e12, 'k': 1e3, 'm': 1e6, 'b': 1e9, 't': 1e12}
_NG_ORDINALS = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6,
                'seventh': 7, 'eighth': 8, 'ninth': 9, 'tenth': 10}
# 量級三個 group：(中文量級)(英文量級字)(緊接的 k/m/b/t)；_NG_NUM_PAT 與 _NG_RANGE_TAIL 共用，
# 取值一律走 _ng_scale_from(三個字串)，不依賴 group 編號（11-16 審查 M1：區間曾讀到不存在的 group）
_NG_SCALE_RE = (r'(?:\s*(千萬|百萬|十億|百億|千億|萬億|兆|億|萬|千(?!瓦))'
                r'|\s*-?\s*(thousand|million|billion|trillion|bn|mn|tn)\b'
                r'|(k|m|b|t)(?![a-z]))?')
_NG_NUM_PAT = re.compile(r'(?<![A-Za-z0-9.])(?<![A-Za-z]-)(\d+(?:\.\d+)?)' + _NG_SCALE_RE, re.I)
_NG_RANGE_TAIL = re.compile(r'\s*(?:[-–—~～至到]|to)\s*\$?\d+(?:\.\d+)?' + _NG_SCALE_RE, re.I)
_NG_PCT_TAIL = re.compile(r'\s*(?:%|％|percent|per cent|個百分點|percentage point)', re.I)
_NG_FW = str.maketrans('０１２３４５６７８９．，', '0123456789.,')

def _ng_norm(text, source=False):
    t = (text or '').translate(_NG_FW)
    if source:
        t = _fa_norm_source(t)
        for w, n in _NG_ORDINALS.items():
            t = re.sub(r'\b' + w + r'\b', str(n), t)
    else:
        t = t.lower()
    t = re.sub(r'(?<=\d),(?=\d{3})', '', t)
    t = re.sub(r'(?<=\d),(?=\d{3})', '', t)
    return t

def _ng_scale_from(zh, en, short):
    if zh:
        return _NG_ZH_SCALE[zh]
    if en:
        return _NG_EN_SCALE[en.lower()]
    if short:
        return _NG_EN_SCALE[short.lower()]
    return 1.0

def _ng_extract(text, source=False):
    """回傳 [{'v':數值,'scaled':換算後,'tol':容許誤差,'scaled_flag':是否帶量級,'kind':類別,
    'pct':是否百分比,'model':是否型號（英文字＋空白緊接數字，如 RTX 5090）,'raw':原字串,'span':(s,e)}]。
    卡片端另抽日期、月份、年份、季度；原文端只抽數值。區間（$135-145 billion、1,350 至 1,450 億）首數字借用尾端量級。"""
    t = _ng_norm(text, source)
    out = []
    taken = [False] * (len(t) + 1)

    def claim(s, e):
        if any(taken[s:e]):
            return False
        for i in range(s, e):
            taken[i] = True
        return True

    if not source:
        for m in re.finditer(r'(?:((?:19|20)\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日'
                             r'|(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/])', t):
            if not claim(m.start(), m.end()):
                continue
            if m.group(2):
                out.append({'kind': 'date', 'mo': int(m.group(2)), 'dy': int(m.group(3)),
                            'year': m.group(1), 'raw': m.group(0), 'span': m.span()})
            else:
                out.append({'kind': 'date', 'mo': int(m.group(4)), 'dy': int(m.group(5)),
                            'year': None, 'raw': m.group(0), 'span': m.span()})
        for m in re.finditer(r'(?<![a-z0-9])q([1-4])(?![0-9])|第\s*([1-4])\s*季', t):
            if claim(m.start(), m.end()):
                out.append({'kind': 'quarter', 'q': int(m.group(1) or m.group(2)),
                            'raw': m.group(0), 'span': m.span()})
        for m in re.finditer(r'(?<![\d.])((?:19|20)\d{2})(?:\s*年)?(?![\d.]|\s*(?:億|萬|千|兆|billion|million))', t):
            if claim(m.start(), m.end()):
                out.append({'kind': 'year', 'year': m.group(1), 'raw': m.group(0), 'span': m.span()})
        for m in re.finditer(r'(?<![\d.])(\d{1,2})\s*月(?![\d底初中])|(?<![\d.])(\d{1,2})\s*月(?=[底初中])', t):
            if claim(m.start(), m.end()):
                out.append({'kind': 'month', 'mo': int(m.group(1) or m.group(2)),
                            'raw': m.group(0), 'span': m.span()})
    for m in _NG_NUM_PAT.finditer(t):
        if not claim(m.start(1), m.end(1)):
            continue
        num = m.group(1)
        v = float(num)
        scale = _ng_scale_from(m.group(2), m.group(3), m.group(4))
        if scale == 1.0:
            rm = _NG_RANGE_TAIL.match(t, m.end())
            if rm and (rm.group(1) or rm.group(2) or rm.group(3)):
                scale = _ng_scale_from(rm.group(1), rm.group(2), rm.group(3))
        dec = len(num.split('.')[1]) if '.' in num else 0
        tol = 0.5 * (10 ** -dec) * scale if scale > 1 else 1e-9
        pct = scale == 1.0 and bool(_NG_PCT_TAIL.match(t, m.end()))
        model = bool(re.search(r'[a-z]\s$', t[max(0, m.start(1) - 2):m.start(1)])) \
            or bool(scale == 1.0 and re.match(r'[a-z]', t[m.end(1):m.end(1) + 1]))
        out.append({'kind': 'num', 'v': v, 'scaled': v * scale, 'tol': tol, 'scaled_flag': scale > 1,
                    'pct': pct, 'dec': dec, 'model': model, 'raw': m.group(0).strip(), 'span': m.span()})
    return out

def _ng_num_supported(tok, src_nums):
    for s in src_nums:
        if s['kind'] != 'num':
            continue
        if abs(s['scaled'] - tok['scaled']) <= max(tok['tol'], s['tol']) + 1e-9 * max(1.0, abs(tok['scaled'])):
            return True
        if not tok['scaled_flag'] and abs(s['v'] - tok['v']) < 1e-9:
            return True
        # 百分比四捨五入：原文 12.3%、卡片 12% 或 12.3% 皆可（卡片精度以內）
        if tok.get('pct') and s.get('pct') and abs(s['v'] - tok['v']) <= 0.5 * (10 ** -tok.get('dec', 0)) + 1e-9:
            return True
    return False

def _ng_year_supported(year, src_norm):
    if re.search(r'(?<!\d)' + year + r'(?!\d)', src_norm):
        return True
    yy = year[2:]
    # FY26／FY 2026／fiscal 26：視同 2026 財年
    return re.search(r"\bfy\s*'?" + yy + r'(?!\d)|\bfiscal\s+(?:year\s+)?\'?' + yy + r'(?!\d)', src_norm) is not None

def _ng_token_supported(tok, src_norm, src_nums):
    k = tok['kind']
    if k == 'num':
        return _ng_num_supported(tok, src_nums)
    if k == 'year':
        return _ng_year_supported(tok['year'], src_norm)
    if k == 'quarter':
        q = tok['q']
        return bool(re.search(r'(?<![a-z0-9])q' + str(q) + r'(?![0-9])', src_norm)
                    or re.search(r'(?<!\d)' + str(q) + r'(?:st|nd|rd|th)?[\s-]+quarter', src_norm)
                    or re.search(r'第\s*' + str(q) + r'\s*季', src_norm))
    if k == 'month':
        mo = tok['mo']
        if not 1 <= mo <= 12:
            return False
        return mo == NOW.month or re.search(r'\b' + _FA_MONTHS[mo - 1], src_norm) is not None \
            or re.search(r'(?<!\d)' + str(mo) + r'\s*月', src_norm) is not None
    if k == 'date':
        mo, dy = tok['mo'], tok['dy']
        if not (1 <= mo <= 12 and 1 <= dy <= 31):
            return False
        if tok.get('year') and not _ng_year_supported(tok['year'], src_norm) \
                and tok['year'] != str(NOW.year):
            return False
        if (mo, dy) == (NOW.month, NOW.day) or _fa_date_supported(src_norm, mo, dy) \
                or re.search(r'(?<!\d)' + str(mo) + r'\s*月\s*' + str(dy) + r'\s*日', src_norm):
            return True
        # 近 7 天內的日期多半是文章發布日（RSS 素材常不帶發布日），比照 fact_audit 視為無法判定、不刪
        try:
            back = (NOW.date() - NOW.replace(month=mo, day=dy).date()).days
        except ValueError:
            return False
        return 0 < back <= 7
    return True

def numbers_unsupported(text, source_text):
    """text 中找不到原文出處的數字字串清單（空清單＝全部有出處）。"""
    src_norm = _ng_norm(source_text, source=True)
    src_nums = _ng_extract(source_text, source=True)
    return [t['raw'] for t in _ng_extract(text) if not _ng_token_supported(t, src_norm, src_nums)]

def numbers_contradict(title, body, source_text=None):
    """標題與內文數字矛盾（11-16 審查 M2 修正）：只有「標題數字在原文找不到，而內文有接近值
    （0.8～1.25 倍、超出顯示精度）且該值在原文找得到」才算，例如標題 68.7 萬、內文與原文 678,000。
    年增對比、財測、不同年度（92 億 vs 去年 75 億）因標題值本身有出處，不判矛盾；
    型號（RTX 5090 vs 5080）與年份不列入比較；沒有原文時無法判定，回傳空清單。回傳 [(標題字串, 內文字串)]。"""
    if not source_text:
        return []
    src_norm = _ng_norm(source_text, source=True)
    src_nums = _ng_extract(source_text, source=True)

    def _cands(text):
        return [t for t in _ng_extract(text) if t['kind'] == 'num' and not t.get('model')
                and (t['scaled_flag'] or t['scaled'] >= 1000)]
    out = []
    for a in _cands(title):
        if _ng_token_supported(a, src_norm, src_nums):
            continue
        for b in _cands(body):
            if a['scaled'] <= 0 or b['scaled'] <= 0:
                continue
            if not 0.8 <= a['scaled'] / b['scaled'] <= 1.25:
                continue
            if abs(a['scaled'] - b['scaled']) <= max(a['tol'], b['tol']) + 1e-9 * a['scaled']:
                continue
            if _ng_token_supported(b, src_norm, src_nums):
                out.append((a['raw'], b['raw']))
    return out

def _ng_drop_unsupported_sentences(text, source_text):
    """刪除含無出處數字的句子，回傳 (新文字, 被刪的數字清單)。"""
    parts = _fa_split_sentences(text or '')
    kept, dropped = [], []
    for p in parts:
        if not p.strip():
            continue
        bad = numbers_unsupported(p, source_text)
        if bad:
            dropped.extend(bad)
        else:
            kept.append(p)
    return ''.join(kept).strip(), dropped

def numeric_gate(data, news_by_cat):
    """程式數字閘門：title 數字找不到原文 → 移除該卡（若內文有接近且有出處的值，記為「標題與內文矛盾」）；
    body 含無出處數字的句子刪除，刪完為空 → 移除。
    找不到素材的卡（來源空白或非素材網址）以空字串當原文：title 有數字即移除、body 數字句刪除。"""
    idx = _fa_index_materials(news_by_cat)
    stats = RUN_STATS.setdefault('numeric_gate', {'runs': 0, 'removed': 0, 'sentences_dropped': 0,
                                                 'contradiction_removed': 0, 'no_material': 0, 'items': []})
    stats['runs'] += 1
    for sec in ('hw', 'corp', 'app'):
        removals = []
        for item in data.get(sec, []) or []:
            if not isinstance(item, dict) or _is_placeholder(item):
                continue
            try:
                mat = _material_for(item, idx)
                if mat is None:
                    stats['no_material'] += 1
                src = mat['text'] if mat else ''
                title = item.get('title') or ''
                bad_title = numbers_unsupported(title, src)
                conflict = numbers_contradict(title, item.get('body') or '', src) if bad_title else []
                if conflict:
                    # 標題數字無出處、內文接近值有出處 → 標題寫錯（例：68.7 萬 vs 678,000）
                    removals.append(item)
                    stats['contradiction_removed'] += 1
                    reason = '標題與內文數字矛盾：' + '、'.join(f"{a} vs {b}" for a, b in conflict)
                elif bad_title:
                    removals.append(item)
                    reason = '標題數字無原文出處：' + '、'.join(bad_title)
                else:
                    new_body, dropped = _ng_drop_unsupported_sentences(item.get('body') or '', src)
                    reason = ''
                    if dropped:
                        stats['sentences_dropped'] += 1
                        reason = 'body 數字無原文出處：' + '、'.join(dropped)
                        if not new_body:
                            removals.append(item)
                        else:
                            item['body'] = new_body
                if reason:
                    _gate_reason_set('numeric_gate', title, reason)
                    if len(stats['items']) < 30:
                        stats['items'].append({'sec': sec, 'title': title[:60], 'reason': reason[:120],
                                               'removed': any(item is r for r in removals)})
                    print(f"  ✂ 數字閘門[{sec}] {title[:40]}：{reason[:80]}")
            except Exception as e:
                print(f"  ⚠ 數字閘門單張處理失敗（略過該張）：{e}")
        if removals:
            stats['removed'] += len(removals)
            data[sec] = [it for it in data.get(sec, []) if not any(it is r for r in removals)]
            if not data[sec]:
                data[sec] = [make_placeholder_item(sec)]
    return stats

# ── LLM 查核關卡 ──
def estimate_tokens(text):
    """粗估 token：中日韓字元約 1 token／字，其餘約 3.5 字元／token（偏保守）。"""
    text = text or ''
    cjk = len(re.findall(r'[　-〿一-鿿＀-￯]', text))
    return int(cjk + (len(text) - cjk) / 3.5) + 1

def _fact_check_header():
    pairs = '、'.join(f"{en}→{zh}" for en, zh in PERSON_NAME_ZH.items())
    return (
        "你是新聞查核員。下面每一則是根據「原文素材」寫成的新聞卡片。請只依各則自己的原文素材逐則查核，"
        "不得使用原文以外的知識、不得引入原文沒有的資訊。\n"
        "查核項目：數字與單位、時間範圍（單季／累計／全年、實際值或預測年份）、指標種類（營收／營業利益／淨利、"
        "預測或實際）、限定語（up to＝最多、at least＝至少、about＝約，不可寫成「超過」）、發言歸屬（標題或作者的話"
        "不可寫成某人原話）、人物職稱與所屬機構、地理與對象範圍、人名（只准以下對照表用中文：" + pairs +
        "；表外人名必須保留原文英文拼寫）。\n"
        "【只有標題的卡片】SOURCE 標示「只有標題」時，標題是唯一素材，只查 TITLE 與 BODY：凡是標題沒寫的事實"
        "（用途、背景、職稱、數字、型號、影響、因果推論）都算錯，判 fix，並在 body 回傳「一句直譯原文標題」的修正句。\n"
        "【只有 RSS 描述的卡片】SOURCE 標示「只有 RSS 描述」時，標題與描述是唯一素材，只查 TITLE 與 BODY：BODY 最多 2 句，"
        "凡是描述沒寫的資訊（全球等範圍、細項分類、與前期比較、趨勢、用途、因果推論）都算錯，判 fix，"
        "並在 body 回傳只含描述資訊的修正句（寫不出來就回傳一句直譯描述）。\n"
        "【範圍、語氣、歸屬】下列三類一律判 fix 並回傳修正後的 title／body：①擴大範圍——原文講部分、特定環節、"
        "單一模型或產品，卡片寫成整體（例：某模型的 prompt processing 約三天，不可寫成整個 GPU 上線約三天）；"
        "②改變語氣強度——cannot maintain 是「無法維持」不是「將失去」、may／could 是「可能」不是「將」、"
        "up to 是「最多」不是「超過」；③漏掉歸屬——原文是某人或某公司的說法、或對特定對象（如 IPO 投資人）的陳述，"
        "卡片必須保留歸屬（「據 X 表示」「X 向投資人稱」），不可寫成既定事實。\n"
        "判定：ok＝全部有原文支撐；fix＝有錯但可只靠原文修正（刪掉或改成原文說法，不准補新資訊），"
        "回傳修正後的 title 與 body（若 impact／insight／chain 有列出且有錯，一併回傳修正版，"
        'chain 格式 [{"label":"…","type":"up|down|warn|neutral"}]）；'
        "drop＝主要事實與原文不符且無法修正。title／body 一律繁體中文（台灣用語）。\n"
        "輸出純 JSON 陣列，不加說明或 markdown；每一則都必須回覆、id 照抄輸入編號：\n"
        '[{"id":1,"verdict":"ok"},{"id":2,"verdict":"fix","title":"…","body":"…","reason":"…"},'
        '{"id":3,"verdict":"drop","reason":"…"}]\n\n'
    )

def _fact_check_entry(n, item, mat):
    lines = [f"#{n}", "TITLE: " + (item.get('title') or ''), "BODY: " + (item.get('body') or '')]
    ho = bool(item.get('headline_only') or (mat or {}).get('headline_only'))
    do = bool(not ho and (item.get('desc_only') or (mat or {}).get('desc_only')))
    # 只有標題／只有描述的卡：impact／insight／chain 由程式一律清空（scrub_*），查核只看 title 與 body
    if not ho and not do:
        for f in ('impact', 'insight'):
            v = item.get(f) or ''
            if isinstance(v, str) and v.strip() and re.search(r'\d', v):
                lines.append(f.upper() + ": " + v)
    if mat is None:
        lines.append("SOURCE: （找不到原文素材）")
    else:
        if mat.get('headline_only'):
            label = "SOURCE（只有標題）"
        elif do:
            label = "SOURCE（只有 RSS 描述）"   # 11-17 B：標題＋描述是唯一素材
        else:
            label = "SOURCE"
        lines.append(label + ": " + (mat.get('text') or '')[:FACT_CHECK_MATERIAL_CHARS])
    return '\n'.join(lines) + '\n\n'

def _call_groq_fact_check(prompt):
    """同模型、同 fallback（413 改用 20b）；回傳 (解析後的 list, usage dict)。"""
    from groq import APIStatusError as GroqAPIStatusError
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    response = None
    for model in models:
        try:
            _groq_wait_window()
            response = client.chat.completions.create(
                model=model,
                reasoning_effort=FACT_CHECK_REASONING,
                messages=[
                    {"role": "system", "content": "只輸出純JSON陣列，不加任何說明或markdown。全程繁體中文。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=FACT_CHECK_MAX_TOKENS,
            )
            _groq_mark(response)
            break
        except GroqAPIStatusError as e:
            _groq_mark(None)
            if e.status_code == 413 and model != models[-1]:
                print(f"  → {model} 超出 TPM，查核改用備用模型…")
                continue
            raise
    raw = (response.choices[0].message.content or '').strip()
    if response.choices[0].finish_reason == 'length':
        raise ValueError(f"查核回應被截斷（finish_reason=length，{len(raw)} 字）")
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
    return parsed, _GROQ_STATE.get('last_usage')

def _fact_check_batches(entries, header_tokens):
    """依 token 預算分批：每批 header＋各則 ≤ FACT_CHECK_TOKEN_BUDGET − FACT_CHECK_MAX_TOKENS。
    單則就超過預算者回傳在 skipped。entries：[(n, item, text)]。"""
    limit = FACT_CHECK_TOKEN_BUDGET - FACT_CHECK_MAX_TOKENS
    batches, cur, cur_tok, skipped = [], [], header_tokens, []
    for e in entries:
        tk = estimate_tokens(e[2])
        if header_tokens + tk > limit:
            skipped.append(e)
            continue
        if cur and cur_tok + tk > limit:
            batches.append((cur, cur_tok))
            cur, cur_tok = [], header_tokens
        cur.append(e)
        cur_tok += tk
    if cur:
        batches.append((cur, cur_tok))
    return batches, skipped

def apply_fact_check_results(batch, results, stats):
    """batch：[(n, item, text)]，n 為批內編號 1..k；results：LLM 回傳 list。以編號對應，回傳要移除的 item 清單。
    漏答者保留原樣並計入 stats['missing']；無效編號略過。容錯：批內編號一個都對不上、但回傳筆數
    與批內則數相同時（例如 LLM 自行改用別的編號起點），依回傳順序對應並計入 stats['positional']。
    fix 只回 body 時保留原標題。修正過的卡片記入 _FACT_CHECK_FIXED，供之後重跑既有文字閘門。"""
    by_id = {}
    dict_results = [r for r in (results or []) if isinstance(r, dict)]
    for r in dict_results:
        rid = r.get('id')
        if isinstance(rid, str) and rid.strip().lstrip('#').isdigit():
            rid = int(rid.strip().lstrip('#'))
        if isinstance(rid, int) and not isinstance(rid, bool):
            by_id.setdefault(rid, r)
    batch_ids = [n for n, _, _ in batch]
    if not any(n in by_id for n in batch_ids) and len(dict_results) == len(batch) and batch:
        by_id = {n: r for n, r in zip(batch_ids, dict_results)}
        stats['positional'] = stats.get('positional', 0) + 1
    removals = []
    for n, item, _ in batch:
        r = by_id.get(n)
        verdict = (r or {}).get('verdict')
        verdict = verdict.strip().lower() if isinstance(verdict, str) else ''
        title = (item.get('title') or '')[:60]
        rec = {'title': title, 'verdict': verdict or 'missing'}
        if r is None or verdict not in ('ok', 'fix', 'drop'):
            stats['missing'] += 1
            rec['verdict'] = 'missing'
        elif verdict == 'ok':
            stats['ok'] += 1
        elif verdict == 'drop':
            stats['dropped'] += 1
            removals.append(item)
            rec['reason'] = str(r.get('reason') or '')[:120]
            _gate_reason_set('llm_fact_check', item.get('title'), 'drop：' + rec['reason'])
        else:
            # 11-16：1116c 試跑兩則 fix 實際沒改到內容 → 記下 LLM 回傳的新欄位，方便追查
            rec['returned'] = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))[:200]
                               for k, v in r.items() if k in ('title', 'body', 'impact', 'insight', 'chain')}
            nt, nb = r.get('title'), r.get('body')
            if not (isinstance(nt, str) and nt.strip()):
                nt = item.get('title') or ''
            unchanged = (isinstance(nb, str) and nb.strip() == (item.get('body') or '').strip()
                         and nt.strip() == (item.get('title') or '').strip()
                         and not any(isinstance(r.get(f), (str, list)) and r.get(f) != item.get(f)
                                     for f in ('impact', 'insight', 'chain') if f in r))
            if not (isinstance(nb, str) and nb.strip()) or unchanged:
                # fix 卻沒給修正內容：只有標題的卡改用標題直譯句＋決定性清理；一般卡降為 opp
                stats['fix_no_content'] = stats.get('fix_no_content', 0) + 1
                rec['verdict'] = 'fix_no_content'
                rec['reason'] = str(r.get('reason') or '')[:120]
                if item.get('headline_only'):
                    item['body'] = headline_sentence(item)
                    scrub_headline_only_item(item)
                    rec['action'] = 'headline_scrub'
                elif item.get('desc_only'):
                    # 11-17 B：程式無法自行翻譯描述，退回標題直譯一句（標題即描述素材的一部分）＋決定性清理
                    item['body'] = headline_sentence(item)
                    scrub_desc_only_item(item)
                    rec['action'] = 'desc_scrub'
                elif item.get('rating') == 'core':
                    item['rating'] = 'opp'
                    rec['action'] = 'core_to_opp'
                _gate_reason_set('llm_fact_check', item.get('title'), 'fix 無修正內容：' + rec['reason'])
            else:
                item['title'] = nt.strip()
                item['body'] = nb.strip()
                _FACT_CHECK_FIXED.append(item)
                for f in ('impact', 'insight'):
                    if isinstance(r.get(f), str) and f in item and (r.get(f).strip() or item.get('headline_only')):
                        item[f] = r[f].strip()
                ch = r.get('chain')
                if isinstance(ch, list) and 'chain' in item and all(
                        isinstance(n_, dict) and isinstance(n_.get('label'), str) and n_['label'].strip()
                        and n_.get('type') in ('up', 'down', 'warn', 'neutral') for n_ in ch):
                    item['chain'] = ch
                if item.get('headline_only'):
                    scrub_headline_only_item(item)
                elif item.get('desc_only'):
                    scrub_desc_only_item(item)
                stats['fixed'] += 1
                rec['reason'] = str(r.get('reason') or '')[:120]
                rec['new_title'] = item['title'][:60]
        if len(stats['items']) < 30 and rec['verdict'] != 'ok':
            stats['items'].append(rec)
    return removals

def llm_fact_check(data, news_by_cat):
    """LLM 查核關卡：每分類一次（必要時分批）送「卡片 title/body＋原文素材」，逐則回 ok／fix／drop。
    呼叫前 _groq_wait_window 維持 65 秒間隔；任何失敗都保留原卡、不影響當日輸出。"""
    stats = {'enabled': LLM_FACT_CHECK, 'calls': 0, 'failed_calls': 0, 'checked': 0, 'ok': 0, 'fixed': 0,
             'dropped': 0, 'missing': 0, 'skipped_over_budget': 0, 'est_prompt_tokens': [], 'usage': [],
             'items': []}
    try:
        RUN_STATS['llm_fact_check'] = stats
    except Exception:
        pass
    if not LLM_FACT_CHECK:
        return stats
    idx = _fa_index_materials(news_by_cat)
    header = _fact_check_header()
    header_tok = estimate_tokens(header)
    for sec in ('hw', 'corp', 'app'):
        items = [it for it in data.get(sec, []) or [] if isinstance(it, dict) and not _is_placeholder(it)]
        if not items:
            continue
        # 11-17 B：desc_only／headline_only 卡必送查核——排在最前面，分批時優先進第一批
        items.sort(key=lambda it: 0 if (it.get('desc_only') or it.get('headline_only')) else 1)
        stats['desc_only_sent'] = stats.get('desc_only_sent', 0) + sum(1 for it in items if it.get('desc_only'))
        entries = [(n, it, _fact_check_entry(n, it, _material_for(it, idx))) for n, it in enumerate(items, 1)]
        batches, skipped = _fact_check_batches(entries, header_tok)
        stats['skipped_over_budget'] += len(skipped)
        removals = []
        for batch, est in batches:
            # 每批重新從 1 編號（批內編號），避免 LLM 自行從 1 起算時整批對不上
            batch = [(i, it, _fact_check_entry(i, it, _material_for(it, idx))) for i, (_, it, _) in enumerate(batch, 1)]
            prompt = header + ''.join(e[2] for e in batch)
            stats['est_prompt_tokens'].append(est)
            try:
                print(f"  → 查核 {sec}：{len(batch)} 則（估 {est}＋{FACT_CHECK_MAX_TOKENS} tokens）")
                results, usage = _call_groq_fact_check(prompt)
                stats['calls'] += 1
                stats['usage'].append(usage)
                stats['checked'] += len(batch)
                removals.extend(apply_fact_check_results(batch, results, stats))
            except Exception as e:
                stats['calls'] += 1
                stats['failed_calls'] += 1
                print(f"  ⚠ 查核 {sec} 失敗，保留原卡（不影響資料生成）：{str(e)[:120]}")
        if removals:
            data[sec] = [it for it in data.get(sec, []) if not any(it is r for r in removals)]
            if not data[sec]:
                data[sec] = [make_placeholder_item(sec)]
    print(f"  → LLM 查核：檢查 {stats['checked']} 則，ok {stats['ok']}、修正 {stats['fixed']}、"
          f"移除 {stats['dropped']}、漏答 {stats['missing']}、超出預算略過 {stats['skipped_over_budget']}、"
          f"呼叫失敗 {stats['failed_calls']}")
    return stats

_FACT_CHECK_FIXED = []

def recheck_fixed_items(data):
    """LLM 查核 fix 過的卡片再過既有文字閘門（禁句、幻覺 pattern、幻覺數字、低品質），只處理被修正的卡。"""
    fixed_ids = {id(it) for it in _FACT_CHECK_FIXED}
    sub = {sec: [it for it in data.get(sec, []) or [] if isinstance(it, dict) and id(it) in fixed_ids]
           for sec in ('hw', 'corp', 'app')}
    if not any(sub.values()):
        return
    for fn in (downgrade_forbidden_phrases, downgrade_hallucination_patterns, downgrade_hallucinated,
               downgrade_low_quality):
        try:
            fn(sub)
        except Exception as e:
            print(f"  ⚠ 查核後重跑 {fn.__name__} 失敗（略過）：{e}")

# ── 週報 ──
def normalize_weekly_part(part):
    """LLM 雙重跳脫留下的字面反斜線 n（\\n 兩字元）轉成真換行；去掉空行與行首尾空白。"""
    if not isinstance(part, str):
        return ''
    t = re.sub(r'\\+n', '\n', part)
    return '\n'.join(l.strip() for l in t.split('\n') if l.strip())

def check_weekly_numbers(data, news_by_cat):
    """週報每一句的數字須在今日最終條目（title/body/impact/insight）或素材原文找得到；
    找不到的句子刪除，刪到只剩前綴（HW：等）的行整行刪除。"""
    ws = data.get('weekly_summary')
    if not ws or not isinstance(ws, str):
        return None
    ref_parts = []
    for sec in ('hw', 'corp', 'app'):
        for it in data.get(sec, []) or []:
            if isinstance(it, dict) and not _is_placeholder(it):
                ref_parts.extend(str(it.get(f) or '') for f in ('title', 'body', 'impact', 'insight'))
    ref_parts.extend(m['text'] for m in _fa_index_materials(news_by_cat).values())
    ref = '\n'.join(ref_parts)
    removed = []
    out_lines = []
    for line in ws.split('\n'):
        pm = re.match(r'^\s*([^：:]{1,8}[：:])\s*', line)
        prefix = pm.group(1) if pm else ''
        content = line[pm.end():] if pm else line
        kept, dropped = _ng_drop_unsupported_sentences(content, ref)
        if dropped:
            removed.extend(dropped)
        if kept.strip():
            out_lines.append(prefix + kept)
    data['weekly_summary'] = '\n'.join(out_lines) or None
    try:
        RUN_STATS['weekly_numeric'] = {'removed_numbers': removed[:20]}
    except Exception:
        pass
    if removed:
        print(f"  ✂ 週報刪除數字無出處的句子：{removed[:5]}")
    return removed
# ══════════════════════════════════════════════════════════════════
#  5. HISTORY
# ══════════════════════════════════════════════════════════════════
DATA_PATH = 'data/history.json'

def _glossary_key(term):
    """詞彙比對 key：去頭尾空白、小寫、取括號前主詞（HBM (High Bandwidth Memory) -> hbm）。"""
    t = (term or '').strip()
    t = re.split(r'[(（]', t, maxsplit=1)[0]
    return t.strip().lower()

def _known_glossary_keys(history, html_path=None, exclude_date=None):
    """蒐集既有詞：html 靜態詞彙表 term:"..." ＋ history 各天 glossary_new。讀檔失敗一律 fallback 成不排除。"""
    keys = set()
    try:
        if html_path is None:
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ai_tracker_v6.html')
        with open(html_path, encoding='utf-8') as f:
            for m in re.finditer(r'\bterm:"([^"]+)"', f.read()):
                k = _glossary_key(m.group(1))
                if k:
                    keys.add(k)
    except Exception as e:
        print(f"  ⚠ 讀取靜態詞彙表失敗，略過此排除來源：{e}")
    try:
        for day in (history or []):
            if exclude_date and day.get('date') == exclude_date:
                continue  # 同日重跑時不排除自己上次寫入的詞
            for g in (day.get('glossary_new') or []):
                k = _glossary_key(g.get('term') if isinstance(g, dict) else '')
                if k:
                    keys.add(k)
    except Exception as e:
        print(f"  ⚠ 讀取 history 詞彙失敗，略過此排除來源：{e}")
    return keys

def filter_new_glossary(items, history, html_path=None, exclude_date=None):
    """同批依 term 去重，並排除已收錄的詞。任何例外都 fallback 成只做同批去重。"""
    try:
        known = _known_glossary_keys(history, html_path, exclude_date)
    except Exception as e:
        print(f"  ⚠ 詞彙排除清單建立失敗，fallback 不排除：{e}")
        known = set()
    seen, out = set(), []
    for g in (items or []):
        k = _glossary_key(g.get('term') if isinstance(g, dict) else '')
        if not k or k in seen or k in known:
            continue
        seen.add(k)
        out.append(g)
    return out

def load_history():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, encoding='utf-8') as f:
            return json.load(f)
    return []

def save_history(history):
    if DRY_RUN:
        print("  → DRY_RUN：不寫 data/history.json")
        return
    history.sort(key=lambda x: x['date'], reverse=True)
    history = history[:7]
    os.makedirs('data', exist_ok=True)
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return history

def upsert(history, data):
    idx = next((i for i,h in enumerate(history) if h['date']==data['date']), None)
    if idx is not None: history[idx] = data
    else: history.append(data)
    return history

# ══════════════════════════════════════════════════════════════════
#  5. EMAIL（5/5 content quality）
# ══════════════════════════════════════════════════════════════════
import html as _html_mod


def _esc(x):
    """信件HTML插值用：外部來源／LLM文字一律escape。"""
    return _html_mod.escape(str(x if x is not None else ''), quote=True)


def _safe_url(u):
    """href只准http(s)://開頭，其餘回傳空字串（呼叫端改為只顯示文字）。"""
    u = str(u or '').strip()
    return u if re.match(r'^https?://', u, re.I) else ''

def send_email(data):
    if DRY_RUN:
        print("  → DRY_RUN：不寄信")
        return
    user = os.environ.get('GMAIL_USER','').replace('\xa0','').replace(' ','').strip()
    pwd  = os.environ.get('GMAIL_APP_PASSWORD','').replace('\xa0','').replace(' ','').strip()

    # --pigeon-only：跳過 GitHub Secret 收件人，只送飛鴿名單
    if PIGEON_ONLY:
        secret_recipients = []
    else:
        secret_to = os.environ.get('NOTIFY_EMAIL', user).replace('\xa0','').replace(' ','').strip()
        secret_recipients = [a.strip() for a in secret_to.split(',') if a.strip()]

    # Extra recipients from email_config.json (exclude those already in secret)
    extra_recipients = []
    cfg_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'email_config.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if cfg.get('enabled', True):
                extra_recipients = [r.strip() for r in cfg.get('recipients', [])
                                if r.strip() and r.strip() not in secret_recipients]
            else:
                if PIGEON_ONLY:
                    print("  → 飛鴿推送已暫停（enabled=false），略過。"); return
                print("  → 公開收件人推送已暫停（enabled=false），僅發送 secret 收件人")
        except Exception:
            pass

    if not user or not pwd or (not secret_recipients and not extra_recipients):
        print("  → Email 未設定，略過。"); return

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    def chain_text(chain):
        return ' → '.join(_esc(c['label']) for c in chain)

    def impact_box(item):
        # 11-16：headline_only 卡 impact 與 chain 皆清空，空值時不畫空框
        inner = _esc(item["impact"]) if item.get("impact") else chain_text(item.get("chain") or [])
        if not inner:
            return ''
        return ('<div style="background:#f0ede9;border-radius:5px;padding:8px 12px;font-size:12px;color:#6a6460;">'
                '<span style="font-size:10px;text-transform:uppercase;letter-spacing:0.6px;color:#9e9890;'
                'display:block;margin-bottom:4px;">供應鏈影響分析</span>' + inner + '</div>')

    def rating_badge(r):
        return {'core':'🔵 CORE','noise':'⚫ NOISE','opp':'🟢 OPP'}.get(r,'')

    def section_html(items, color, emoji, label):
        if not items: return ''
        cards = ''
        for item in items:
            eth = f'<div style="background:#fff8f0;border-left:3px solid #c07030;padding:8px 12px;margin:8px 0;font-size:12px;color:#805020;">⚠️ {_esc(item["ethic"])}</div>' if item.get('ethic') else ''
            src_url = _safe_url(item.get('source'))
            src_lbl = _esc(item.get("source_label") or "來源連結")
            if src_url:
                src = f'<div style="margin-top:6px;font-size:11px;"><a href="{_esc(src_url)}" style="color:{color};">{src_lbl} →</a></div>'
            elif item.get('source'):
                src = f'<div style="margin-top:6px;font-size:11px;color:#9e9890;">{src_lbl}</div>'
            else:
                src = ''
            if item.get('thin_source'):
                thin = '<span style="color:#9e9890;"> · 僅摘要</span>'
                if src.endswith('</a></div>'):
                    src = src[:-len('</div>')] + thin + '</div>'
                elif src.endswith('</div>'):
                    src = src[:-len('</div>')] + thin + '</div>'
                else:
                    src = f'<div style="margin-top:6px;font-size:11px;color:#9e9890;">僅摘要</div>'
            cards += f'''
            <div style="background:#faf9f7;border-left:3px solid {color};padding:14px 16px;margin:10px 0;border-radius:0 6px 6px 0;">
              <div style="display:flex;align-items:center;margin-bottom:6px;">
                <span style="font-size:11px;font-weight:700;color:{color};background:{color}18;padding:2px 8px;border-radius:10px;border:1px solid {color}44;margin-right:8px;">{_esc(item["layer"])}</span>
                <span style="font-size:11px;color:#888;">{rating_badge(item.get("rating",""))}</span>
              </div>
              <div style="font-size:14px;font-weight:700;color:#2c2a28;margin-bottom:8px;line-height:1.4;">{_esc(item["title"])}</div>
              <div style="font-size:13px;color:#4a4744;line-height:1.65;margin-bottom:10px;">{_esc(item["body"])}</div>
              {impact_box(item)}
              {eth}
              {f'<div style="margin-top:10px;font-size:12.5px;color:#3a6860;background:#f0f8f6;padding:8px 12px;border-radius:5px;border-left:2px solid #4a8a6a;">注意方向：{_esc(item["insight"])}</div>' if item.get("insight") else ''}
              {src}
            </div>'''
        return f'''
        <div style="margin-bottom:24px;">
          <h3 style="font-size:14px;font-weight:700;color:{color};margin:0 0 10px 0;padding-bottom:6px;border-bottom:2px solid {color}33;">
            {emoji} {label}
          </h3>
          {cards}
        </div>'''

    def _fmt_weekly_html(text):
        rows = []
        for ln in text.split('\n'):
            ln = ln.strip()
            if not ln: continue
            if '：' in ln:
                sep = ln.index('：')
                lbl, body = ln[:sep], ln[sep+1:]
                rows.append(f'<div style="margin:5px 0;padding:3px 0 3px 10px;border-left:2px solid #d4b060;"><strong style="font-weight:700;color:#8a6030;">{_esc(lbl)}：</strong>{_esc(body)}</div>')
            else:
                rows.append(f'<div style="margin:5px 0;padding:3px 0 3px 10px;border-left:2px solid #d4b060;">{_esc(ln)}</div>')
        return ''.join(rows)

    weekly = ''
    if data.get('weekly_summary'):
        ws_html = _fmt_weekly_html(data['weekly_summary'])
        weekly = f'''
        <div style="background:#fdf8f0;border:1px solid #d4b060;border-radius:8px;padding:16px 20px;margin:20px 0;">
          <div style="font-size:14px;font-weight:700;color:#a07040;margin-bottom:10px;">📊 本週摘要</div>
          <div style="font-size:13px;color:#4a4744;line-height:1.7;">{ws_html}</div>
        </div>'''

    # 08-11：季底最後一天（3/31、6/30、9/30、12/31）在新聞上方加一段財報回顧banner，
    # 資料來自data/hw_corp_reference.json——跟網頁HW/Corp面板同一份JSON單一來源，
    # 不會有兩邊數字不同步的問題。banner一律誠實標示ref裡的last_updated季別，
    # 不會假裝是「今天剛公布」的資料（本季財報通常季底後幾週才會公布，季底當天
    # 能用的一定是上一季或更早的數字），這就是使用者要的「先依財報更新完成後再納入」
    # ——banner只反映JSON目前實際被更新到的季別，沒更新就照實顯示舊季別，不是條件式隱藏
    qbanner = ''
    if (NOW.month, NOW.day) in [(3,31),(6,30),(9,30),(12,31)]:
        ref_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'hw_corp_reference.json')
        try:
            with open(ref_path, 'r', encoding='utf-8') as f:
                ref = json.load(f)

            def _ref_card(c):
                return f'''<div style="background:#faf9f7;border:1px solid #d8d4ce;border-radius:8px;padding:10px 14px;margin-bottom:8px;">
                  <div style="font-size:11px;color:#7a756f;text-transform:uppercase;letter-spacing:.5px;">{_esc(c["label"])}</div>
                  <div style="font-size:16px;font-weight:700;color:#2c2a28;margin:2px 0;">{_esc(c["value"])}</div>
                  <div style="font-size:11.5px;color:#7a756f;line-height:1.5;">{_esc(c["sub"])}</div>
                </div>'''

            hw_cards = ''.join(_ref_card(c) for c in (ref.get('hw',{}).get('bottleneck_cards') or []) + (ref.get('hw',{}).get('vendor_cards') or []))
            corp_cards = ''.join(_ref_card(c) for c in (ref.get('corp',{}).get('metric_cards') or []))
            qbanner = f'''
            <div style="background:#f0f5f8;border:1px solid #b8c8d8;border-radius:8px;padding:16px 20px;margin:20px 0;">
              <div style="font-size:14px;font-weight:700;color:#3a5a7a;margin-bottom:4px;">📊 季底財報回顧（{_esc(ref.get("last_updated","—"))}）</div>
              <div style="font-size:11px;color:#9e9890;margin-bottom:12px;">本季最後一天彙整目前掌握的最新財報數字，僅供快速回顧；本季（今日）財報通常尚未公布，上方季別為實際最後更新的資料來源</div>
              <div style="font-size:12px;font-weight:700;color:#5a7fa8;margin:10px 0 6px;">🔩 硬體缺口</div>
              {hw_cards}
              <div style="font-size:12px;font-weight:700;color:#a07040;margin:14px 0 6px;">💰 巨頭角力</div>
              {corp_cards}
            </div>'''
        except Exception as e:
            print(f"  ⚠ 季底banner讀取hw_corp_reference.json失敗（不影響信件其餘內容）：{e}")

    html = f'''<html><body style="font-family:'Segoe UI',sans-serif;max-width:620px;margin:auto;padding:0;background:#eceae6;color:#2c2a28;">
      <div style="background:#faf9f7;padding:24px 28px;">

        <!-- Header -->
        <div style="border-bottom:1px solid #d8d4ce;padding-bottom:16px;margin-bottom:20px;">
          <div style="font-size:20px;font-weight:800;color:#2c2a28;">📡 AI 產業動態</div>
          <div style="font-size:13px;color:#9e9890;margin-top:4px;">{DATE_STR}{"（週報）" if data.get("is_sunday") else ""} &nbsp;·&nbsp; 自動更新</div>
        </div>

        {weekly}
        {qbanner}
        {section_html(data["hw"], "#5a7fa8", "🔩", "硬體缺口")}
        {section_html(data["corp"],"#a07040", "💰", "巨頭角力")}
        {section_html(data["app"], "#4a8a6a", "🤖", "新興應用")}

        <!-- Footer -->
        <div style="border-top:1px solid #d8d4ce;padding-top:16px;margin-top:8px;text-align:center;">
          {{footer_extra}}
          <div style="font-size:11px;color:#b0b0b0;margin-top:12px;">AI Tracker · 自動產生 · {DATE_STR}</div>
        </div>

      </div>
    </body></html>'''

    footer_with = '<a href="https://resolutetinging.github.io/aitracker/ai_tracker_v6.html" style="display:inline-block;background:#5a7fa8;color:#fff;padding:10px 24px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;">🔗 查看完整 Dashboard →</a>'

    subject = f'📡 AI Tracker {DATE_STR}{"（週報）" if data.get("is_sunday") else ""}'

    def do_send(recipients, include_dashboard):
        if not recipients: return
        body = html.replace('{footer_extra}', footer_with if include_dashboard else '')
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = user
        msg['To'] = ','.join(recipients)
        msg.attach(MIMEText(body, 'html', 'utf-8'))
        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
                s.login(user, pwd)
                s.send_message(msg)
            print(f"  → Email 已發送至 {', '.join(recipients)}")
        except Exception as e:
            print(f"  → Email 失敗：{e}")
    do_send(secret_recipients, include_dashboard=True)
    do_send(extra_recipients, include_dashboard=False)

# ══════════════════════════════════════════════════════════════════
#  6. NOTION
# ══════════════════════════════════════════════════════════════════
def push_notion(data):
    if DRY_RUN:
        print("  → DRY_RUN：不推送 Notion")
        return
    token = os.environ.get('NOTION_TOKEN')
    db_id = os.environ.get('NOTION_DB_ID')
    if not token or not db_id:
        print("  → Notion 未設定，略過。"); return

    def para(text):
        return {"object":"block","type":"paragraph",
                "paragraph":{"rich_text":[{"type":"text","text":{"content":text[:2000]}}]}}
    def h2(text):
        return {"object":"block","type":"heading_2",
                "heading_2":{"rich_text":[{"type":"text","text":{"content":text}}]}}

    def item_block(item):
        impact = item.get('impact') or ' → '.join(c['label'] for c in item.get('chain') or [])
        text = f"▸ {item['title']}\n{item['body']}" + (f"\n供應鏈影響：{impact}" if impact else "")
        if item.get('insight'):
            text += f"\n注意方向：{item['insight']}"
        return para(text)

    blocks = [
        h2("🔩 硬體缺口"),
        *[item_block(i) for i in data['hw']],
        h2("💰 巨頭角力"),
        *[item_block(i) for i in data['corp']],
        h2("🤖 新興應用"),
        *[item_block(i) for i in data['app']],
    ]
    if data.get('weekly_summary'):
        def bullet(text):
            return {"object":"block","type":"bulleted_list_item",
                    "bulleted_list_item":{"rich_text":[{"type":"text","text":{"content":text[:2000]}}]}}
        ws_lines = [l.strip() for l in data['weekly_summary'].split('\n') if l.strip()]
        blocks += [h2("📊 本週摘要")] + [bullet(l) for l in ws_lines]

    payload = {
        "parent":{"database_id":db_id},
        "properties":{
            "Name":{"title":[{"text":{"content":f'📡 AI 動態 {DATE_STR}{"（週報）" if data.get("is_sunday") else ""}'}}]},
            "Date":{"date":{"start":DATE_STR}}
        },
        "children":blocks
    }
    try:
        req = urllib.request.Request("https://api.notion.com/v1/pages",
            data=json.dumps(payload).encode(),
            headers={"Authorization":f"Bearer {token}","Content-Type":"application/json","Notion-Version":"2022-06-28"},
            method="POST")
        with urllib.request.urlopen(req) as res:
            print(f"  → Notion 頁面已建立：{json.loads(res.read())['id']}")
    except Exception as e:
        print(f"  → Notion 錯誤：{e}")

def write_dry_run_result(data, news_by_cat=None, note=''):
    """DRY_RUN：把當日 data、RUN_STATS、每則條目的原文素材寫到 DRY_RUN_DIR/result_YYYYMMDD_HHMM.json。
    失敗只印警告。回傳寫入路徑或 None。"""
    try:
        idx = _fa_index_materials(news_by_cat or {})
        per_item = []
        for sec in ('hw', 'corp', 'app'):
            for it in (data or {}).get(sec, []) or []:
                if not isinstance(it, dict):
                    continue
                mat = _material_for(it, idx)
                per_item.append({'section': sec, 'title': it.get('title'), 'rating': it.get('rating'),
                                 'source': it.get('source'), 'headline_only': bool(it.get('headline_only')),
                                 'desc_only': bool(it.get('desc_only')), 'material': mat})
        out = {'note': note, 'generated_at': datetime.now(TW).isoformat(), 'data': data,
               'run_stats': RUN_STATS, 'item_materials': per_item, 'news_by_cat': news_by_cat}
        os.makedirs(DRY_RUN_DIR, exist_ok=True)
        path = os.path.join(DRY_RUN_DIR, 'result_' + datetime.now(TW).strftime('%Y%m%d_%H%M') + '.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"  → DRY_RUN 結果已寫入 {path}")
        return path
    except Exception as e:
        print(f"  ⚠ DRY_RUN 結果寫入失敗：{e}")
        return None

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print(f"🚀 開始更新 AI Tracker（{DATE_STR}）...")

    print("📋 筆記 Gist 快照備份...")
    snapshot_notes_backup()

    history = load_history()

    # ── 冪等保護：今日已有高品質資料，直接沿用（加 --force 強制重跑）──
    existing_today = next((h for h in history if h.get('date') == DATE_STR), None)
    if existing_today and not FORCE_REGEN and not DRY_RUN:
        all_items = existing_today.get('hw',[]) + existing_today.get('corp',[]) + existing_today.get('app',[])
        core_count = sum(1 for i in all_items if i.get('rating') == 'core')
        if core_count >= 2:
            print(f"  → 今日已有資料（{core_count} CORE），直接沿用既有內容寄送")
            print(f"  → 如需強制重新生成請加 --force 參數")
            print("📧 發送 Email（沿用）...")
            send_email(existing_today)
            print("📝 推送 Notion（沿用）...")
            push_notion(existing_today)
            print("✅ 完成！")
            sys.exit(0)

    recent_titles = get_recent_titles(history, days=3)
    recent_urls = get_recent_urls(history, days=3)
    recent_items = get_recent_items(history, days=3)
    print(f"  → 近三日標題 {len(recent_titles)} 條、來源網址 {len(recent_urls)} 條（NO-REPEAT 用）")

    print("📰 抓取新聞（含近日預過濾、分類分流）...")
    news_by_cat = fetch_news(recent_titles, recent_urls)  # {'hw':str,'corp':str,'app':str}
    news_combined = "\n\n".join(news_by_cat[c] for c in ['hw', 'corp', 'app'])
    total = len(news_combined.splitlines())
    print(f"  → 合計 {total} 行新聞摘要")

    hs_count = count_high_signal(news_combined)
    print(f"  → 高信號素材 {hs_count} 條（門檻：3）")
    if hs_count < 3 and not FORCE_REGEN:
        print("  → 素材不足，跳過 LLM 生成，存為空日（零捏造模式）")
        data = make_empty_day()
        history = upsert(history, data)
        save_history(history)
        print(f"  → data/history.json 已更新（空日）")
        try:
            RUN_STATS['llm_items'] = {'hw': 0, 'corp': 0, 'app': 0}
            RUN_STATS['llm_failed'] = {'hw': False, 'corp': False, 'app': False}
            RUN_STATS['llm_ratings'] = {c: {'core': 0, 'opp': 0, 'noise': 0} for c in ('hw', 'corp', 'app')}
            RUN_STATS['final'] = {c: {'core': 0, 'opp': 0, 'noise': 0} for c in ('hw', 'corp', 'app')}
            RUN_STATS['placeholders'] = 0
            save_run_stats(RUN_STATS)
            print("  → data/run_stats.json 已更新（空日）")
        except Exception as e:
            print(f"  ⚠ run_stats 寫入失敗（不影響主流程）：{e}")
        if DRY_RUN:
            write_dry_run_result(data, news_by_cat, note='empty_day')
        print("✅ 完成（空日）")
        sys.exit(0)

    # ── 一分類一次 LLM 呼叫，避開 Groq 免費額度 6000 TPM/分鐘 限流 ──
    # 每次呼叫間隔 65 秒；單一分類失敗（例外/限流）時 fallback 為空清單，
    # 不影響其他分類、不整體失敗（會自然走佔位卡邏輯）。
    print("🤖 呼叫 Groq API（hw→corp→app，一分類一次）...")
    data = {
        'date': DATE_STR, 'is_sunday': IS_SUNDAY,
        'hw': [], 'corp': [], 'app': [],
        'glossary_new': [], 'weekly_summary': None,
    }
    weekly_parts = []
    categories = ['hw', 'corp', 'app']
    for i, cat in enumerate(categories):
        try:
            result = call_groq(make_prompt(news_by_cat[cat], cat, recent_titles, avoid_items=data.get('hw')))
            result, _retry_st = retry_bucket_if_empty(cat, result, news_by_cat[cat], recent_titles,
                                                      avoid_items=data.get('hw'))
            RUN_STATS.setdefault('retry', {})[cat] = _retry_st
            data[cat] = result.get(cat, [])
            data['glossary_new'].extend(result.get('glossary_new') or [])
            if IS_SUNDAY and result.get('weekly_summary'):
                # 11-16：LLM 雙重跳脫的字面 \\n 轉成真換行
                _wp = normalize_weekly_part(result['weekly_summary'])
                if _wp:
                    weekly_parts.append(_wp)
            print(f"  → {cat} 完成，{len(data[cat])} 則")
            try:
                # 原始回傳數：在任何閘門之前記（含 LLM 自己給的佔位卡）
                RUN_STATS['llm_items'][cat] = len(data[cat])
                RUN_STATS['llm_failed'][cat] = False
                RUN_STATS.setdefault('llm_usage', {})[cat] = _GROQ_STATE.get('last_usage')
            except Exception as e:
                print(f"  ⚠ 漏斗統計收集失敗（LLM/{cat}，不影響資料生成）：{e}")
        except Exception as e:
            print(f"  ⚠ {cat} 分類呼叫失敗，fallback 為空清單（不影響其他分類）：{e}")
            data[cat] = []
            try:
                RUN_STATS['llm_items'][cat] = 0
                RUN_STATS['llm_failed'][cat] = True
            except Exception as e2:
                print(f"  ⚠ 漏斗統計收集失敗（LLM/{cat}，不影響資料生成）：{e2}")
        if i < len(categories) - 1:
            print("  → 避開 TPM 限流，休息 65 秒...")
            time.sleep(65)

    if IS_SUNDAY and weekly_parts:
        data['weekly_summary'] = '\n'.join(weekly_parts)

    # glossary_new 三次呼叫各自產生，依 term 去重（大小寫不分）；
    # 另排除 html 靜態詞彙表與 history 既有 glossary_new 已收錄的詞（事後過濾，不動 prompt）
    data['glossary_new'] = filter_new_glossary(data['glossary_new'], history, exclude_date=data.get('date'))

    print(f"  → 硬體 {len(data.get('hw',[]))} / 巨頭 {len(data.get('corp',[]))} / 應用 {len(data.get('app',[]))} 則")

    # 閘門管線執行前先採集 LLM 剛生成時的評級分佈，與稍後的 final 對照，
    # 用來分辨「模型自己判冷」vs「被閘門翻冷」（凍結期調參歸因用）
    try:
        llm_ratings = {}
        for section in ['hw', 'corp', 'app']:
            tally = {'core': 0, 'opp': 0, 'noise': 0}
            for item in data.get(section, []):
                rating = item.get('rating')
                if rating in tally:
                    tally[rating] += 1
            llm_ratings[section] = tally
        RUN_STATS['llm_ratings'] = llm_ratings
    except Exception as e:
        print(f"  ⚠ 漏斗統計收集失敗（llm_ratings，不影響資料生成）：{e}")

    print("🏷 HEADLINE_ONLY 卡片（core 上限 opp）...")
    try:
        _run_gate('enforce_headline_only', enforce_headline_only, data, news_by_cat)
    except Exception as e:
        print(f"  ⚠ HEADLINE_ONLY 標記失敗（略過，不影響資料生成）：{e}")
    # validate_sources 會清空失效網址；先記下原 source，數字閘門／查核仍能找回原素材
    try:
        _MATERIAL_URLS.update(_fa_index_materials(news_by_cat).keys())
    except Exception as e:
        print(f"  ⚠ 素材網址集合建立失敗（不影響資料生成）：{e}")
    _ORIG_SOURCES.update({id(it): (it.get('source') if isinstance(it.get('source'), str) else '')
                          for sec in ('hw', 'corp', 'app')
                          for it in data.get(sec, []) if isinstance(it, dict)})
    print("🔗 驗證 source URL...")
    _run_gate('validate_sources', validate_sources, data)
    # 放在 validate_sources 之後：來源已失效（source 被清空）的卡不白抓、也不會只剩一行「僅摘要」
    if DEEPEN_THIN_CARDS:
        print("📖 補深薄卡（補抓全文重寫）...")
        _run_gate('deepen_thin_cards', deepen_thin_cards, data, news_by_cat)
    # 11-17 B：放在補深之後——補深成功的卡素材已換成全文，不標 desc_only
    print("🏷 DESC_ONLY 卡片（只有 RSS 描述，core 上限 opp、body 最多 2 句）...")
    try:
        _run_gate('enforce_desc_only', enforce_desc_only, data, news_by_cat)
    except Exception as e:
        print(f"  ⚠ DESC_ONLY 標記失敗（略過，不影響資料生成）：{e}")
    print("🔢 數字閘門（title/body 數字一致且出自原文）...")
    try:
        _run_gate('numeric_gate', numeric_gate, data, news_by_cat)
    except Exception as e:
        print(f"  ⚠ 數字閘門失敗（略過，不影響資料生成）：{e}")
    print("📉 品質管線（降評低品質條目）...")
    _run_gate('remove_repeated_stories', remove_repeated_stories, data, recent_titles)
    _run_gate('downgrade_unsourced', downgrade_unsourced, data)
    print("🔍 body 品質檢核...")
    _run_gate('downgrade_low_quality', downgrade_low_quality, data)
    print("🔍 impact 品質檢核（換句話說偵測）...")
    _run_gate('downgrade_shallow_impact', downgrade_shallow_impact, data)
    print("🚨 幻覺 pattern 攔截...")
    _run_gate('downgrade_hallucination_patterns', downgrade_hallucination_patterns, data)
    _run_gate('downgrade_hallucinated', downgrade_hallucinated, data)
    print("🎯 HW 區跑偏偵測...")
    _run_gate('downgrade_hw_offtopic', downgrade_hw_offtopic, data)
    print("🔤 禁句 pattern 攔截...")
    _run_gate('downgrade_forbidden_phrases', downgrade_forbidden_phrases, data)
    print("🔁 跨條目重複偵測...")
    _run_gate('downgrade_cross_item_duplicates', downgrade_cross_item_duplicates, data)
    print("🔗 跨分類同網址重複移除...")
    _run_gate('remove_cross_item_url_duplicates', remove_cross_item_url_duplicates, data)
    _run_gate('strip_noise_impact', strip_noise_impact, data)
    print("🔎 body 聲明 LLM 驗證...")
    _run_gate('validate_body', validate_body, data, news_by_cat)
    print("🔗 supply chain 品質檢核...")
    _run_gate('fix_chains', fix_chains, data)
    print("🔎 impact 因果驗證...")
    _run_gate('validate_impact', validate_impact, data)
    print("🧠 語意去重/新鮮度檢核...")
    _run_gate('dedup_and_freshness_check', dedup_and_freshness_check, data, recent_items)
    print("🔍 主角矛盾偵測...")
    _run_gate('fix_protagonist_as_competitor', fix_protagonist_as_competitor, data)
    print("🧹 佔位卡攔截（真實卡存在時移除並存佔位卡）...")
    _run_gate('drop_stale_placeholders', drop_stale_placeholders, data)
    if FACT_CHECK_MODE != 'off':
        print(f"🧾 原文比對稽核（FACT_CHECK_MODE={FACT_CHECK_MODE}）...")
        try:
            _run_gate('fact_audit', fact_audit, data, news_by_cat)
        except Exception as e:
            print(f"  ⚠ 原文比對稽核失敗（略過，不影響資料生成）：{e}")
    if LLM_FACT_CHECK:
        print("🕵 LLM 查核關卡（逐則對照原文）...")
        try:
            _run_gate('llm_fact_check', llm_fact_check, data, news_by_cat)
        except Exception as e:
            print(f"  ⚠ LLM 查核失敗（略過，不影響資料生成）：{e}")
        print("🔤 查核修正過的卡片重跑既有文字閘門...")
        try:
            _run_gate('recheck_fixed_items', recheck_fixed_items, data)
        except Exception as e:
            print(f"  ⚠ 查核後文字閘門失敗（略過，不影響資料生成）：{e}")
        print("🔢 數字閘門（查核修正後再跑一次）...")
        try:
            _run_gate('numeric_gate', numeric_gate, data, news_by_cat)
        except Exception as e:
            print(f"  ⚠ 數字閘門失敗（略過，不影響資料生成）：{e}")
    print("🏷 HEADLINE_ONLY 卡片決定性清理（impact／insight／chain 清空、body 1 句）...")
    try:
        _run_gate('scrub_headline_only', scrub_headline_only, data)
    except Exception as e:
        print(f"  ⚠ HEADLINE_ONLY 清理失敗（略過，不影響資料生成）：{e}")
    print("🏷 DESC_ONLY 卡片決定性清理（impact／insight／chain 清空、body 最多 2 句）...")
    try:
        _run_gate('scrub_desc_only', scrub_desc_only, data)
    except Exception as e:
        print(f"  ⚠ DESC_ONLY 清理失敗（略過，不影響資料生成）：{e}")
    if data.get('weekly_summary'):
        print("🔢 週報數字出處檢查...")
        try:
            check_weekly_numbers(data, news_by_cat)
        except Exception as e:
            print(f"  ⚠ 週報數字檢查失敗（略過，不影響資料生成）：{e}")

    try:
        final = {}
        placeholders = 0
        for section in ['hw', 'corp', 'app']:
            items = data.get(section, [])
            tally = {'core': 0, 'opp': 0, 'noise': 0}
            for item in items:
                rating = item.get('rating')
                if rating in tally:
                    tally[rating] += 1
                if _is_placeholder(item):
                    placeholders += 1
            final[section] = tally
        RUN_STATS['final'] = final
        RUN_STATS['placeholders'] = placeholders
        save_run_stats(RUN_STATS)
        print("  → data/run_stats.json 已更新")
    except Exception as e:
        print(f"  ⚠ run_stats 寫入失敗（不影響主流程）：{e}")

    if DRY_RUN:
        write_dry_run_result(data, news_by_cat)
        print("✅ 完成（DRY_RUN：未寫 data/、未寄信、未推 Notion）")
        sys.exit(0)
    history = upsert(history, data)
    save_history(history)
    print(f"  → data/history.json 已更新（共 {len(history)} 天）")

    print("📧 發送 Email...")
    send_email(data)
    print("📝 推送 Notion...")
    push_notion(data)
    print("✅ 完成！")
