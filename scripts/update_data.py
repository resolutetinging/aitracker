#!/usr/bin/env python3
"""
AI Tracker 每日自動更新腳本 v3
新聞來源：RSS feeds（主）+ DuckDuckGo（副）
"""

import json, os, sys, smtplib, urllib.request, xml.etree.ElementTree as ET, time, re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from groq import Groq

FORCE_REGEN   = '--force' in sys.argv
PIGEON_ONLY   = '--pigeon-only' in sys.argv

TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')
IS_SUNDAY = NOW.weekday() == 6

KNOWN_TERMS = ["HBM","CoWoS","CSP","OSAT","VLA 模型",
               "Reticle Limit","CapEx","Agentic AI","Physical AI","TSV"]

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
    for label, url in RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
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
            print(f"  RSS {label}({url.split('/')[2]}): {len(items)} items, {kept} kept (72h filter)")
        except Exception as e:
            print(f"  RSS failed ({url.split('/')[2]}): {e}")
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

def fetch_article_text(url, max_chars=1100, timeout=6):
    """抓取文章內文前幾段真實段落，取代單薄的 RSS/DDG 摘要（title+200字短句無法支撐具體事實）"""
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(300_000)
        html = raw.decode('utf-8', errors='ignore')
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
        return None

def enrich_with_full_text(snippets, max_fetch=8):
    """對高信號候選（有 SOURCE_URL 且命中 HIGH_SIGNAL_PAT）抓取真實內文取代薄摘要；
    成功抓到內文的條目移到最前面，確保後續 3500 字截斷時優先保留高密度素材。"""
    fetched = 0
    enriched, rest = [], []
    for s in snippets:
        if fetched < max_fetch and 'SOURCE_URL:' in s and HIGH_SIGNAL_PAT.search(s):
            m = re.search(r'SOURCE_URL:(\S+)', s)
            text = fetch_article_text(m.group(1)) if m else None
            if text:
                head = s.split(' — ', 1)[0]
                url_part = s[s.find(' | SOURCE_URL:'):]
                # SOURCE_URL 緊接標題之後，不放在長內文最後面——內文拉長到 1100 字後，
                # LLM 常常讀到文末前就已經寫完 JSON，導致 source 欄位留空或抓錯
                enriched.append(f"{head}{url_part} — {text}")
                fetched += 1
                continue
        rest.append(s)
    if fetched:
        print(f"  → 已抓取 {fetched} 篇完整內文取代薄摘要")
    return enriched + rest

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
    # 對高信號候選抓取完整內文取代薄摘要，並移到最前面優先保留
    unique = enrich_with_full_text(unique)
    # 限制總字數，避免超過 Groq TPM 限制
    joined = "\n\n".join(unique)
    if len(joined) > 8000:
        joined = joined[:8000]
        print(f"  → 截斷至 8000 字元")
    return joined


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

def load_notes():
    """讀取每日筆記：優先用當日 Gist 快照 notes_backup.json，退回舊 notes.json"""
    for path in ('data/notes_backup.json', 'data/notes.json'):
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    return {}

def snapshot_notes_backup():
    """每日快照：從 Gist 抓 notes.json 寫入 data/notes_backup.json（config 缺失則靜默跳過）"""
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
def make_prompt(news_context, recent_titles=None):
    notes = load_notes() if IS_SUNDAY else {}
    notes_text = "; ".join(f"{d}:{n}" for d, n in sorted(notes.items()) if n.strip()) if notes else ""
    weekly_val = (
        '"HW：[半導體本週最重要一句摘要]\\nCORP：[CSP/CapEx本週一句摘要]\\nAPP：[新興AI本週一句摘要]\\n下週看點：[下週最值得追蹤的一個指標或事件]'
        + ('\\n用戶洞察：[根據用戶筆記的核心洞察]' if notes_text else '') + '"'
        if IS_SUNDAY else 'null'
    )
    no_repeat_str = ("NO-REPEAT (STRICT): these topics were covered in recent days — do NOT generate any item about the same story or event even with a different headline; only include if there is a significant new development with wholly new facts not present before: " + "; ".join(recent_titles)) if recent_titles else ""
    notes_ctx = ("User notes context: " + notes_text[:200]) if notes_text else ""
    news_short = news_context[:3500]
    weekly_rule = (
        'each distinct point must be its own line separated by \\n (one sentence per line, ending with 。); never merge multiple topics into one continuous paragraph'
        if IS_SUNDAY else
        'MUST be null — today is NOT Sunday; outputting any non-null value is an error'
    )

    return f"""You are an AI supply chain analyst. Analyze the news below and output pure JSON (start directly with {{).

NEWS:
{news_short}

CATEGORIES:
- hw: AI infrastructure supply chain signals — capacity commitments (CoWoS/HBM/OSAT/fab), strategic supplier decisions, export controls that shift production geography; prioritize financial/strategic signals over technical specifications; NOT GPU architecture analysis, chip benchmark comparisons, or speculative roadmap commentary
- corp: industry AI adoption signals — CSP CapEx, major enterprise AI contracts, vertical-sector deployments (healthcare/automotive/finance/manufacturing) by large incumbents, model commercialization milestones that show where AI is being adopted at scale; NOT stock prices
- app: real-world AI application advances — deployed products and services across any industry (healthcare, finance, manufacturing, legal, creative, education, logistics), measurable commercial traction, new business models enabled by AI; prefer concrete launched products over research-stage announcements

OUTPUT FORMAT:
{{"date":"{DATE_STR}","is_sunday":{str(IS_SUNDAY).lower()},"hw":[ITEMS],"corp":[ITEMS],"app":[ITEMS],"glossary_new":[{{"term":"","full":"","def":"2-3 sentences","why":"why it matters","category":"semiconductor|ai_technique|hardware|role"}}],"weekly_summary":{weekly_val}}}

Each ITEM: {{"title":"Traditional Chinese title","layer":"sublayer","body":"1 to 3 sentences, ALL about the SAME single news event. Write ONLY as many sentences as the source material actually supports with a distinct fact — every sentence must contain at least one specific number, date, or named entity from the source AND must state a fact NOT already stated in a previous sentence. HARD MINIMUM BAR: the total body must be at least 60 Traditional Chinese characters long AND must contain at least 1 distinct concrete fact (pick one: specific numbers/quantities, technical specs, monetary amounts, timelines/dates, or place names/locations) PLUS at least 1 clearly named entity (a specific company, institution, or product name) — a sentence with neither a concrete fact nor a named entity (pure vague restatement of the headline) is NOT acceptable and must not be treated as done. A single honest, fact-dense sentence is ALWAYS better than 3 sentences where sentences 2-3 restate sentence 1 in different words or add generic unsourced reasoning (e.g. speculation about competitors, job creation, timelines like 'will launch soon', or vague ambition/expansion framing) — that kind of padding gets the whole item rejected as noise, so never do it. If the source material genuinely cannot support 60 characters with 1 concrete fact and 1 named entity, do NOT pad with generic reasoning to reach the length — rate the item noise instead.","impact":"2-3 sentences in zh-TW tracing the upstream/downstream ripple effects on the SUPPLY CHAIN. Structure: (1) direct effect on the closest supply chain tier (e.g. TSMC capacity, HBM ASP, OSAT utilization); (2) second-order effect on the next tier; (3) if applicable, end-market or competitor implication. Every claim must be traceable to the news source — do NOT just rephrase the body. NEVER say '可能會影響X' without stating the direction (↑/↓) and mechanism. NEVER mention stock prices.","rating":"core|opp|noise","insight":"1-sentence investor takeaway","source_label":"source name","source":"use SOURCE_URL value or empty string"}}

RULES:
- 2-4 items per section; if no relevant news → 1 noise item only, and that item's title/body must PLAINLY say so (e.g. title:"今日無相關新聞", body:"今日該分類無足夠具體新聞素材可供分析。") — do NOT invent a vague-sounding pseudo-headline like "AI 應用進步" or "產業趨勢觀察" with circular reasoning; a fake generic title is worse than admitting there is no news
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
- All titles, body, impact, insight in Traditional Chinese (zh-TW)
- weekly_summary: {weekly_rule}
{no_repeat_str}
{notes_ctx}"""

# ══════════════════════════════════════════════════════════════════
#  3. GROQ API + CHAIN QUALITY FIX
# ══════════════════════════════════════════════════════════════════
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
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role":"system","content":"只輸出純JSON陣列，不加任何說明或markdown。"},
                {"role":"user","content":prompt}
            ],
            temperature=0.3, max_tokens=800,
        )
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
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "只輸出純JSON陣列，不加任何說明或markdown。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1, max_tokens=1200,
        )
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

def call_groq(prompt):
    from groq import APIStatusError as GroqAPIStatusError
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是AI供應鏈分析師。只輸出純JSON，不加說明。全程繁體中文：晶片（非芯片）、記憶體（非内存）、當機（非宕機）。"
        "【noise 鐵律】某分區若已有任何 core 或 opp 條目，該分區嚴禁再加 noise 條目。"
        "noise 條目的 title 必須是『本日無相關[分區名稱]新聞』，不得使用任何具體或看似具體的新聞標題（例如「AI 應用進步」「產業趨勢觀察」皆為違規）。"
        "【body 句數】body 是 1 到 3 句，句數視你實際掌握的具體事實數量而定，不得為了湊句數而重述前一句或加入未經證實的推論；"
        "湊出來的句子無論句數多寡都會被視為空洞輸出。"
    )
    models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    response = None
    for model in models:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role":"system","content":sys_msg},
                    {"role":"user","content":prompt}
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            if model != models[0]:
                print(f"  → 使用備用模型 {model}")
            break
        except GroqAPIStatusError as e:
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
            if method == 'HEAD' and e.code in (403, 405):
                continue  # HEAD 被拒，試 GET
            return False
        except Exception:
            return False
    return False

def validate_sources(data):
    """驗證所有 NewsItem 的 source URL；失效者清空 source/source_label"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            url = item.get('source', '')
            if not url:
                continue
            if check_url(url):
                print(f"  ✓ {url[:60]}")
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
    re.compile(r'根據.{0,25}報導[，,。]'),
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

def downgrade_forbidden_phrases(data):
    """body/insight 含禁句或日期幻覺 → noise；impact 含模板 → 清除 impact（無論 rating）"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            body = item.get('body', '')
            impact = item.get('impact') or ''
            insight = item.get('insight') or ''
            if item.get('rating') != 'noise':
                # 日期幻覺：body 聲稱 2 年前的「發表於」年份
                if _contains_stale_date(body):
                    print(f"  ↓ 日期幻覺→noise：{item.get('title','')[:50]}")
                    item['rating'] = 'noise'
                    continue
                hit = False
                for pat in FORBIDDEN_PATS:
                    if pat.search(body):
                        print(f"  ↓ 禁句降評→noise：{item.get('title','')[:50]}")
                        item['rating'] = 'noise'
                        hit = True
                        break
                # insight（注意方向）欄位過去從未被檢查，同一套通用套話會鑽這個漏洞
                if not hit:
                    for pat in FORBIDDEN_PATS:
                        if pat.search(insight):
                            print(f"  ↓ insight 禁句降評→noise：{item.get('title','')[:50]}")
                            item['rating'] = 'noise'
                            break
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
        text_i = all_items[i].get('body', '') + all_items[i].get('insight', '')
        bi_i = _cjk_bigrams(text_i)
        for j in range(i + 1, len(all_items)):
            if all_items[j].get('rating') == 'noise':
                continue
            text_j = all_items[j].get('body', '') + all_items[j].get('insight', '')
            bi_j = _cjk_bigrams(text_j)
            if bi_i and bi_j and len(bi_i) > 5 and len(bi_j) > 5:
                overlap = len(bi_i & bi_j) / min(len(bi_i), len(bi_j))
                if overlap > 0.55:
                    print(f"  ↓ 跨條目重複降評→noise：{all_items[j].get('title','')[:50]} (overlap {overlap:.0%})")
                    all_items[j]['rating'] = 'noise'

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
    """無來源網址且標題為「今日無…」句式的 noise 佔位卡"""
    return (not (item.get('source') or '').strip()
            and '今日無' in item.get('title', ''))

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


def downgrade_hallucination_patterns(data):
    """pattern-based 幻覺攔截：兩類最常見的 Llama 幻覺結構"""
    PATTERNS = [
        # 跨公司採用幻覺：「已被 Google/Microsoft/… 採用」
        (r'已(?:被|經).{0,25}(?:Google|Microsoft|Amazon|Meta|Apple|Nvidia|AMD).{0,25}採用',
         '跨公司採用幻覺'),
        (r'(?:Google|Microsoft|Amazon|Meta|Apple|Nvidia|AMD).{0,25}已(?:採用|導入|使用)',
         '跨公司採用幻覺'),
        # 產能模板幻覺：「每月X個單位/晶圓的產能」套用在非晶圓廠公司上
        (r'每月\d[\d,]*\s*(?:個|萬|千)\s*(?:單位|晶圓)',
         '非晶圓廠產能模板幻覺'),
    ]
    count = 0
    for sec in ['hw', 'corp', 'app']:
        for item in data.get(sec, []):
            if item.get('rating') not in ('core', 'opp'):
                continue
            body = item.get('body', '')
            for pat, reason in PATTERNS:
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
            print(f"  ↓ HW 跑偏降評→noise：{item.get('title','')[:60]}")
            item['rating'] = 'noise'

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


def validate_body(data, news_context):
    """LLM 二次驗證：body 聲明是否能在原始 RSS 摘要中找到支撐"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    items = [
        {"sec": sec, "title": item["title"], "body": item.get("body", "")}
        for sec in ['hw', 'corp', 'app']
        for item in data.get(sec, [])
        if item.get("rating") in ("core", "opp")
    ]
    if not items:
        print("  → body 聲明驗證：無 core/opp 條目"); return

    news_short = news_context[:2500]
    items_json = json.dumps(items, ensure_ascii=False)
    prompt = (
        "以下是今日 RSS 新聞摘要（原始來源）：\n"
        f"---\n{news_short}\n---\n\n"
        "以下是根據這些摘要生成的新聞條目。請逐一檢查每條的 body，判斷是否存在幻覺：\n"
        "1. body 中是否聲稱 Google/Microsoft/Amazon/Meta/Apple 已採用某技術，"
        "但摘要中未明確提及該公司作為客戶或夥伴？→ downgrade=true\n"
        "2. body 中是否出現「每月X個單位/晶圓的產能」，"
        "但摘要中未出現此數字，或該公司並非晶圓廠？→ downgrade=true\n"
        "3. body 中有無主要事實聲明（非推論）完全無法在摘要中找到對應文字？→ downgrade=true\n"
        f"4. body 中若出現「發表於 20XX 年」的具體年份，該年份必須來自摘要原文；"
        f"若摘要中沒有該年份，或年份明顯早於新聞發布時間（{NOW.year - 1}年以前），視為日期幻覺 → downgrade=true\n"
        "若以上均無問題 → downgrade=false。\n"
        f"條目：{items_json}\n"
        '只輸出純JSON陣列：[{"title":"原標題","downgrade":true或false}]'
    )
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "只輸出純JSON陣列，不加說明或markdown。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1, max_tokens=500,
        )
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
                            item['rating'] = 'noise'
                            count += 1
                            print(f"  ↓ body聲明無來源支撐→noise：{item['title'][:50]}")
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
NAMED_ENTITY_PAT = re.compile(
    r'\b(?!(?:The|This|That|These|Those|A|An|Our|Their|Its|His|Her|My|Your)\s)'
    r'[A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+|'
    r'都靈|米蘭|東京|首爾|矽谷|新加坡|深圳|北京|上海|香港|台北|倫敦|紐約|柏林|巴黎|'
    r'阿姆斯特丹|杜拜|雪梨|多倫多|奧斯汀|班加羅爾|不丹|'
    r'Turin|Milan|Tokyo|Seoul|Silicon Valley|Beijing|Shenzhen|Shanghai|Hong Kong|'
    r'Taipei|London|New York|Berlin|Amsterdam|Dubai|Singapore|Sydney|Toronto|'
    r'Austin|Bangalore|Bhutan'
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
            # CJK bigram 重疊率 > 45%（相同主題換句話說）
            b1, b2 = _cjk_bigrams(sentences[i]), _cjk_bigrams(sentences[j])
            if b1 and b2 and len(b1 & b2) / max(len(b1), len(b2), 1) > 0.45:
                return True
    # 2+ 句共用相同前 5 個中文字 → 主語重複
    prefixes = [_cjk_prefix(s) for s in sentences if len(_cjk_prefix(s)) >= 5]
    if len(prefixes) != len(set(prefixes)):
        return True
    # core/opp body 必須含數字（%, $, 億, 倍, 具體數量；含中文數字如「四個」「第一」）
    # 或具名地名/機構（NAMED_ENTITY_PAT）——兩者擇一即達標，對齊 make_prompt RULES
    has_number = re.search(r'\d|%|億|兆|倍|萬|百億|千億|[一二三四五六七八九十兩]+[個家款次項座台支波批輪席人年月日]|第[一二三四五六七八九十]', body)
    if not has_number and not NAMED_ENTITY_PAT.search(body):
        return True
    return False

def downgrade_low_quality(data):
    """body 重複或無具體數字的 core/opp 條目降級為 noise"""
    count = 0
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') in ('core', 'opp') and _body_is_low_quality(item.get('body', '')):
                item['rating'] = 'noise'
                count += 1
    if count:
        print(f"  → {count} 筆低品質 body（重複句/無數字）已降級為 noise")
    else:
        print("  → body 品質檢核通過")

# ══════════════════════════════════════════════════════════════════
#  5. HISTORY
# ══════════════════════════════════════════════════════════════════
DATA_PATH = 'data/history.json'

def load_history():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, encoding='utf-8') as f:
            return json.load(f)
    return []

def save_history(history):
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
def send_email(data):
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
        return ' → '.join(c['label'] for c in chain)

    def rating_badge(r):
        return {'core':'🔵 CORE','noise':'⚫ NOISE','opp':'🟢 OPP'}.get(r,'')

    def section_html(items, color, emoji, label):
        if not items: return ''
        cards = ''
        for item in items:
            eth = f'<div style="background:#fff8f0;border-left:3px solid #c07030;padding:8px 12px;margin:8px 0;font-size:12px;color:#805020;">⚠️ {item["ethic"]}</div>' if item.get('ethic') else ''
            src = f'<div style="margin-top:6px;font-size:11px;"><a href="{item["source"]}" style="color:{color};">{item.get("source_label","來源連結")} →</a></div>' if item.get('source') else ''
            cards += f'''
            <div style="background:#faf9f7;border-left:3px solid {color};padding:14px 16px;margin:10px 0;border-radius:0 6px 6px 0;">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
                <span style="font-size:11px;font-weight:700;color:{color};background:{color}18;padding:2px 8px;border-radius:10px;border:1px solid {color}44;">{item["layer"]}</span>
                <span style="font-size:11px;color:#888;">{rating_badge(item.get("rating",""))}</span>
              </div>
              <div style="font-size:14px;font-weight:700;color:#2c2a28;margin-bottom:8px;line-height:1.4;">{item["title"]}</div>
              <div style="font-size:13px;color:#4a4744;line-height:1.65;margin-bottom:10px;">{item["body"]}</div>
              <div style="background:#f0ede9;border-radius:5px;padding:8px 12px;font-size:12px;color:#6a6460;">
                <span style="font-size:10px;text-transform:uppercase;letter-spacing:0.6px;color:#9e9890;display:block;margin-bottom:4px;">供應鏈影響分析</span>
                {item.get("impact") or chain_text(item.get("chain") or [])}
              </div>
              {eth}
              <div style="margin-top:10px;font-size:12.5px;color:#3a6860;background:#f0f8f6;padding:8px 12px;border-radius:5px;border-left:2px solid #4a8a6a;">
                注意方向：{item["insight"]}
              </div>
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
                rows.append(f'<div style="margin:5px 0;padding:3px 0 3px 10px;border-left:2px solid #d4b060;"><strong style="font-weight:700;color:#8a6030;">{lbl}：</strong>{body}</div>')
            else:
                rows.append(f'<div style="margin:5px 0;padding:3px 0 3px 10px;border-left:2px solid #d4b060;">{ln}</div>')
        return ''.join(rows)

    weekly = ''
    if data.get('weekly_summary'):
        ws_html = _fmt_weekly_html(data['weekly_summary'])
        weekly = f'''
        <div style="background:#fdf8f0;border:1px solid #d4b060;border-radius:8px;padding:16px 20px;margin:20px 0;">
          <div style="font-size:14px;font-weight:700;color:#a07040;margin-bottom:10px;">📊 本週摘要</div>
          <div style="font-size:13px;color:#4a4744;line-height:1.7;">{ws_html}</div>
        </div>'''

    all_items = data['hw']+data['corp']+data['app']
    core_count = sum(1 for i in all_items if i.get('rating')=='core')
    opp_count  = sum(1 for i in all_items if i.get('rating')=='opp')

    html = f'''<html><body style="font-family:'Segoe UI',sans-serif;max-width:620px;margin:auto;padding:0;background:#eceae6;color:#2c2a28;">
      <div style="background:#faf9f7;padding:24px 28px;">

        <!-- Header -->
        <div style="border-bottom:1px solid #d8d4ce;padding-bottom:16px;margin-bottom:20px;">
          <div style="font-size:20px;font-weight:800;color:#2c2a28;">📡 AI 產業動態</div>
          <div style="font-size:13px;color:#9e9890;margin-top:4px;">{DATE_STR}{"（週報）" if data.get("is_sunday") else ""} &nbsp;·&nbsp; 自動更新</div>
        </div>

        <!-- Stats bar -->
        <div style="display:flex;gap:12px;margin-bottom:24px;">
          <div style="background:#eef3f8;border:1px solid #c8d8e8;border-radius:8px;padding:10px 16px;flex:1;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#5a7fa8;">{len(data["hw"])}</div>
            <div style="font-size:11px;color:#7a8898;">硬體缺口</div>
          </div>
          <div style="background:#f8f3ee;border:1px solid #e0c8a0;border-radius:8px;padding:10px 16px;flex:1;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#a07040;">{len(data["corp"])}</div>
            <div style="font-size:11px;color:#887060;">巨頭角力</div>
          </div>
          <div style="background:#eef5f0;border:1px solid #b0d0b8;border-radius:8px;padding:10px 16px;flex:1;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#4a8a6a;">{len(data["app"])}</div>
            <div style="font-size:11px;color:#608070;">新興應用</div>
          </div>
          <div style="background:#f0f5f8;border:1px solid #b8c8d8;border-radius:8px;padding:10px 16px;flex:1;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#3a5a7a;">{core_count} CORE / {opp_count} OPP</div>
            <div style="font-size:11px;color:#607080;">訊號分類</div>
          </div>
        </div>

        {weekly}
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

    subject = f'📡 AI 動態 {DATE_STR}{"（週報）" if data.get("is_sunday") else ""} — {core_count} CORE · {opp_count} OPP'

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
        text = f"▸ {item['title']}\n{item['body']}\n供應鏈影響：{impact}\n注意方向：{item['insight']}"
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
    if existing_today and not FORCE_REGEN:
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
    print(f"  → 近三日標題 {len(recent_titles)} 條、來源網址 {len(recent_urls)} 條（NO-REPEAT 用）")

    print("📰 抓取新聞（含近日預過濾）...")
    news = fetch_news(recent_titles, recent_urls)
    total = len(news.splitlines())
    print(f"  → 合計 {total} 行新聞摘要")

    hs_count = count_high_signal(news)
    print(f"  → 高信號素材 {hs_count} 條（門檻：3）")
    if hs_count < 3 and not FORCE_REGEN:
        print("  → 素材不足，跳過 LLM 生成，存為空日（零捏造模式）")
        data = make_empty_day()
        history = upsert(history, data)
        save_history(history)
        print(f"  → data/history.json 已更新（空日）")
        print("✅ 完成（空日）")
        sys.exit(0)

    print("🤖 呼叫 Groq API...")
    data = call_groq(make_prompt(news, recent_titles))
    print(f"  → 硬體 {len(data.get('hw',[]))} / 巨頭 {len(data.get('corp',[]))} / 應用 {len(data.get('app',[]))} 則")

    print("🔗 驗證 source URL...")
    validate_sources(data)
    print("📉 品質管線（降評低品質條目）...")
    remove_repeated_stories(data, recent_titles)
    downgrade_unsourced(data)
    print("🔍 body 品質檢核...")
    downgrade_low_quality(data)
    print("🚨 幻覺 pattern 攔截...")
    downgrade_hallucination_patterns(data)
    downgrade_hallucinated(data)
    print("🎯 HW 區跑偏偵測...")
    downgrade_hw_offtopic(data)
    print("🔤 禁句 pattern 攔截...")
    downgrade_forbidden_phrases(data)
    print("🔁 跨條目重複偵測...")
    downgrade_cross_item_duplicates(data)
    print("🔗 跨分類同網址重複移除...")
    remove_cross_item_url_duplicates(data)
    strip_noise_impact(data)
    print("🔎 body 聲明 LLM 驗證...")
    validate_body(data, news)
    print("🔗 supply chain 品質檢核...")
    fix_chains(data)
    print("🔎 impact 因果驗證...")
    validate_impact(data)
    print("🔍 主角矛盾偵測...")
    fix_protagonist_as_competitor(data)
    print("🧹 佔位卡攔截（真實卡存在時移除並存佔位卡）...")
    drop_stale_placeholders(data)

    history = upsert(history, data)
    save_history(history)
    print(f"  → data/history.json 已更新（共 {len(history)} 天）")

    print("📧 發送 Email...")
    send_email(data)
    print("📝 推送 Notion...")
    push_notion(data)
    print("✅ 完成！")
