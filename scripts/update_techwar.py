#!/usr/bin/env python3
"""
中美科技戰 & 台灣角色專區週查證腳本

比照 scripts/update_nvidia.py 的整體架構（獨立於每日新聞pipeline之外，獨立
GitHub Actions週排程觸發，見 .github/workflows/weekly-techwar-update.yml），
但拿掉email通知邏輯——這個分頁目前不需要email。

安全設計（鐵律，跟update_nvidia.py的6大類一致）：本腳本絕不直接改寫
data/techwar_status.json 裡的4大類實質內容（recent_events/procurement_flows/
key_companies/capital_talent_controls）——那是實際顯示在頁面上、看起來權威的
參考資料，LLM若把幻覺內容寫進去會誤導使用者。所有候選異動一律寫進
data/techwar_pending_review.json，前端只顯示「本週查證偵測到 N 項候選異動」
的提示，實際套用需要使用者告知 Claude 人工複核後手動更新techwar_status.json。
唯一允許自動改寫techwar_status.json的欄位是last_checked（純粹是查證時間戳，
不影響任何實質內容）。
"""
import json, os, re, time
from datetime import datetime, timezone, timedelta
from groq import Groq
from groq import APIStatusError as GroqAPIStatusError

TW = timezone(timedelta(hours=8))
NOW = datetime.now(TW)
DATE_STR = NOW.strftime('%Y-%m-%d')

REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
TW_STATUS_PATH = os.path.join(REPO_DIR, 'data', 'techwar_status.json')
TW_PENDING_PATH = os.path.join(REPO_DIR, 'data', 'techwar_pending_review.json')

CATEGORY_LABELS = {
    'recent_events': '近期動態',
    'procurement_flows': '採購／供應流向',
    'key_companies': '關鍵企業',
    'capital_talent_controls': '資本與人才管制',
}


def fetch_techwar_news():
    """用DDG查最近一週中美科技戰相關新聞，關鍵字方向對準出口管制/關稅/TSMC/
    晶片走私轉運/人才資金管制/中國採購網絡，特別聚焦台灣在其中的角色與
    被影響性——這跟update_nvidia.py原本針對NVIDIA企業動態的關鍵字方向不同，
    這裡是國際政策/供應鏈層級的查證，不是單一公司動態。"""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("  ⚠ 找不到 ddgs/duckduckgo_search 套件，本次跳過新聞蒐集")
            return []
    queries = [
        ("出口管制/關稅", "China US chip export controls tariffs semiconductor policy Taiwan"),
        ("台灣供應鏈角色", "Taiwan TSMC semiconductor supply chain US China tech war role"),
        ("晶片走私/轉運", "AI chip smuggling transshipment diversion China sanctions evasion"),
        ("人才/資金管制", "China US talent capital controls investment restriction semiconductor"),
        ("中國採購網絡", "China chip procurement network shell company sanctions circumvention"),
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
    """核對現有4大類結構化資料是否過時或有值得新增的候選項目，只回報有明確
    新聞佐證的候選異動，system prompt刻意要求極度保守——沒有證據支持的內容
    一律不提出，多數週查證後的正確答案就是「沒有任何異動」。
    輸出schema跟update_nvidia.py的call_groq_diff()不同（那邊是category/action/
    target_name/name/desc/status），這裡改成target_field+data巢狀候選物件，
    比照前端render函式（twFlowCard/twCompanyCard/twControlCard/近期事件）
    實際吃的4種schema分別要求LLM輸出對應欄位，減少人工複核時的轉換成本。"""
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是中美科技戰與台灣供應鏈角色的分析師，任務是核對一份既有的結構化"
        "參考資料是否過時，並在有明確新聞佐證時提出候選新增/更新項目。"
        "只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "特別聚焦「台灣在中美科技戰中的角色與被影響性」這個視角，其餘國際"
        "動態（出口管制、關稅、晶片走私轉運、人才資金管制、中國採購網絡）"
        "若跟台灣供應鏈/廠商/政策因應無關聯，優先度較低。"
        "極度保守：沒有明確新聞佐證的欄位一律維持原樣、不提出更新建議；"
        "禁止臆測、禁止捏造來源URL、禁止把不確定的傳聞當成確定事實。"
        "多數週查證後的正確答案就是「沒有任何異動」，回傳空items陣列是完全"
        "正常且被期待的結果，不需要為了顯得有查證成果而硬湊出候選異動。"
    )

    def build_prompt(news_list):
        status_json = json.dumps(current_status, ensure_ascii=False, separators=(',', ':'))
        news_text = chr(10).join(news_list) if news_list else '（本週未蒐集到相關新聞片段）'
        schema_note = chr(10).join([
            '- target_field="recent_events" 的 data 欄位需含：date/title/category/thread(可留空)/summary/src/src_note(可留空)',
            '- target_field="procurement_flows" 的 data 欄位需含：title/summary/nodes(陣列，每個元素含label/sub/role，role只能是hw|mem|pkg|end其中之一)/arrows(陣列，長度=nodes長度-1)/src/src_note(可留空)',
            '- target_field="key_companies" 的 data 欄位需含：name/role/desc/exposure/policy_actions/risk/src/src_note(可留空)',
            '- target_field="capital_talent_controls" 的 data 欄位需含：measure/scope/desc/src/src_note(可留空)',
        ])
        return f"""以下是「中美科技戰與台灣角色」結構化參考資料的現況（JSON）：

{status_json}

以下是過去一週蒐集到的相關新聞片段，每則片段結尾若有「| SOURCE_URL:網址」就是該則新聞的
原始來源網址；候選項目的src欄位只能填這裡實際出現過的SOURCE_URL，禁止自己編造或憑記憶生成網址：

{news_text}

請核對上述新聞是否讓現有4大類資料（recent_events/procurement_flows/key_companies/
capital_talent_controls）過時或有值得新增的項目，只針對有明確新聞佐證的部分提出候選異動。
不要因為沒有新聞佐證就自己推測任何欄位「應該」要改；找不到能對應到具體新聞的變化就不要提出，
空陣列是完全正常的結果。

每個target_field對應的data欄位schema：
{schema_note}

輸出格式（純JSON）：
{{
  "items": [
    {{
      "target_field": "recent_events|procurement_flows|key_companies|capital_talent_controls",
      "data": {{ ...依上方schema... }},
      "reason": "為何提議新增/變更，具體說明新聞依據"
    }}
  ],
  "no_change_summary": "若items為空陣列，一句話說明本週查證後判斷現有資料仍準確；若items非空則留空字串"
}}"""

    # 比照update_nvidia.py call_groq_diff()的縮減重試機制：120b/20b雙model
    # fallback，413（超出TPM）時先換model，兩個model都失敗就砍news_snippets
    # 對半重試，最多5輪；current_status本身用compact JSON（無indent）省字元。
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
                    print(f"  → {model} 超出TPM（目前新聞{len(news_list)}則）...")
                    continue
                raise
        if response is not None:
            break
        if not news_list:
            break
        news_list = news_list[:len(news_list)//2]
        print(f"  → 縮減新聞片段至{len(news_list)}則重試...")
    if response is None:
        raise ValueError(f"連續{shrink_round+1}輪（含縮減新聞片段至{len(news_list)}則）仍超出Groq TPM限制，current_status本身可能已過大")
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    finish_reason = response.choices[0].finish_reason
    if finish_reason == 'length':
        raise ValueError(f"Groq回應被截斷（finish_reason=length，{len(raw)}字元）")
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


def main():
    print(f"\n{'='*50}")
    print(f"中美科技戰 & 台灣角色專區週查證 — {NOW.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    status = load_json(TW_STATUS_PATH, None)
    if status is None:
        print("  ⚠ 找不到 data/techwar_status.json，中止")
        return

    print("📰 蒐集中美科技戰相關新聞（過去一週，聚焦台灣角色與被影響性）...")
    news = fetch_techwar_news()
    print(f"  → 共 {len(news)} 則片段")

    # DDG從GitHub Actions機房IP常被限流（比照update_nvidia.py同樣的處理方式），
    # 若這週完全沒抓到新聞片段，直接跳過Groq呼叫——沒有任何新聞佐證卻硬要模型
    # 「核對是否過時」，等於在誘導它憑空生出候選異動
    if not news:
        print("  → 本週未蒐集到任何新聞片段，跳過Groq呼叫（避免無佐證卻要求提出異動）")
        pending = {
            'checked_at': DATE_STR,
            'items': [],
            'no_change_summary': '本週未蒐集到相關新聞片段，僅更新查證時間戳，未進行內容查證。',
        }
        save_json(TW_PENDING_PATH, pending)
        status['last_checked'] = DATE_STR
        save_json(TW_STATUS_PATH, status)
        print("✅ 完成（本週無新聞片段，僅更新查證時間戳）\n")
        return

    print("🤖 Groq 核對現有資料是否過時...")
    try:
        diff = call_groq_diff(status, news)
        if not isinstance(diff, dict):
            raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
    except Exception as e:
        # 比照update_nvidia.py的教訓：Groq失敗時仍要留下紀錄，last_checked刻意
        # 不更新，讓techwar_pending_review.json/techwar_status.json誠實反映
        # 「這週其實沒查證成功」，不能只print就悄悄return（沒有email通知的話
        # 更不能連檔案都不留任何痕跡）
        print(f"  ⚠ Groq 呼叫失敗，本次不更新任何內容：{e}")
        pending = {
            'checked_at': DATE_STR,
            'items': [],
            'no_change_summary': f'本週自動查證因技術問題失敗（{e}），現有資料未變動，將於下次排程自動重試。',
        }
        save_json(TW_PENDING_PATH, pending)
        return

    items = diff.get('items') or []
    pending = {
        'checked_at': DATE_STR,
        'items': items,
        'no_change_summary': diff.get('no_change_summary', ''),
    }
    save_json(TW_PENDING_PATH, pending)

    # last_checked 時間戳寫回 techwar_status.json 本身（唯一允許自動改寫的欄位）
    status['last_checked'] = DATE_STR
    save_json(TW_STATUS_PATH, status)

    if items:
        print(f"  → 偵測到 {len(items)} 項候選異動，已寫入 data/techwar_pending_review.json（未套用，待人工複核）")
    else:
        print(f"  → 本週查證後無需更新：{pending['no_change_summary']}")
    # git commit/push 交給 GitHub Actions 的 git-auto-commit-action 處理（比照
    # daily-update.yml/weekly-nvidia-update.yml 慣例），本腳本只負責寫檔案，不自己動 git
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
