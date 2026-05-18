#!/usr/bin/env python3
"""
AI Tracker 每日自動更新腳本 v3
新聞來源：RSS feeds（主）+ DuckDuckGo（副）
"""

import json, os, smtplib, urllib.request, xml.etree.ElementTree as ET, time, re
from datetime import datetime, timezone, timedelta
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
    # Semiconductor / Supply Chain（高訊噪比，優先）
    ("Semiconductor", "https://www.theregister.com/headlines.atom"),
    ("Semiconductor", "https://feeds.reuters.com/reuters/technologyNews"),
    ("Semiconductor", "https://hnrss.org/frontpage?q=TSMC+HBM+CoWoS+OSAT+semiconductor+packaging"),
    ("Semiconductor", "https://hnrss.org/frontpage?q=NVIDIA+AMD+Intel+chip+wafer+capacity+supply"),
    # CSP CapEx / Cloud
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
    "packaging","wafer","foundry","chiplet","osat"
]

def fetch_rss():
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
            for item in items[:8]:
                title = (item.findtext('title') or item.findtext('atom:title',namespaces=ns) or '').strip()
                desc  = (item.findtext('description') or item.findtext('summary') or
                         item.findtext('atom:summary',namespaces=ns) or '').strip()
                # Strip HTML tags
                desc = re.sub(r'<[^>]+>', '', desc)[:200]
                if title and any(kw in (title+desc).lower() for kw in AI_KEYWORDS):
                    snippets.append(f"[{label}] {title} — {desc}")
            print(f"  RSS {label}({url.split('/')[2]}): {len(items)} items")
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
                results = list(ddgs.news(q, max_results=5, timelimit="w"))
                for r in results:
                    snippets.append(f"[{label}] {r.get('title','')} — {r.get('body','')[:300]}")
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
    # 限制總字數在 5000 字元以內，避免超過 Groq TPM 限制
    joined = "\n\n".join(unique)
    if len(joined) > 5000:
        joined = joined[:5000]
        print(f"  → 截斷至 5000 字元")
    return joined


def get_recent_titles(history, days=3, max_titles=10):
    """取得近 N 天已報道過的新聞標題，用於跨日去重（最多 max_titles 條）"""
    titles = []
    for entry in history[:days]:
        for section in ['hw', 'corp', 'app']:
            for item in entry.get(section, []):
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

    recent_str = ("【勿重複】" + "／".join(recent_titles)) if recent_titles else ""
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

    # 新聞截斷至 2000 字元，控制 token 數
    news_short = news_context[:2000]

    return f"""AI產業供應鏈分析師。根據新聞輸出純JSON（直接從{{開始）。

新聞：{news_short}
{recent_str}
{notes_context}

分類：hw=半導體/封裝(CoWoS/OSAT/HBM)/晶片製造；corp=CSP(MS/Google/Meta/Amazon)CapEx/投資；app=Agentic AI/Physical AI/VLA/推論落地

格式（每區2-4條，無相關新聞則1條noise）：
{{"date":"{DATE_STR}","is_sunday":{str(IS_SUNDAY).lower()},"hw":[{{"title":"標題","layer":"封裝層/記憶體層/晶圓製造/散熱層","body":"3句含數字摘要","chain":[{{"label":"受益↑","type":"up"}},{{"label":"受壓↓","type":"down"}}],"rating":"core","insight":"供應鏈投資者視角","source_label":"來源","source":"url"}}],"corp":[同格式,layer:需求端/CapEx決策/財報訊號/平台戰略],"app":[同格式,layer:Agentic AI/Physical AI/VLA模型/推論部署],"glossary_new":[{{"term":"","full":"","def":"","why":"","category":"semiconductor/ai_technique/hardware/role"}}],"weekly_summary":{weekly_val}}}

規則：
- hw 僅限硬體供應鏈；各條目數字不得跨條目複製；已知術語勿重列:{known}
- 全程繁體中文，勿夾雜其他語言；「晶片」非「芯片」，「記憶體」非「内存」
- body 欄位嚴禁使用「...」「…」等省略符號，資訊不確定請直接省略或改寫成完整句子
- body 必須包含至少 3 句完整陳述，每句需含具體數字、時間點、公司名稱或技術細節，不得泛泛而談
- 若某條目的原始新聞資訊不足以寫出 3 句有內容的句子，請將該條評為 noise 並簡短說明，不要用空話填充"""

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
                "hw 分類僅限半導體/封裝/記憶體供應鏈，應用層或軟體新聞絕對不能放入 hw。"
                "每個條目的具體數字必須來自該條目本身的新聞，嚴禁跨條目複製數字或細節。"
                "若某維度今日無相關新聞，回傳 1 條 noise 評級的條目，不要憑空生成內容。"
                "每條 body 必須包含至少 3 句，每句需含具體數字、時間點或技術細節；資訊不足請評為 noise，不要用空話填充。"
                "全程繁體中文：晶片（非芯片）、記憶體（非内存）、處理器（非处理器）。"
            )},
            {"role":"user","content":prompt}
        ],
        temperature=0.45,
        max_tokens=2500,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
    return json.loads(raw)

# ══════════════════════════════════════════════════════════════════
#  4. HISTORY
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
    print(f"🚀 開始更新 AI Tracker（{DATE_STR}）...")
    print("📰 抓取新聞...")
    news = fetch_news()
    total = len(news.splitlines())
    print(f"  → 合計 {total} 行新聞摘要")

    history = load_history()
    recent_titles = get_recent_titles(history, days=3)
    print(f"  → 近三日已報道標題 {len(recent_titles)} 條（用於去重）")

    print("🤖 呼叫 Groq API...")
    data = call_groq(make_prompt(news, recent_titles))
    print(f"  → 硬體 {len(data.get('hw',[]))} / 巨頭 {len(data.get('corp',[]))} / 應用 {len(data.get('app',[]))} 則")

    history = upsert(history, data)
    save_history(history)
    print(f"  → data/history.json 已更新（共 {len(history)} 天）")

    print("📧 發送 Email...")
    send_email(data)
    print("📝 推送 Notion...")
    push_notion(data)
    print("✅ 完成！")
