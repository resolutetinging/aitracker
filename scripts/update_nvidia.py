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

TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')

REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
NV_STATUS_PATH = os.path.join(REPO_DIR, 'data', 'nv_status.json')
NV_PENDING_PATH = os.path.join(REPO_DIR, 'data', 'nv_pending_review.json')

CATEGORY_LABELS = {
    'pyramid': '技術金字塔',
    'cases_live': '應用案例（已上線）',
    'cases_poc': '應用案例（POC）',
    'roadmap': '產品 Roadmap',
    'alliances': '聯盟／夥伴關係',
    'governance': '生態系權力布局／合規治理',
}
ACTION_LABELS = {'update': '更新既有項目', 'add': '新增項目'}


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
    """核對現有結構化資料是否過時，只回報有明確新聞佐證的候選異動，
    system prompt刻意要求極度保守，沒有證據支持的欄位一律維持原樣，
    不可為了「看起來有更新」而臆測。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是AI供應鏈分析師，任務是核對一份既有的NVIDIA結構化參考資料是否過時。"
        "只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "極度保守：沒有明確新聞佐證的欄位一律維持原樣、不提出更新建議；"
        "禁止臆測、禁止捏造來源URL、禁止把不確定的傳聞當成確定事實。"
        "多數週查證後的正確答案就是「沒有任何異動」，回傳空items陣列是完全正常且被期待的結果，"
        "不需要為了顯得有查證成果而硬湊出候選異動。"
    )
    prompt = f"""以下是NVIDIA相關結構化參考資料的現況（JSON）：

{json.dumps(current_status, ensure_ascii=False, indent=2)}

以下是過去一週蒐集到的NVIDIA相關新聞片段，每則片段結尾若有「| SOURCE_URL:網址」就是該則新聞的
原始來源網址；source欄位只能填這裡實際出現過的SOURCE_URL，禁止自己編造或憑記憶生成網址：

{chr(10).join(news_snippets) if news_snippets else '（本週未蒐集到相關新聞片段）'}

請核對上述新聞是否讓現有資料的任何欄位過時或不準確，只針對有明確新聞佐證的部分提出候選異動。
不要因為沒有新聞佐證就自己推測任何欄位「應該」要改；找不到能對應到具體新聞的變化就不要提出，
空陣列是完全正常的結果。

輸出格式（純JSON）：
{{
  "items": [
    {{
      "category": "pyramid|cases_live|cases_poc|roadmap|alliances|governance",
      "action": "update|add",
      "target_name": "若action=update，填現有資料裡對應項目的name或label文字；若action=add則留空",
      "name": "項目/公司名稱",
      "desc": "更新後或新增的描述文字",
      "status": "狀態文字（cases/roadmap/alliances/governance類別適用，pyramid類別留空）",
      "reason": "為何提出這個異動，具體說明新聞依據",
      "source": "新聞來源URL"
    }}
  ],
  "no_change_summary": "若items為空陣列，一句話說明本週查證後判斷現有資料仍準確；若items非空則留空字串"
}}"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=3000,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


def build_overview_html(status):
    """08-17第二版：使用者澄清這封信要的是「一週總結」——不管本週有沒有偵測到候選異動，
    都要讓使用者看一眼就了解NVIDIA各block（技術堆疊/案例/roadmap/聯盟/生態系布局）
    當下的完整現況，週跟週之間本來就會有大量重複內容（架構圖平常不會變）是預期行為，
    不是要避免的東西。所以這個區塊改成每週信件都固定包含，不是只有「無候選異動」
    才出現的備案內容；且每個項目附一行實質desc/status，不是只列名字。"""
    def item_lines(key, show_status=False):
        # 08-17第四版：使用者截圖回報bullet圓點跟標題掉成兩行——查根因是很多email
        # client（尤其行動版信箱app）不支援position:absolute，第三版靠絕對定位讓
        # 圓點疊在標題左邊，在瀏覽器（Playwright/Chromium）裡驗證正常，但email
        # client會直接忽略position屬性，讓<span>退回正常inline flow、被後面的
        # block級<div>換行擠開。改成bullet直接當文字前綴寫進標題那一行，不依賴任何
        # 定位屬性，任何HTML渲染引擎都會照樣把它跟標題文字排在同一行
        rows = ''
        for it in status.get(key, []):
            name = it.get('name') or it.get('label') or ''
            detail = it.get('items') or it.get('desc') or ''
            status_txt = f'<span style="font-weight:400;color:#8a6030;">（{it["status"]}）</span>' if show_status and it.get('status') else ''
            detail_html = f'<div style="margin-top:3px;padding-left:14px;font-size:12px;color:#6a6460;line-height:1.6;">{detail}</div>' if detail else ''
            rows += f'''<div style="margin:12px 0;">
              <div style="font-size:13px;font-weight:700;color:#2c2a28;line-height:1.5;"><span style="color:#6a8a20;">•</span> {name}{status_txt}</div>
              {detail_html}
            </div>'''
        return rows

    sections = [
        ('🔺', '技術堆疊', 'pyramid', False),
        ('✅', '已上線案例', 'cases_live', False),
        ('🔵', '概念驗證中', 'cases_poc', False),
        ('🗺', '產品 Roadmap', 'roadmap', True),
        ('🤝', '聯盟／夥伴', 'alliances', True),
        ('🔗', '生態系布局', 'governance', True),
    ]
    blocks = ''
    for emoji, label, key, show_status in sections:
        n = len(status.get(key, []))
        blocks += f'''
        <div style="margin:16px 0;">
          <div style="font-size:11.5px;font-weight:700;color:#6a8a20;margin-bottom:6px;">{emoji} {label}（{n}）</div>
          {item_lines(key, show_status)}
        </div>'''
    return f'''
    <div style="margin-top:8px;">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;margin-bottom:4px;">📌 本週 NVIDIA 專區現況總結</div>
      {blocks}
    </div>'''


def send_email(items, no_change_summary, status):
    """獨立信件，跟daily的AI產業動態信完全分開發送、不合併內容。
    收件人只送GitHub Secret設定的NOTIFY_EMAIL（比照daily的secret_recipients），
    不碰data/email_config.json的飛鴿公開名單——那份名單是給每日AI新聞訂閱的，
    NVIDIA週查證是另一種性質的內容，不應該未經同意就多推給那些人。"""
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
            cat = CATEGORY_LABELS.get(it.get('category', ''), it.get('category', ''))
            act = ACTION_LABELS.get(it.get('action', ''), it.get('action', ''))
            src = f'<div style="margin-top:6px;font-size:11px;"><a href="{it["source"]}" style="color:#6a8a20;">來源連結 →</a></div>' if it.get('source') else ''
            status_line = f'<div style="font-size:12px;color:#6a6460;margin-top:4px;">狀態：{it["status"]}</div>' if it.get('status') else ''
            cards += f'''
            <div style="background:#faf9f7;border-left:3px solid #6a8a20;padding:14px 16px;margin:10px 0;border-radius:0 6px 6px 0;">
              <div style="display:flex;gap:8px;margin-bottom:6px;">
                <span style="font-size:11px;font-weight:700;color:#6a8a20;background:#6a8a2018;padding:2px 8px;border-radius:10px;">{cat}</span>
                <span style="font-size:11px;color:#888;">{act}</span>
              </div>
              <div style="font-size:14px;font-weight:700;color:#2c2a28;margin-bottom:6px;">{it.get("name","")}</div>
              <div style="font-size:13px;color:#4a4744;line-height:1.6;margin-bottom:6px;">{it.get("desc","")}</div>
              {status_line}
              <div style="background:#f0ede9;border-radius:5px;padding:8px 12px;font-size:12px;color:#6a6460;margin-top:8px;">
                <span style="font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#9e9890;display:block;margin-bottom:4px;">查證依據</span>
                {it.get("reason","")}
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
          {summary}
        </div>'''
    body_html = changes_html + build_overview_html(status)

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

    print("🤖 Groq 核對現有資料是否過時...")
    try:
        diff = call_groq_diff(status, news)
        if not isinstance(diff, dict):
            raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
    except Exception as e:
        print(f"  ⚠ Groq 呼叫失敗，本次不更新任何內容：{e}")
        return

    items = diff.get('items') or []
    pending = {
        'checked_at': DATE_STR,
        'items': items,
        'no_change_summary': diff.get('no_change_summary', ''),
    }
    save_json(NV_PENDING_PATH, pending)

    # last_checked 時間戳寫回 nv_status.json 本身（這個欄位是唯一允許自動改寫的部分，
    # 純粹是「上次查證時間」的紀錄用途，不影響任何實質內容）
    status['last_checked'] = DATE_STR
    save_json(NV_STATUS_PATH, status)

    if items:
        print(f"  → 偵測到 {len(items)} 項候選異動，已寫入 data/nv_pending_review.json（未套用，待人工複核）")
    else:
        print(f"  → 本週查證後無需更新：{pending['no_change_summary']}")
    send_email(items, pending['no_change_summary'], status)
    # git commit/push 交給 GitHub Actions 的 git-auto-commit-action 處理（比照
    # daily-update.yml 慣例），本腳本只負責寫檔案，不自己動 git
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
