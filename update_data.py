#!/usr/bin/env python3
"""
AI Tracker 每日自動更新腳本
API：Groq（免費）+ DuckDuckGo 新聞搜尋（免費，不需要 key）
"""

import json, os, smtplib, urllib.request
from datetime import datetime, timezone, timedelta
from groq import Groq
from duckduckgo_search import DDGS

# ── 時區設定（台灣 UTC+8）
TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')
IS_FRIDAY = NOW.weekday() == 4

KNOWN_TERMS = ["HBM","CoWoS","CSP","OSAT","VLA 模型",
               "Reticle Limit","CapEx","Agentic AI","Physical AI","TSV"]

# ══════════════════════════════════════════════════════════════════
#  1. 抓取當日新聞（DuckDuckGo，免費免 key）
# ══════════════════════════════════════════════════════════════════
def fetch_news():
    ddgs = DDGS()
    queries = [
        ("硬體/供應鏈", "HBM CoWoS TSMC AI chip supply 2026"),
        ("巨頭角力",     "Microsoft Google Meta Amazon AI capex data center 2026"),
        ("新興應用",     "Agentic AI Physical AI robotics humanoid 2026"),
    ]
    all_snippets = []
    for label, q in queries:
        try:
            results = ddgs.news(keywords=q, max_results=6, timelimit="w")
            for r in results:
                snippet = f"[{label}] {r.get('title','')} — {r.get('body','')[:300]}"
                all_snippets.append(snippet)
        except Exception as e:
            print(f"  warning: search '{label}' failed: {e}")
    return "\n\n".join(all_snippets)


# ══════════════════════════════════════════════════════════════════
#  2. 建立 Prompt
# ══════════════════════════════════════════════════════════════════
def make_prompt(news_context):
    known = "、".join(KNOWN_TERMS)
    friday_field = (
        '"本週AI產業趨勢摘要（200字）：硬體端：...巨頭端：...應用端：...未來一週預測：..."'
        if IS_FRIDAY else 'null'
    )
    return f"""你是 AI 產業首席情報官。根據以下今日新聞摘要，依三個維度整理，輸出純 JSON（不要任何 markdown、反引號或說明文字，直接從 {{ 開始）。

=== 今日新聞來源 ===
{news_context}
===================

輸出格式：
{{
  "date": "{DATE_STR}",
  "is_friday": {str(IS_FRIDAY).lower()},
  "hw": [
    {{
      "title": "新聞標題",
      "layer": "封裝層/記憶體層/先進封裝/散熱層（四選一）",
      "body": "2-3句重點摘要",
      "chain": [
        {{"label": "受影響方（方向說明）", "type": "up"}},
        {{"label": "受影響方（方向說明）", "type": "down"}}
      ],
      "rating": "core",
      "insight": "一句話對個人AI轉型者的啟發"
    }}
  ],
  "corp": [],
  "app":  [],
  "glossary_new": [
    {{"term": "新術語", "full": "英文全名　中文", "def": "定義說明", "why": "📌 為何重要"}}
  ],
  "weekly_summary": {friday_field}
}}

規則：
- rating 只能是 "core"/"noise"/"opp" 三選一
- chain type 只能是 "up"/"down"/"warn" 三選一
- 若有道德/安全疑慮，加 "ethic": "說明" 欄位
- 每個維度至少2條、最多4條
- glossary_new 只列今天新出現的術語，以下已知術語不要重複：{known}
- corp layer 用：需求端/採購策略/財報訊號
- app layer 用：Agentic AI/Physical AI/推論端
- 若今天是週五，weekly_summary 填入本週趨勢摘要與未來一週預測"""


# ══════════════════════════════════════════════════════════════════
#  3. Groq API 呼叫
# ══════════════════════════════════════════════════════════════════
def call_groq(prompt):
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": "你是 AI 產業分析師。只輸出純 JSON，不加任何說明或 markdown。"},
            {"role": "user",   "content": prompt}
        ],
        temperature=0.3,
        max_tokens=4000,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    return json.loads(raw)


# ══════════════════════════════════════════════════════════════════
#  4. History 管理
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
    idx = next((i for i, h in enumerate(history) if h['date'] == data['date']), None)
    if idx is not None: history[idx] = data
    else: history.append(data)
    return history


# ══════════════════════════════════════════════════════════════════
#  5. Email（選配）GMAIL_USER + GMAIL_APP_PASSWORD + NOTIFY_EMAIL
# ══════════════════════════════════════════════════════════════════
def send_email(data):
    user = os.environ.get('GMAIL_USER')
    pwd  = os.environ.get('GMAIL_APP_PASSWORD')
    to   = os.environ.get('NOTIFY_EMAIL', user)
    if not user or not pwd:
        print("  → Email 未設定，略過。"); return

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    core = [i for i in data['hw']+data['corp']+data['app'] if i.get('rating')=='core']
    opp  = [i for i in data['hw']+data['corp']+data['app'] if i.get('rating')=='opp']

    def card(item, color):
        return (f'<div style="background:#faf9f7;border-left:3px solid {color};'
                f'padding:10px 14px;margin:8px 0;border-radius:4px;">'
                f'<b>{item["title"]}</b><br>'
                f'<span style="color:#7a756f;font-size:13px;">{item["body"]}</span><br>'
                f'<i style="color:#8a5060;font-size:12px;">💡 {item["insight"]}</i></div>')

    weekly = (f'<h3 style="color:#a07040;">📊 本週摘要</h3>'
              f'<div style="background:#faf9f7;padding:14px;border-radius:4px;font-size:13px;">'
              f'{data["weekly_summary"]}</div>' if data.get('weekly_summary') else '')

    html = (f'<html><body style="font-family:sans-serif;max-width:600px;margin:auto;padding:24px;background:#eceae6;">'
            f'<h2 style="color:#5a7fa8;">📡 AI 產業動態</h2>'
            f'<p style="color:#9e9890;font-size:13px;">{DATE_STR}{"（週報）" if data.get("is_friday") else ""}</p>'
            f'<h3 style="color:#3a5a7a;">🔵 CORE 關鍵訊號</h3>'
            + ''.join(card(i,'#5a7fa8') for i in core)
            + f'<h3 style="color:#4a7060;">🟢 OPPORTUNITY</h3>'
            + ''.join(card(i,'#4a8a6a') for i in opp)
            + weekly
            + f'<hr style="border:none;border-top:1px solid #d8d4ce;margin:20px 0;">'
            f'<a href="https://resolutetinging.github.io/aitracker/ai_tracker_v4.html"'
            f' style="color:#5a7fa8;font-size:13px;">🔗 查看完整 Dashboard →</a>'
            f'</body></html>')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'📡 AI 動態 {DATE_STR}{"（週報）" if data.get("is_friday") else ""}'
    msg['From'] = user
    msg['To']   = to
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print(f"  → Email 已發送至 {to}")


# ══════════════════════════════════════════════════════════════════
#  6. Notion（選配）NOTION_TOKEN + NOTION_DB_ID
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

    core = [i for i in data['hw']+data['corp']+data['app'] if i.get('rating')=='core']
    opp  = [i for i in data['hw']+data['corp']+data['app'] if i.get('rating')=='opp']
    blocks = [h2("🔵 CORE"), *[para(f"▸ {i['title']}\n{i['body']}\n💡 {i['insight']}") for i in core],
              h2("🟢 OPP"),  *[para(f"▸ {i['title']}\n💡 {i['insight']}") for i in opp]]
    if data.get('weekly_summary'):
        blocks += [h2("📊 本週摘要"), para(data['weekly_summary'])]

    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "Name": {"title":[{"text":{"content":f'📡 AI 動態 {DATE_STR}{"（週報）" if data.get("is_friday") else ""}'}}]},
            "Date": {"date":{"start": DATE_STR}}
        },
        "children": blocks
    }
    req = urllib.request.Request("https://api.notion.com/v1/pages",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}","Content-Type":"application/json","Notion-Version":"2022-06-28"},
        method="POST")
    with urllib.request.urlopen(req) as res:
        print(f"  → Notion 頁面已建立：{json.loads(res.read())['id']}")


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print(f"🚀 開始更新 AI Tracker（{DATE_STR}）...")

    print("  → 搜尋今日新聞（DuckDuckGo）...")
    news = fetch_news()
    print(f"  → 取得 {len(news.splitlines())} 行新聞摘要")

    print("  → 呼叫 Groq API（llama-3.3-70b）分析...")
    data = call_groq(make_prompt(news))
    print(f"  → 取得：硬體 {len(data.get('hw',[]))} / 巨頭 {len(data.get('corp',[]))} / 應用 {len(data.get('app',[]))} 則")

    history = upsert(load_history(), data)
    save_history(history)
    print(f"  → data/history.json 已更新（共 {len(history)} 天）")

    send_email(data)
    push_notion(data)
    print("✅ 完成！")
