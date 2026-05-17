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
                    snippets.append(f"[{label}] {r.get('title','')} — {r.get('body','')[:150]}")
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
    notes_text = "\n".join(
        f"- {d}：{n}" for d, n in sorted(notes.items()) if n.strip()
    ) if notes else ""

    recent_block = ""
    if recent_titles:
        recent_block = (
            "\n=== 近三日已報道（勿重複選用相同事件）===\n"
            + "\n".join(f"- {t}" for t in recent_titles)
            + "\n==========================================\n"
        )

    sunday_field = (
        f'"本週AI產業趨勢摘要（約300字）：\\n'
        f'【硬體供應鏈】這週 CoWoS/HBM/OSAT 最重要的產能或技術變動是...\\n'
        f'【CSP資本支出】Microsoft/Google/Meta/Amazon 這週最關鍵的投資行動是...\\n'
        f'【應用落地】Agentic AI/Physical AI/VLA 有哪些真實部署或技術突破...\\n'
        f'{"【筆記整合】（本週觀察：" + notes_text + "）請將這些觀察融入摘要。" if notes_text else ""}\\n'
        f'【下週預測】根據上述趨勢，下週最值得追蹤的一個具體指標或事件是..."'
        if IS_SUNDAY else 'null'
    )

    notes_block = (
        f"\n=== 本週使用者筆記（請融入週報）===\n{notes_text}\n==================================\n"
        if notes_text else ""
    )

    return f"""你是 AI 產業供應鏈分析師，專注 AI 晶片硬體生態、CSP 資本支出決策、Agentic/Physical AI 落地應用。
根據以下今日新聞，依三個維度整理，輸出純 JSON（不要任何 markdown、反引號或說明文字，直接從 {{ 開始）。

=== 今日新聞 ===
{news_context}
================{recent_block}{notes_block}
【分類定義（嚴格遵守）】
- hw（硬體缺口）：僅限半導體製造/封裝（CoWoS/OSAT/EMIB）/記憶體（HBM/LPDDR）/晶片供應鏈相關。應用層新聞、軟體功能、公司股價一律不歸 hw。
- corp（巨頭角力）：CSP（Microsoft/Google/Meta/Amazon/Apple）的 CapEx 決策、AI 基礎設施投資、財報中 AI 相關支出、平台戰略購併。
- app（新興應用）：Agentic AI、Physical AI、VLA 模型、推論端部署、機器人/人形機器人、具體落地案例。

輸出格式：
{{
  "date": "{DATE_STR}",
  "is_sunday": {str(IS_SUNDAY).lower()},
  "hw": [
    {{
      "title": "新聞標題（繁體中文，具體說明誰做了什麼、涉及哪個技術節點）",
      "layer": "封裝層/記憶體層/晶圓製造/散熱層（四選一）",
      "body": "3-4句重點摘要。每句結尾用句點。必須包含：具體數字、公司名稱、技術規格或時間點。",
      "chain": [
        {{"label": "受益方＋原因 ↑", "type": "up"}},
        {{"label": "受壓方＋原因 ↓", "type": "down"}},
        {{"label": "待觀察方 ⚠️", "type": "warn"}}
      ],
      "rating": "core",
      "insight": "一句話：從 AI 供應鏈投資者視角，這代表什麼產能/成本/競爭格局訊號？",
      "source_label": "Reuters",
      "source": "https://原始新聞網址（若有）"
    }}
  ],
  "corp": [ /* 同格式，layer：需求端/CapEx決策/財報訊號/平台戰略（四選一）*/ ],
  "app": [ /* 同格式，layer：Agentic AI/Physical AI/VLA模型/推論部署（四選一）*/ ],
  "glossary_new": [
    {{
      "term": "新術語縮寫",
      "full": "英文全名　繁體中文",
      "def": "清楚的定義說明（2-3句）",
      "why": "📌 為何對 AI 供應鏈/產業轉型重要（具體說明）",
      "category": "role/semiconductor/ai_technique/hardware（四選一）"
    }}
  ],
  "weekly_summary": {sunday_field}
}}

規則：
- rating 只能是 "core"/"noise"/"opp" 三選一（core=重大訊號；opp=投資/轉型機會；noise=背景雜訊）
- chain type 只能是 "up"/"down"/"warn" 三選一
- hw/corp/app 每個維度 2-4 條；若今日真的無符合分類的新聞，可回傳 1 條並標 rating:"noise"，不要強行歸類或憑空生成
- 每個條目的具體數字（金額/百分比/數量/規格）必須來自該條目本身的新聞，嚴禁從其他條目複製數字填充
- glossary_new 只列今天新出現術語，以下已知不要重複：{known}
- category：role=產業角色；semiconductor=半導體技術；ai_technique=AI技術方法；hardware=硬體/材料
- 若新聞來源有 URL，填入 source 欄位
- insight 欄位角度：AI 晶片供應鏈投資者/AI 產業轉型觀察者最在意的訊號（產能瓶頸/成本走向/競爭格局）
- 週日時 weekly_summary 需整合使用者筆記的觀察視角
- 若有資安/道德疑慮，加 "ethic": "說明" 欄位"""

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
            )},
            {"role":"user","content":prompt}
        ],
        temperature=0.45,
        max_tokens=4096,
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
        blocks += [h2("📊 本週摘要"), para(data['weekly_summary'])]

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
