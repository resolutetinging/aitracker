#!/usr/bin/env python3
"""
AI Tracker 每日自動更新腳本 v3
新聞來源：RSS feeds（主）+ DuckDuckGo（副）
"""

import json, os, smtplib, urllib.request, urllib.error, xml.etree.ElementTree as ET, time, re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from groq import Groq

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
    # ── 硬體供應鏈 & 半導體（供應鏈投資訊號優先）────────────────────────
    ("Semiconductor", "https://www.theregister.com/headlines.atom"),
    ("Semiconductor", "https://www.eetimes.com/feed/"),                     # 專業半導體業界新聞
    ("Semiconductor", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),  # CNBC Tech（財報/投資訊號）
    # ── 產業採用 / 巨頭投資 ─────────────────────────────────────────
    ("CSP/CapEx",    "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("CSP/CapEx",    "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    # ── AI 應用落地（跨行業）─────────────────────────────────────────
    ("App/AI",       "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("App/AI",       "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("App/AI",       "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("App/AI",       "https://venturebeat.com/category/ai/feed/"),  # 企業 AI 採用與垂直行業落地
]

# 硬體供應鏈關鍵字（hw 分類必須命中其中之一）
HW_KEYWORDS = [
    "hbm","cowos","tsmc","nvidia","amd","intel","sk hynix","micron","samsung foundry",
    "semiconductor","packaging","wafer","gpu","chip","osat","asic","foundry",
    "advanced packaging","chiplet","tsv","emib","soi","reticle","capacity","fab"
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
    r'chip ban|wafer capacity|foundry capacity',
    re.IGNORECASE
)

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

def fetch_rss():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
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
            for item in items[:15]:
                title = (item.findtext('title') or item.findtext('atom:title',namespaces=ns) or '').strip()
                desc  = (item.findtext('description') or item.findtext('summary') or
                         item.findtext('atom:summary',namespaces=ns) or '').strip()
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
            print(f"  RSS {label}({url.split('/')[2]}): {len(items)} items, {kept} kept (48h filter)")
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
                results = list(ddgs.news(q, max_results=5, timelimit="d"))
                for r in results:
                    link = r.get('url', '')
                    url_part = f" | SOURCE_URL:{link}" if link else ""
                    snippets.append(f"[{label}] {r.get('title','')} — {r.get('body','')[:300]}{url_part}")
                print(f"  DDG '{label}': {len(results)} results")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                else:
                    print(f"  DDG '{label}' failed after 3 attempts: {e}")
    return snippets

def fetch_article_text(url, max_chars=500, timeout=6):
    """抓取文章內文前幾段真實段落，取代單薄的 RSS/DDG 摘要（title+短句無法支撐具體事實）"""
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
                enriched.append(f"{head} — {text}{url_part}")
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

def filter_recent(snippets, recent_titles):
    """過濾與近日已報道標題高度重疊的新聞摘要（code-level 去重，不依賴 LLM 指令）"""
    if not recent_titles:
        return snippets
    recent_norm = [_title_tokens(rt) for rt in recent_titles]
    filtered, dropped = [], 0
    for s in snippets:
        dash = s.find(' — ')
        bracket = s.find(']')
        title_part = s[bracket+1:dash if dash > 0 else bracket+80].strip()
        tokens = _title_tokens(title_part)
        # ≥3 個 bigram/英文詞重疊才視為重複（CJK bigram 比單詞更精準）
        is_dup = any(len(tokens & rn) >= 3 for rn in recent_norm) if tokens else False
        if is_dup:
            dropped += 1
        else:
            filtered.append(s)
    if dropped:
        print(f"  → 預過濾舊新聞 {dropped} 條（與近日標題重疊）")
    return filtered


def downgrade_repeated_stories(data, recent_titles):
    """生成後 title-level 去重：LLM 改寫標題繞過 NO-REPEAT 時的最後一道 code-level guard。
    用 CJK bigram + 英文詞混合 token 比對，≥50% 重疊且 ≥3 token 才降評（修正中文無空格問題）。"""
    if not recent_titles:
        return
    recent_norm = [_title_tokens(rt) for rt in recent_titles]
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') == 'noise':
                continue
            title = item.get('title', '')
            t_tokens = _title_tokens(title)
            if not t_tokens:
                continue
            for rn in recent_norm:
                if not rn:
                    continue
                overlap = len(t_tokens & rn)
                ratio = overlap / min(len(t_tokens), len(rn))
                if ratio >= 0.5 and overlap >= 3:
                    print(f"  ↓ 跨日重複降評→noise：{title[:60]}")
                    item['rating'] = 'noise'
                    break

def fetch_news(recent_titles=None):
    print("  → RSS feeds...")
    rss = fetch_rss()
    print(f"  → RSS 取得 {len(rss)} 條")
    print("  → DuckDuckGo...")
    ddg = fetch_ddg()
    print(f"  → DDG 取得 {len(ddg)} 條")
    all_news = rss + ddg
    # Deduplicate by title（去掉 [label] 前綴再比對，同一文章不同 section label 視為重複）
    seen, unique = set(), []
    for s in all_news:
        body = re.sub(r'^\[[^\]]+\]\s*', '', s)  # 移除 "[Semiconductor] " 等 label
        key = re.sub(r'[^\w\s]', '', body[:80]).lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(s)
    print(f"  → 去重後 {len(unique)} 條")
    # code-level 預過濾：移除與近三日標題重疊度高的新聞
    if recent_titles:
        unique = filter_recent(unique, recent_titles)
        print(f"  → 預過濾後剩 {len(unique)} 條")
    # 對高信號候選抓取完整內文取代薄摘要，並移到最前面優先保留
    unique = enrich_with_full_text(unique)
    # 限制總字數在 8000 字元以內，避免超過 Groq TPM 限制
    joined = "\n\n".join(unique)
    if len(joined) > 8000:
        joined = joined[:8000]
        print(f"  → 截斷至 8000 字元")
    return joined


def get_recent_titles(history, days=3, max_titles=30):
    """取得近 N 天 core/opp 標題用於跨日去重；排除今日自身與 noise 條目"""
    titles = []
    count = 0
    for entry in history:
        if entry.get('date') == DATE_STR:
            continue  # 跳過今日自身，避免二次執行自我封鎖
        if count >= days:
            break
        count += 1
        for section in ['hw', 'corp', 'app']:
            for item in entry.get(section, []):
                if item.get('rating') == 'noise':
                    continue  # noise 不列入 NO-REPEAT，否則佔位無效
                t = item.get('title', '').strip()
                if t:
                    titles.append(t)
    return titles[:max_titles]

def load_notes():
    """讀取使用者存到 repo 的每日筆記"""
    if os.path.exists('data/notes.json'):
        with open('data/notes.json', encoding='utf-8') as f:
            return json.load(f)
    return {}

# ══════════════════════════════════════════════════════════════════
#  2. PROMPT
# ══════════════════════════════════════════════════════════════════
def make_prompt(news_context, recent_titles=None):
    known = "、".join(KNOWN_TERMS)
    notes = load_notes() if IS_SUNDAY else {}
    notes_text = "; ".join(f"{d}:{n}" for d, n in sorted(notes.items()) if n.strip()) if notes else ""

    recent_str = (
        "【前三日已報道，嚴禁任何形式重複】以下是近三天已出現的 core/opp 標題。"
        "只要是同一家公司、同一產品、同一事件的報道（不論標題措辭是否不同），"
        "今日若無全新數字/新宣告/新公司動作，必須評為 noise，不得以任何方式重述或補充細節：\n"
        + "\n".join(f"・{t}" for t in recent_titles)
        + "\n以上清單中的故事今日若重複出現即為錯誤，請直接略過不生成。"
    ) if recent_titles else ""
    notes_context = ('【本週筆記參考（勿逐字複製，請融入分析寫成洞察）】' + notes_text) if notes_text else ''
    notes_line = '\\n【筆記整合】根據本週筆記寫出一句核心洞察（不得原文照抄）' if notes_text else ''
    weekly_val = (
        '"【硬體供應鏈】CoWoS/HBM/OSAT本週最重要產能或技術變動（一句話）'
        '\\n【CSP投資】MS/Google/Meta/Amazon最關鍵CapEx動作（一句話）'
        '\\n【應用落地】Agentic AI/Physical AI本週最具體進展（一句話）'
        '\\n【下週預測】最值得追蹤的一個指標或事件（一句話）'
        + notes_line + '"'
        if IS_SUNDAY else 'null'
    )

    # 新聞截斷至 5500 字元（給 LLM 更多新鮮素材）
    news_short = news_context[:5500]

    return f"""AI產業供應鏈分析師。根據新聞輸出純JSON（直接從{{開始）。

新聞：{news_short}
{recent_str}
{notes_context}

分類：hw=AI基礎設施供應鏈投資訊號（CoWoS/HBM/OSAT/Fab產能承諾、策略供應商決策、出口管制地緣影響，偏重資金承諾而非技術規格，不含GPU架構分析或晶片效能比較）；corp=產業AI採用訊號（CSP CapEx、大型企業AI合約、垂直行業部署醫療/汽車/金融/製造，商業化里程碑，不含股價）；app=現實世界AI應用推進（已落地產品、跨行業部署醫療/金融/製造/法律/創意/教育/物流，可量化商業成果，偏重已推出產品而非研究宣告）

格式（每區2-4條，無相關新聞則1條noise）：
{{"date":"{DATE_STR}","is_sunday":{str(IS_SUNDAY).lower()},"hw":[{{"title":"標題","layer":"封裝層/記憶體層/晶圓製造/散熱層","body":"3句含數字摘要","chain":[{{"label":"TSMC 議價能力↑","type":"up"}},{{"label":"AMD 交期拉長↓","type":"down"}},{{"label":"SK Hynix ASP↑","type":"up"}}],"rating":"core","insight":"供應鏈投資者視角","source_label":"來源","source":"url"}}],"corp":[同格式,layer:需求端/CapEx決策/財報訊號/平台戰略],"app":[同格式,layer:Agentic AI/Physical AI/VLA模型/推論部署],"glossary_new":[{{"term":"","full":"","def":"","why":"","category":"semiconductor/ai_technique/hardware/role"}}],"weekly_summary":{weekly_val}}}

規則：
- hw 僅限硬體供應鏈；各條目數字不得跨條目複製；已知術語勿重列:{known}
- 全程繁體中文，勿夾雜其他語言；「晶片」非「芯片」，「記憶體」非「内存」
- body 欄位嚴禁使用「...」「…」等省略符號，資訊不確定請直接省略或改寫成完整句子
- body 必須包含至少 3 句完整陳述，每句需含具體數字、時間點、公司名稱或技術細節，不得泛泛而談
- 若某條目的原始新聞資訊不足以寫出 3 句有內容的句子，請將該條評為 noise 並簡短說明，不要用空話填充
- 【body 禁句型】以下句型嚴禁出現：「根據報導，...」「預計在20XX年底前將達到每月N個」「已被多家公司採用，包括...」「正在助力...的發展」；每句必須直接陳述具體事實，不得轉述套話
- 【禁止通用經濟影響套話填充】若只掌握 1 個具體事實，禁止用「將迫使競爭對手提高服務質量和降低價格」「預計將創造大量的就業機會」「投資者應該關注...的發展」等任何公司新聞都套得上的空話填滿剩餘句數；第 2、3 句必須引用來源中另一個具體細節（數字/時間點/人名），若來源真的只有 1 個事實，該條目請評為 noise
- 【分析角度差異化】同日各條目 body 分析角度須各異：hw 聚焦產能/良率/技術參數；corp 聚焦財務決策/投資額/競爭動機；app 聚焦落地場景/效能數字/商業模式；禁止不同條目重用同一分析框架
- noise 條目只在該分區完全無相關新聞時才加入（限 1 條）；若已有 core 或 opp 條目，不得再混入 noise
- 【前三日已報道】清單中的主題：無新進展則必須 noise；絕不允許用改寫、重述、補充細節等方式「偽裝成新內容」通過審查
- chain label 必須是具體公司名/產品/角色 + 方向詞，例如「TSMC 議價能力↑」「Azure 交期拉長↓」；嚴禁使用「受益↑」「受壓↓」「受損↓」等泛稱；每條 chain 應有 2-4 個節點
- source 欄位必須直接使用新聞列表中「SOURCE_URL:」後的完整 URL；若該則新聞無 SOURCE_URL，則 source 填 ""，source_label 填 "—"；絕對禁止自行推測或捏造任何 URL"""

# ══════════════════════════════════════════════════════════════════
#  3. GROQ API
# ══════════════════════════════════════════════════════════════════
def call_groq(prompt):
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role":"system","content":(
                "你是 AI 產業供應鏈分析師，專注半導體供應鏈（HBM/CoWoS/OSAT）、CSP 資本支出、Agentic/Physical AI 落地。"
                "只輸出純 JSON，不加任何說明或 markdown 格式。"
                "【hw 分類鐵律】hw 絕對僅限：TSMC/三星/英特爾晶圓代工、CoWoS/OSAT/chiplet 封裝、HBM/DDR 記憶體、GPU/ASIC 供應鏈。"
                "凡標題/body 不含 TSMC/CoWoS/HBM/OSAT/GPU/晶圓/封裝/半導體 等核心詞彙，必須放 corp 或 app 或評為 noise，嚴禁放 hw。"
                "3D 列印/消費電子促銷/網路火災/資安/軟體/社群媒體等新聞一律不得進入 hw，違者即為錯誤輸出。"
                "【noise 鐵律】某分區若已有任何 core 或 opp 條目，該分區嚴禁再加 noise 條目。"
                "noise 條目的 title 必須是『本日無相關[分區名稱]新聞』，不得使用任何具體新聞標題。"
                "每個條目的具體數字必須來自該條目本身的新聞，嚴禁跨條目複製數字或細節。"
                "每條 body 必須包含至少 3 句，每句需含具體數字、時間點或技術細節；資訊不足請評為 noise，不要用空話填充。"
                "【body 禁句絕對規定】body 中嚴禁出現以下句型：「根據報導，」「預計在20XX年底前將達到每月」「已被多家公司採用，包括Google和Microsoft」「正在助力...的發展」「將繼續增加」；違者即為空洞輸出，視同錯誤。每句必須直接以事件主體開頭，陳述具體數字或技術動作。"
                "【NO-REPEAT 鐵律】user 訊息中【前三日已報道】清單的任何標題涉及的公司/產品/事件，若今日新聞中沒有全新數字或新宣告，直接跳過，不得生成任何條目。"
                "全程繁體中文：晶片（非芯片）、記憶體（非内存）、處理器（非处理器）。"
            )},
            {"role":"user","content":prompt}
        ],
        temperature=0.3,
        max_tokens=4000,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
    return json.loads(raw)

# ══════════════════════════════════════════════════════════════════
#  4. URL VALIDATION
# ══════════════════════════════════════════════════════════════════
def check_url(url, timeout=6):
    """HEAD request 驗證 URL，403/405 自動改用 GET（避免 The Register 等媒體誤判失效）"""
    if not url or not url.startswith('http'):
        return False
    for method in ('HEAD', 'GET'):
        try:
            req = urllib.request.Request(url, method=method,
                                         headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status < 400
        except urllib.error.HTTPError as e:
            if method == 'HEAD' and e.code in (403, 405):
                continue
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

# ══════════════════════════════════════════════════════════════════
#  4b. QUALITY PIPELINE
# ══════════════════════════════════════════════════════════════════
def fix_chains(data):
    """確保每條 chain 有合法的 label 與 type 欄位"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            chain = item.get('chain')
            if not isinstance(chain, list):
                item['chain'] = []
            else:
                item['chain'] = [c for c in chain if isinstance(c, dict) and c.get('label')]

def downgrade_unsourced(data):
    """無來源 URL 的 core/opp 條目降評為 noise（LLM 常幻覺 URL，這道閘強制把守）"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') in ('core', 'opp') and not item.get('source'):
                print(f"  ↓ 無來源降評→noise：{item.get('title','')[:50]}")
                item['rating'] = 'noise'

def _char_overlap(a, b):
    sa, sb = set(a.lower()), set(b.lower())
    if not sa or not sb:
        return 0
    return len(sa & sb) / min(len(sa), len(sb))

def _cjk_bigrams(text):
    cjk = re.sub(r'[^一-鿿]', '', text)
    return set(cjk[i:i+2] for i in range(len(cjk) - 1))

def downgrade_low_quality(data):
    """重複句 / 無數字 / 空洞 body → noise（三道閘）"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') == 'noise':
                continue
            body = item.get('body', '')
            # 無數字直接降評
            if not re.search(r'\d', body):
                print(f"  ↓ 無數字降評→noise：{item.get('title','')[:50]}")
                item['rating'] = 'noise'
                continue
            sents = [s.strip() for s in re.split(r'[。！？]', body) if len(s.strip()) > 5]
            dup = False
            for i in range(len(sents)):
                for j in range(i + 1, len(sents)):
                    # Gate 1: 字元集重疊 > 65%
                    if _char_overlap(sents[i], sents[j]) > 0.65:
                        dup = True; break
                    # Gate 2: CJK bigram 重疊 > 45%
                    bi, bj = _cjk_bigrams(sents[i]), _cjk_bigrams(sents[j])
                    if bi and bj and len(bi & bj) / min(len(bi), len(bj)) > 0.45:
                        dup = True; break
                    # Gate 3: CJK 前 5 字共用前綴
                    pi = re.sub(r'[^一-鿿]', '', sents[i])[:5]
                    pj = re.sub(r'[^一-鿿]', '', sents[j])[:5]
                    if len(pi) >= 3 and pi == pj:
                        dup = True; break
                if dup:
                    break
            if dup:
                print(f"  ↓ 重複句降評→noise：{item.get('title','')[:50]}")
                item['rating'] = 'noise'

# HW 區段不屬半導體/GPU/HBM/CoWoS/封裝/晶圓廠相關 → 降噪
HW_MUST_CONTAIN = re.compile(
    r'TSMC|台積|CoWoS|HBM|OSAT|封裝|晶圓|半導體|GPU|ASIC|AI.?chip|Nvidia|AMD|晶片|'
    r'HBM|Micron|SK.?Hynix|三星|Samsung|chiplet|wafer|fab|foundry|N\d[nN]|先進封裝',
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

# body 末尾幻覺偵測：句子包含「未來」+「預計」+百分比但 source 非可信財報詞 → noise
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

# prompt 明列禁句（Llama 常忽略）
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
    re.compile(r'對下游的.{1,30}需求產生正面的影響'),
    re.compile(r'競爭對手.{1,30}難以跟上.{1,20}的技術進步'),
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
]

def downgrade_forbidden_phrases(data):
    """body 含禁句 → noise；impact 含模板 → 清除 impact（無論 rating）"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            body = item.get('body', '')
            impact = item.get('impact') or ''
            if item.get('rating') != 'noise':
                for pat in FORBIDDEN_PATS:
                    if pat.search(body):
                        print(f"  ↓ 禁句降評→noise：{item.get('title','')[:50]}")
                        item['rating'] = 'noise'
                        break
            if impact:
                for pat in FORBIDDEN_PATS:
                    if pat.search(impact):
                        print(f"  ✕ 模板 impact 清除：{item.get('title','')[:50]}")
                        item['impact'] = None
                        break

def strip_noise_impact(data):
    """所有 noise 條目的 impact 欄位設為 None，避免模板框出現在前端"""
    for section in ['hw', 'corp', 'app']:
        for item in data.get(section, []):
            if item.get('rating') == 'noise' and item.get('impact'):
                item['impact'] = None

def downgrade_cross_item_duplicates(data):
    """跨條目 body+insight bigram 相似度 > 55% → 後者降評→noise（截圖問題：不同 item 描述幾乎相同）"""
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
    to   = os.environ.get('NOTIFY_EMAIL', user).replace('\xa0','').replace(' ','').strip()
    if not user or not pwd or not to:
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
                <span style="font-size:10px;text-transform:uppercase;letter-spacing:0.6px;color:#9e9890;display:block;margin-bottom:4px;">供應鏈影響鏈</span>
                {chain_text(item["chain"])}
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

    weekly = ''
    if data.get('weekly_summary'):
        weekly = f'''
        <div style="background:#fdf8f0;border:1px solid #d4b060;border-radius:8px;padding:16px 20px;margin:20px 0;">
          <div style="font-size:14px;font-weight:700;color:#a07040;margin-bottom:10px;">📊 本週摘要</div>
          <div style="font-size:13px;color:#4a4744;line-height:1.7;">{data["weekly_summary"]}</div>
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
          <a href="https://resolutetinging.github.io/aitracker/ai_tracker_v5.html"
             style="display:inline-block;background:#5a7fa8;color:#fff;padding:10px 24px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;">
            🔗 查看完整 Dashboard →
          </a>
          <div style="font-size:11px;color:#b0b0b0;margin-top:12px;">AI Tracker · 自動產生 · {DATE_STR}</div>
        </div>

      </div>
    </body></html>'''

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'📡 AI 動態 {DATE_STR}{"（週報）" if data.get("is_sunday") else ""} — {core_count} CORE · {opp_count} OPP'
    msg['From'] = user
    msg['To']   = to
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(user, pwd)
            s.send_message(msg)
        print(f"  → Email 已發送至 {to}")
    except Exception as e:
        print(f"  → Email 失敗：{e}")

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
        chain = ' → '.join(c['label'] for c in item['chain'])
        text = f"▸ {item['title']}\n{item['body']}\n供應鏈：{chain}\n注意方向：{item['insight']}"
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
    import sys
    force = '--force' in sys.argv
    pigeon_only = '--pigeon-only' in sys.argv

    print(f"🚀 開始更新 AI Tracker（{DATE_STR}）...")

    history = load_history()
    recent_titles = get_recent_titles(history, days=3)
    print(f"  → 近三日 core/opp 標題 {len(recent_titles)} 條（NO-REPEAT 用）")

    # 冪等保護：今日已有 ≥2 CORE 且非強制重跑
    today_entry = next((e for e in history if e.get('date') == DATE_STR), None)
    if today_entry and not force:
        core_n = sum(1 for s in ['hw','corp','app'] for i in today_entry.get(s,[]) if i.get('rating')=='core')
        if core_n >= 2:
            print(f"  → 今日已有 {core_n} 條 CORE，跳過重新生成（加 --force 可強制）")
            print("📧 發送 Email...")
            send_email(today_entry)
            print("✅ 完成（沿用今日現有資料）")
            sys.exit(0)

    print("📰 抓取新聞（含近日預過濾）...")
    news = fetch_news(recent_titles)
    total = len(news.splitlines())
    print(f"  → 合計 {total} 行新聞摘要")

    print("🤖 呼叫 Groq API...")
    data = call_groq(make_prompt(news, recent_titles))
    print(f"  → 硬體 {len(data.get('hw',[]))} / 巨頭 {len(data.get('corp',[]))} / 應用 {len(data.get('app',[]))} 則")

    print("🔧 修復供應鏈欄位...")
    fix_chains(data)

    print("🔗 驗證 source URL...")
    validate_sources(data)

    print("📉 品質管線（降評低品質條目）...")
    downgrade_repeated_stories(data, recent_titles)
    downgrade_unsourced(data)
    downgrade_hw_offtopic(data)
    downgrade_hallucinated(data)
    downgrade_forbidden_phrases(data)
    downgrade_cross_item_duplicates(data)
    downgrade_low_quality(data)
    strip_noise_impact(data)

    history = upsert(history, data)
    save_history(history)
    print(f"  → data/history.json 已更新（共 {len(history)} 天）")

    if not pigeon_only:
        print("📧 發送 Email...")
        send_email(data)
    print("📝 推送 Notion...")
    push_notion(data)
    print("✅ 完成！")
