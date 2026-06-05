#!/usr/bin/env python3
"""
AI Tracker 每日自動更新腳本 v3
新聞來源：RSS feeds（主）+ DuckDuckGo（副）
"""

import json, os, sys, smtplib, urllib.request, xml.etree.ElementTree as ET, time, re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from groq import Groq

FORCE_REGEN = '--force' in sys.argv

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
    # Google News RSS（精準關鍵字，優先餵給 LLM）
    ("Semiconductor", "https://news.google.com/rss/search?q=TSMC+HBM+CoWoS+semiconductor+AI+chip&hl=en-US&gl=US&ceid=US:en"),
    ("Semiconductor", "https://news.google.com/rss/search?q=NVIDIA+AMD+Intel+SK+Hynix+Micron+packaging&hl=en-US&gl=US&ceid=US:en"),
    ("CSP/CapEx",    "https://news.google.com/rss/search?q=Microsoft+Google+Meta+Amazon+AI+capex+data+center+2026&hl=en-US&gl=US&ceid=US:en"),
    ("App/AI",       "https://news.google.com/rss/search?q=Agentic+AI+Physical+AI+humanoid+robot+inference+2026&hl=en-US&gl=US&ceid=US:en"),
    # 重大展會（Computex / CES / GTC）—— 非展會期間自動沒有結果，無副作用
    ("Semiconductor", "https://news.google.com/rss/search?q=Computex+2026+AI+chip+GPU+NVIDIA+AMD&hl=en-US&gl=US&ceid=US:en"),
    # Semiconductor / Supply Chain — 專業媒體
    ("Semiconductor", "https://www.theregister.com/headlines.atom"),
    ("Semiconductor", "https://semiengineering.com/feed/"),
    ("Semiconductor", "https://www.tomshardware.com/feeds/all"),
    ("Semiconductor", "https://blocksandfiles.com/feed/"),
    ("Semiconductor", "https://feeds.reuters.com/reuters/technologyNews"),
    ("Semiconductor", "https://hnrss.org/frontpage?q=TSMC+HBM+CoWoS+OSAT+semiconductor+packaging"),
    ("Semiconductor", "https://hnrss.org/frontpage?q=NVIDIA+AMD+Intel+chip+wafer+capacity+supply"),
    # CSP CapEx / Cloud
    ("CSP/CapEx",    "https://www.datacenterdynamics.com/en/rss/"),
    ("CSP/CapEx",    "https://nextplatform.com/feed/"),
    ("CSP/CapEx",    "https://hnrss.org/frontpage?q=Microsoft+Google+Meta+Amazon+capex+data+center+AI+investment"),
    # Agentic / Physical AI
    ("App/AI",       "https://hnrss.org/frontpage?q=Agentic+AI+Physical+AI+robotics+humanoid+VLA+inference"),
    ("App/AI",       "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("App/AI",       "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
]

# 硬體供應鏈關鍵字（hw 分類必須命中其中之一）
HW_KEYWORDS = [
    "hbm","cowos","tsmc","nvidia","amd","intel","sk hynix","micron","samsung foundry",
    "semiconductor","packaging","wafer","gpu","chip","osat","asic","foundry",
    "advanced packaging","chiplet","tsv","emib","soi","reticle","capacity","fab",
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
    "computex","ces","gtc"  # 展會關鍵字：避免展覽期間大量相關新聞被過濾掉
]

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
        ("硬體供應鏈", "HBM CoWoS TSMC NVIDIA AMD AI chip 2026"),
        ("CSP資本支出",  "Microsoft Google Meta Amazon AI capex data center 2026"),
        ("新興應用",     "Agentic AI Physical AI robotics humanoid inference 2026"),
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

def fetch_news():
    print("  → RSS feeds...")
    rss = fetch_rss()
    print(f"  → RSS 取得 {len(rss)} 條")
    print("  → DuckDuckGo...")
    ddg = fetch_ddg()
    print(f"  → DDG 取得 {len(ddg)} 條")
    all_news = rss + ddg
    # Deduplicate by normalized title (first 80 chars, strip punctuation)
    seen, unique = set(), []
    for s in all_news:
        key = re.sub(r'[^\w\s]', '', s[:80]).lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(s)
    print(f"  → 去重後 {len(unique)} 條")
    # 限制總字數在 8000 字元以內，避免超過 Groq TPM 限制
    joined = "\n\n".join(unique)
    if len(joined) > 8000:
        joined = joined[:8000]
        print(f"  → 截斷至 8000 字元")
    return joined


def get_recent_titles(history, days=3, max_titles=20):
    """取得近 N 天已報道過的新聞標題，用於跨日去重（最多 max_titles 條）
    注意：今日自己的資料不納入（避免同日第二次跑時把素材全封鎖）"""
    titles = []
    for entry in history:
        if entry.get('date') == DATE_STR:
            continue  # 跳過今日，防止自我封鎖
        for section in ['hw', 'corp', 'app']:
            for item in entry.get(section, []):
                t = item.get('title', '').strip()
                if t:
                    titles.append(t)
        if len(titles) >= max_titles:
            break
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
    notes = load_notes() if IS_SUNDAY else {}
    notes_text = "; ".join(f"{d}:{n}" for d, n in sorted(notes.items()) if n.strip()) if notes else ""
    weekly_val = (
        '"hw weekly summary | corp weekly summary | app weekly summary | next week forecast'
        + (' | notes insight' if notes_text else '') + '"'
        if IS_SUNDAY else 'null'
    )
    no_repeat_str = ("Do NOT repeat these recently covered titles: " + "; ".join(recent_titles[:4])) if recent_titles else ""
    notes_ctx = ("User notes context: " + notes_text[:200]) if notes_text else ""
    news_short = news_context[:3500]

    return f"""You are an AI supply chain analyst. Analyze the news below and output pure JSON (start directly with {{).

NEWS:
{news_short}

CATEGORIES:
- hw: semiconductor/packaging (CoWoS/OSAT/HBM)/chip manufacturing only
- corp: CSP (Microsoft/Google/Meta/Amazon/AWS) CapEx, AI investment, earnings signals only — NOT stock prices
- app: Agentic AI, Physical AI, VLA, inference deployment

OUTPUT FORMAT:
{{"date":"{DATE_STR}","is_sunday":{str(IS_SUNDAY).lower()},"hw":[ITEMS],"corp":[ITEMS],"app":[ITEMS],"glossary_new":[{{"term":"","full":"","def":"2-3 sentences","why":"why it matters","category":"semiconductor|ai_technique|hardware|role"}}],"weekly_summary":{weekly_val}}}

Each ITEM: {{"title":"Traditional Chinese title","layer":"sublayer","body":"EXACTLY 3 sentences each with specific numbers/dates/company names","impact":"2-3 sentences in zh-TW analyzing how this news ripples through the supply chain: identify upstream suppliers (foundry/OSAT/memory/materials) and downstream customers (CSP/OEM/ODM/end users) by name, describe the specific direction of impact for each role (e.g. 產能吃緊/ASP走高/訂單轉移/資本支出削減), and explain why. Example: 「TSMC CoWoS 產能持續吃緊，Amkor/ASE 等 OSAT 廠商有望承接溢出封裝訂單；Nvidia 下游客戶出貨時程料延後 1-2 季。SK Hynix HBM ASP 因需求集中持續走高，Samsung 被迫加速 HBM3E 良率改善以防市占流失。」","rating":"core|opp|noise","insight":"1-sentence investor takeaway","source_label":"source name","source":"use SOURCE_URL value or empty string"}}

RULES:
- 2-4 items per section; if no relevant news → 1 noise item only
- One item = one story; if source mixes 2 unrelated stories, split into 2 items
- body: 3 sentences using ONLY facts from the provided news above — each sentence MUST start with a DIFFERENT subject (company/product/metric); NEVER start 2+ sentences with the same subject; each sentence must state a NEW fact not covered in the others; NEVER invent numbers, dates, or connections between companies not stated in the source; if you cannot write 3 genuinely different sentences from the source, rate it noise instead
- SOURCE REQUIREMENT: every core or opp item MUST have a SOURCE_URL from the news; if no SOURCE_URL exists for a story, you MUST rate it noise — never assign core/opp to unsourced items
- HALLUCINATION IS FORBIDDEN: do not combine unrelated companies or technologies; every company-technology pairing must come directly from the news text
- impact: must be 2-3 full sentences analyzing supply chain ripple effects; never a comma-separated keyword list; never vague phrases like "industry benefits"
- glossary_new: required, 1-3 terms from today's news that readers may not know
- source: copy verbatim from SOURCE_URL in the news; never fabricate URLs
- All titles, body, impact, insight in Traditional Chinese (zh-TW)
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
    items_json = json.dumps(
        [{"sec":sec,"title":item["title"],"body":item.get("body","")[:300]}
         for sec,item in bad], ensure_ascii=False)

    prompt = (
        "以下新聞的 chain 使用了「受益↑」「受損↓」等泛稱或節點不足2個，請重新生成。\n"
        "要求：每條 chain 2–4 個節點；label 必須含具體公司名/產品+方向詞，"
        "例如「TSMC CoWoS 產能↑」「SK Hynix ASP↑」「Azure GPU 交期↓」「AMD 市占↓」；"
        "嚴禁使用「受益」「受損」「受壓」等泛稱。\n"
        f"條目：{items_json}\n"
        '輸出純JSON陣列（直接從[開始）：'
        '[{"title":"原標題","chain":[{"label":"具體公司+方向","type":"up"}]}]'
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
        fixes = json.loads(raw)
        fixed = 0
        for fix in fixes:
            for sec in ['hw','corp','app']:
                for item in data.get(sec,[]):
                    if item['title'] == fix['title'] and fix.get('chain'):
                        item['chain'] = fix['chain']
                        fixed += 1
                        print(f"  ✓ {item['title'][:45]}")
        print(f"  → 共修正 {fixed} 條")
    except Exception as e:
        print(f"  → chain 修正失敗：{e}")

def call_groq(prompt):
    from groq import APIStatusError as GroqAPIStatusError
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = "你是AI供應鏈分析師。只輸出純JSON，不加說明。全程繁體中文：晶片/記憶體。"
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

def _body_is_low_quality(body: str) -> bool:
    """True = body 不達標（重複句 or 無具體數字）"""
    if not body or len(body) < 30:
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
    # core/opp body 必須含數字（%, $, 億, 倍, 具體數量）
    if not re.search(r'\d|%|億|兆|倍|萬|百億|千億', body):
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
    secret_to = os.environ.get('NOTIFY_EMAIL', user).replace('\xa0','').replace(' ','').strip()
    secret_recipients = [a.strip() for a in secret_to.split(',') if a.strip()]

    # Extra recipients from email_config.json (exclude those already in secret)
    extra_recipients = []
    cfg_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'email_config.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if not cfg.get('enabled', True):
                print("  → Email 推送已暫停（enabled=false），略過。"); return
            extra_recipients = [r.strip() for r in cfg.get('recipients', [])
                                if r.strip() and r.strip() not in secret_recipients]
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

    print("📰 抓取新聞...")
    news = fetch_news()
    total = len(news.splitlines())
    print(f"  → 合計 {total} 行新聞摘要")

    recent_titles = get_recent_titles(history, days=3)
    print(f"  → 近三日已報道標題 {len(recent_titles)} 條（今日自身已排除）")

    print("🤖 呼叫 Groq API...")
    data = call_groq(make_prompt(news, recent_titles))
    print(f"  → 硬體 {len(data.get('hw',[]))} / 巨頭 {len(data.get('corp',[]))} / 應用 {len(data.get('app',[]))} 則")

    print("🔗 驗證 source URL...")
    validate_sources(data)
    downgrade_unsourced(data)
    print("🔍 body 品質檢核...")
    downgrade_low_quality(data)
    print("🔗 supply chain 品質檢核...")
    fix_chains(data)

    history = upsert(history, data)
    save_history(history)
    print(f"  → data/history.json 已更新（共 {len(history)} 天）")

    print("📧 發送 Email...")
    send_email(data)
    print("📝 推送 Notion...")
    push_notion(data)
    print("✅ 完成！")
