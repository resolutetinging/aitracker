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
import json, os, re, sys, time
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



# ---------------------------------------------------------------------------
# 11-02 重構：原本整份 current_status（約45KB）塞進單一 prompt，必超 Groq 6000 TPM，
# 連續5週失敗。改為四大類各自一次呼叫，只送精簡欄位（比照 update_nvidia.py）。
# supply_chain_comparison 與地圖是靜態展示資料，不屬於週查證範圍，不送。
# ---------------------------------------------------------------------------
MAX_CATEGORY_CHARS = 4500   # 每類序列化後字數上限
CALL_INTERVAL_SEC = 65      # 呼叫間隔，讓 Groq TPM 滾動視窗重置（全腳本唯一的 Groq 呼叫點）
MAX_NEWS_PER_CAT = 14

# 每類：(精簡欄位, 各欄位字數上限, 新聞標籤分流)
CATEGORY_CONFIG = {
    'recent_events': {
        'fields': (('date', 12), ('category', 12), ('title', 60), ('summary', 70)),
        'news_labels': None,  # None＝全部新聞
        'schema': '近期事件：date/title/category/thread(可留空)/summary/src/src_note(可留空)',
    },
    'procurement_flows': {
        'fields': (('title', 60), ('summary', 80)),
        'news_labels': ('晶片走私/轉運', '中國採購網絡', '出口管制/關稅'),
        'schema': '採購流向：title/summary/nodes(陣列，每個元素含label/sub/role，role只能是hw|mem|pkg|end其中之一)/arrows(陣列，長度=nodes長度-1)/src/src_note(可留空)',
    },
    'key_companies': {
        'fields': (('name', 30), ('country', 8), ('role', 40), ('risk', 50)),
        'news_labels': ('台灣供應鏈角色', '出口管制/關稅', '中國採購網絡'),
        'schema': '關鍵企業：name/role/desc/exposure/policy_actions/risk/src/src_note(可留空)',
    },
    'capital_talent_controls': {
        'fields': (('measure', 50), ('scope', 40), ('desc', 70)),
        'news_labels': ('人才/資金管制', '出口管制/關稅'),
        'schema': '資本與人才管制：measure/scope/desc/src/src_note(可留空)',
    },
}


def _clip(v, n):
    s = str(v if v is not None else '').replace('|', '／').replace('\n', ' ').strip()
    return s[:n]


def build_category_data(status, cat):
    """只送該類精簡欄位（每筆壓成一行「欄位|欄位」字串，不送 src/長描述）。
    序列化超過 MAX_CATEGORY_CHARS 就把各欄位上限逐步砍半，直到放得下。"""
    cfg = CATEGORY_CONFIG[cat]
    items = [x for x in (status.get(cat) or []) if isinstance(x, dict)]
    if not items:
        raise ValueError(f'techwar_status.json 的 {cat} 無資料')
    scale = 1.0
    for _ in range(6):
        rows = []
        for it in items:
            row = []
            for k, n in cfg['fields']:
                v = it.get(k)
                if cat == 'procurement_flows' and k == 'summary':
                    pass
                row.append(_clip(v, max(8, int(n * scale))))
            if cat == 'procurement_flows':
                labels = [str(nd.get('label', '')) for nd in (it.get('nodes') or []) if isinstance(nd, dict)]
                row.append(_clip('→'.join(labels), max(20, int(80 * scale))))
            rows.append('|'.join(row))
        cols = [k for k, _n in cfg['fields']] + (['nodes'] if cat == 'procurement_flows' else [])
        data = {'format': '|'.join(cols), 'rows': rows}
        if len(json.dumps(data, ensure_ascii=False, separators=(',', ':'))) <= MAX_CATEGORY_CHARS:
            return data
        scale /= 2
    raise ValueError(f'{cat} 精簡後仍超過 {MAX_CATEGORY_CHARS} 字')


def news_for_category(news, cat):
    labels = CATEGORY_CONFIG[cat]['news_labels']
    if labels is None:
        picked = list(news)
    else:
        picked = [n for n in news if any(str(n).startswith(f'[{lb}]') for lb in labels)]
    return picked[:MAX_NEWS_PER_CAT]


def call_groq_diff_one(cat, cat_data, news_snippets):
    """單一大類核對：只回報有明確新聞佐證的候選異動，極度保守，空 items 是正常結果。"""
    cfg = CATEGORY_CONFIG[cat]
    label = CATEGORY_LABELS[cat]
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    sys_msg = (
        "你是中美科技戰與台灣供應鏈角色的分析師，任務是核對一份既有的結構化"
        "參考資料是否過時，並在有明確新聞佐證時提出候選新增/更新項目。"
        "只輸出純JSON，不加任何說明文字或markdown。"
        "全程繁體中文（禁止簡體字、日文、越南文等其他語言字詞混入）。"
        "特別聚焦「台灣在中美科技戰中的角色與被影響性」這個視角。"
        "極度保守：沒有明確新聞佐證的欄位一律維持原樣；禁止臆測、禁止捏造來源URL、"
        "禁止把不確定的傳聞當成確定事實。多數週查證後的正確答案就是「沒有任何異動」，"
        "回傳空items陣列是完全正常且被期待的結果。"
    )

    def build_prompt(news_list):
        data_json = json.dumps(cat_data, ensure_ascii=False, separators=(',', ':'))
        news_text = chr(10).join(news_list) if news_list else '（本週未蒐集到相關新聞片段）'
        return f"""以下是「中美科技戰與台灣角色」資料中「{label}」一類的現況（精簡欄位，格式見format）：

{data_json}

以下是過去一週蒐集到的相關新聞片段，結尾若有「| SOURCE_URL:網址」就是原始來源網址；
候選項目的src欄位只能填這裡實際出現過的SOURCE_URL，禁止自己編造網址：

{news_text}

請核對新聞是否讓「{label}」資料過時或有值得新增的項目，只針對有明確新聞佐證的部分提出候選異動，
找不到對應具體新聞的變化就不要提出，空陣列是正常結果。
候選項目 data 欄位需含：{cfg['schema']}

輸出格式（純JSON）：
{{
  "items": [
    {{
      "target_field": "{cat}",
      "data": {{ ...依上方欄位... }},
      "reason": "為何提議新增/變更，具體說明新聞依據"
    }}
  ],
  "no_change_summary": "若items為空陣列，一句話說明判斷現有資料仍準確；若items非空則留空字串"
}}"""

    # 保留既有機制：120b/20b 雙 model 備援，413（超出TPM）先換 model，都失敗就砍新聞對半重試。
    # gpt-oss 是 reasoning model，隱藏推理吃 max_tokens，輸出 JSON 至少留 2000，這裡給 3000。
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
        raise ValueError(f"連續{shrink_round+1}輪（含縮減新聞片段至{len(news_list)}則）仍超出Groq TPM限制")
    raw = response.choices[0].message.content.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    if response.choices[0].finish_reason == 'length':
        raise ValueError(f"Groq回應被截斷（finish_reason=length，{len(raw)}字元）")
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', raw)
    return json.loads(raw)


def set_last_checked_text(text, date_str):
    """文字層級只改 last_checked 值，不重新序列化整份 JSON（避免污染 diff）。"""
    pat = re.compile(r'("last_checked":\s*")[^"]*(")')
    m = pat.search(text)
    if not m:
        raise ValueError("techwar_status.json 找不到 last_checked 欄位")
    return text[:m.start()] + m.group(1) + date_str + m.group(2) + text[m.end():]


def write_last_checked():
    with open(TW_STATUS_PATH, 'r', encoding='utf-8') as f:
        text = f.read()
    with open(TW_STATUS_PATH, 'w', encoding='utf-8') as f:
        f.write(set_last_checked_text(text, DATE_STR))


def main():
    print(f"\n{'='*50}")
    print(f"中美科技戰 & 台灣角色專區週查證 — {NOW.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    status = load_json(TW_STATUS_PATH, None)
    if status is None:
        print("  ⚠ 找不到 data/techwar_status.json，中止")
        sys.exit(1)

    print("📰 蒐集中美科技戰相關新聞（過去一週，聚焦台灣角色與被影響性）...")
    news = fetch_techwar_news()
    print(f"  → 共 {len(news)} 則片段")

    # DDG 從 GitHub Actions 機房 IP 常被限流；完全沒新聞就不呼叫 Groq（避免無佐證卻誘導生出異動）
    if not news:
        print("  → 本週未蒐集到任何新聞片段，跳過Groq呼叫")
        save_json(TW_PENDING_PATH, {
            'checked_at': DATE_STR,
            'items': [],
            'no_change_summary': '本週未蒐集到相關新聞片段，僅更新查證時間戳，未進行內容查證。',
            'category_failures': [],
        })
        write_last_checked()
        print("✅ 完成（本週無新聞片段，僅更新查證時間戳）\n")
        return

    print("🤖 Groq 逐類核對現有資料是否過時...")
    all_items, no_change_parts, category_failures = [], [], []
    cats = list(CATEGORY_CONFIG)
    for idx, cat in enumerate(cats):
        label = CATEGORY_LABELS[cat]
        print(f"  [{idx+1}/{len(cats)}] {label}")
        try:
            cat_data = build_category_data(status, cat)
            diff = call_groq_diff_one(cat, cat_data, news_for_category(news, cat))
            if not isinstance(diff, dict):
                raise ValueError(f"Groq回傳非預期格式（非dict）：{type(diff)}")
            cat_items = [dict(it, target_field=cat) for it in (diff.get('items') or []) if isinstance(it, dict)]
            all_items.extend(cat_items)
            if not cat_items and diff.get('no_change_summary'):
                no_change_parts.append(f"{label}：{diff['no_change_summary']}")
            print(f"    → {'偵測到'+str(len(cat_items))+'項候選異動' if cat_items else '無需更新'}")
        except Exception as e:
            print(f"    ⚠ 「{label}」查證失敗，其他類繼續：{e}")
            category_failures.append(f"{label}：{e}")
        if idx < len(cats) - 1:
            time.sleep(CALL_INTERVAL_SEC)

    if len(category_failures) == len(cats):
        fail_summary = '；'.join(category_failures)
        print(f"  ⚠ 全部{len(cats)}類查證失敗，不更新 last_checked：{fail_summary}")
        save_json(TW_PENDING_PATH, {
            'checked_at': DATE_STR,
            'items': [],
            'no_change_summary': f'本週自動查證因技術問題失敗（{fail_summary}），現有資料未變動，將於下次排程自動重試。',
            'category_failures': category_failures,
        })
        sys.exit(1)

    summary = '' if all_items else '；'.join(no_change_parts)
    if category_failures:
        summary += ('；' if summary else '') + f'另有{len(category_failures)}類查證失敗：' + '；'.join(category_failures)
    save_json(TW_PENDING_PATH, {
        'checked_at': DATE_STR,
        'items': all_items,
        'no_change_summary': summary,
        'category_failures': category_failures,
    })
    write_last_checked()

    print(f"  → 共 {len(all_items)} 項候選異動，失敗 {len(category_failures)} 類；未套用，待人工複核")
    # git commit/push 交給 GitHub Actions（比照 weekly-nvidia-update.yml），本腳本只寫檔案
    print("✅ 完成\n")


if __name__ == '__main__':
    main()
