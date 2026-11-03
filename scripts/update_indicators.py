#!/usr/bin/env python3
"""全球 AI 實際指標資料管線（只採官方原始來源，無需金鑰）。

多類自動抓取，任一類失敗不影響其他類，失敗時保留舊值：
  1. TSMC 月營收：TWSE OpenAPI（最新月）＋ MOPS 靜態頁（歷史回補）
  2. 四大雲端業者每季 CapEx：每季人工依官方財報查證填入，本腳本不抓、只保留
  3. Eurostat isoc_eb_ai：EU27、10 人以上企業使用 AI 比例
  4. 企業 AI 採用深化：Eurostat isoc_eb_ain2 產業排行；各地區（美 BTOS、韓 OECD、星 SingStat 自動，
     加 StatCan、澳 ABS 人工查證原樣保留）；跨區資訊業（歐／美自動，加／澳人工）

輸出 data/global_indicators.json。全部自動類失敗 exit 1，部分失敗 exit 0 並印警告。
來源資料變少時以 月份／季／年 為 key 合併，不刪舊點。
"""
import json
import os
import re
import sys
import time
import datetime as dt
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("INDICATORS_OUT") or os.path.join(ROOT, "data", "global_indicators.json")

BROWSER_UA = "Mozilla/5.0"

TWSE_API = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
MOPS_URL = "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{y}_{m}_0.html"
TSMC_SRC = "https://mopsov.twse.com.tw/nas/t21/sii/ (TWSE OpenAPI t187ap05_L、公開資訊觀測站 t21sc03，公司代號 2330)"
TSMC_MONTHS = 60


EU_URL = os.environ.get("INDICATORS_EU_URL") or (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/isoc_eb_ai"
    "?format=JSON&geo=EU27_2020&size_emp=GE10&unit=PC_ENT&indic_is=E_AI_TANY")
EU_SRC = "https://ec.europa.eu/eurostat/databrowser/view/isoc_eb_ai/default/table"


def http_get(url, ua=BROWSER_UA, timeout=60, retries=3):
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers={"User-Agent": ua})
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # 網路暫時性錯誤
            last = e
        time.sleep(2 * (i + 1))
    raise last


def http_json(url, ua=BROWSER_UA):
    # strict=False：容忍來源 JSON 內的控制字元
    return json.loads(http_get(url, ua).decode("utf-8"), strict=False)


# ---------- 1. TSMC ----------
def ym_add(y, m, delta):
    n = y * 12 + (m - 1) + delta
    return n // 12, n % 12 + 1


def parse_mops_2330(html_bytes):
    html = html_bytes.decode("big5", errors="replace")
    m = re.search(r"<td[^>]*>\s*2330\s*</td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>\s*([\d,]+)\s*</td>",
                  html, re.I)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def with_yoy(series):
    by = {p["month"]: p for p in series}
    out = []
    for p in sorted(series, key=lambda x: x["month"]):
        y, m = int(p["month"][:4]), int(p["month"][5:])
        prev = by.get("%04d-%02d" % (y - 1, m))
        yoy = None
        if prev and prev["revenue"]:
            yoy = round((p["revenue"] / prev["revenue"] - 1) * 100, 1)
        out.append({"month": p["month"], "revenue": p["revenue"], "yoy": yoy})
    return out


def fetch_tsmc(old):
    old_series = (old or {}).get("series", [])
    have = {p["month"]: p for p in old_series}
    fresh = {}
    latest = None
    try:
        rows = http_json(TWSE_API)
        row = next(r for r in rows if r.get("公司代號") == "2330")
        ym = str(row["資料年月"])  # 民國 YYYMM，例 11509
        y, m = int(ym[:-2]) + 1911, int(ym[-2:])
        latest = (y, m)
        fresh["%04d-%02d" % (y, m)] = round(int(row["營業收入-當月營收"]) / 100000, 1)
    except Exception as e:
        print("警告：TWSE OpenAPI 失敗（%s），改用 MOPS 回補" % e)
    if latest is None:
        today = dt.date.today()
        latest = ym_add(today.year, today.month, -1)
    for i in range(TSMC_MONTHS):
        y, m = ym_add(latest[0], latest[1], -i)
        key = "%04d-%02d" % (y, m)
        if key in fresh or key in have:
            continue
        try:
            rev = parse_mops_2330(http_get(MOPS_URL.format(y=y - 1911, m=m)))
        except HTTPError:
            rev = None
        except Exception as e:
            print("警告：MOPS %s 失敗：%s" % (key, e))
            rev = None
        if rev is not None:
            fresh[key] = round(rev / 100000, 1)  # 千元 -> 億元
        time.sleep(0.4)
    if not fresh and not have:
        raise RuntimeError("TWSE 與 MOPS 皆未取得任何月份")
    if not fresh and old_series:
        raise RuntimeError("TWSE 與 MOPS 皆未取得新資料")
    merged = dict((k, {"month": k, "revenue": v["revenue"]}) for k, v in have.items())
    for k, v in fresh.items():
        merged[k] = {"month": k, "revenue": v}
    series = with_yoy(list(merged.values()))
    return {"src": TSMC_SRC, "unit": "新台幣億元", "series": series}


# ---------- 3. Eurostat ----------
def fetch_eu(old):
    d = http_json(EU_URL)
    idx = d["dimension"]["time"]["category"]["index"]  # {"2021": 0, ...}
    vals = d["value"]
    fresh = {}
    for yr, pos in idx.items():
        v = vals.get(str(pos))
        if v is not None:
            fresh[int(yr)] = round(float(v), 2)
    if not fresh:
        raise RuntimeError("Eurostat 回應無數值")
    merged = dict((p["year"], p["value"]) for p in (old or {}).get("series", []))
    merged.update(fresh)
    return {"src": EU_SRC, "unit": "%", "scope": "EU27，10 人以上企業",
            "series": [{"year": y, "value": merged[y]} for y in sorted(merged)]}


# ---------- 4. 企業 AI 採用：產業排行／各地區／跨區資訊業 ----------
import csv
import io
import zipfile
import html as _html

IND_URL = os.environ.get("INDICATORS_IND_URL") or (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/isoc_eb_ain2"
    "?format=JSON&lang=EN&unit=PC_ENT&indic_is=E_AI_TANY&size_emp=GE10&geo=EU27_2020")
IND_TOTAL = "C10-S951_X_K"  # 全部產業總計列，排除
# 彙總碼（內含其他列的上層或重複碼）；前端排行應只取 agg 為 False 的列
IND_AGG = set(["C-F", "C-E", "C", "C10-C18", "C19-C23", "C26-C33", "C27_C28", "D_E", "D",
               "G", "G45-S951_X_K", "I", "J", "L", "L_M", "M", "N", "ICT"])
NACE_ZH = {
    "C-F": "製造、能源、水電與營建", "C-E": "製造、能源與水資源",
    "C": "製造業", "C10-C18": "食品、紡織、木材紙業製造",
    "C10-C12": "食品、飲料與菸草製造", "C13-C15": "紡織、服飾與皮革製造",
    "C16-C18": "木製品、紙業與印刷", "C19-C23": "石化、化學與非金屬製品製造",
    "C19": "煉油與焦炭製品", "C20": "化學品製造", "C21": "製藥",
    "C22_C23": "橡塑膠與非金屬礦物製品", "C24_C25": "基本金屬與金屬製品",
    "C26-C33": "電腦、電機與機械設備製造", "C26": "電腦、電子與光學產品製造",
    "C27_C28": "電機設備與機械製造", "C27": "電機設備製造", "C28": "機械設備製造",
    "C29_C30": "汽車與運輸設備製造", "C31-C33": "家具、珠寶與其他製造",
    "D_E": "電力燃氣與水資源廢棄物", "D": "電力、燃氣與空調供應",
    "D35": "電力、燃氣與空調供應", "E": "水資源、廢棄物與環境整治",
    "F": "營建業", "G": "批發與零售業", "G45-S951_X_K": "服務業（不含金融保險）",
    "G45": "汽機車批發零售與維修", "G46": "批發業（不含汽機車）",
    "G47": "零售業（不含汽機車）", "H": "運輸與倉儲", "I": "住宿與餐飲",
    "I55": "住宿業", "J": "資訊及通訊業", "J58-J60": "出版、影視與廣播",
    "J61": "電信業", "J62_J63": "電腦程式設計與資訊服務",
    "L_M": "不動產與專業科學技術", "L": "不動產業", "L68": "不動產活動",
    "M": "專業、科學及技術服務", "M69-M71": "法律會計、管理顧問與建築工程",
    "M72": "科學研究與發展", "M73-M75": "廣告市調與其他專業科技服務",
    "N77-N82_X_N79": "租賃、人力派遣與保全等支援服務", "N": "行政支援服務",
    "N79": "旅行社與訂位服務", "S951": "電腦與通訊設備維修",
    "ICT": "資通訊產業合計",
}
IND_SCOPE = ("EU27，10 人以上企業（不含農林漁、採礦、金融保險），"
             "過去 12 個月使用至少一種 AI 技術")


def fetch_industry(old):
    d = http_json(IND_URL)
    years = sorted(int(y) for y in d["dimension"]["time"]["category"]["index"])
    tpos = d["dimension"]["time"]["category"]["index"]
    nidx = d["dimension"]["nace_r2"]["category"]["index"]
    nt = len(tpos)
    vals = d["value"]

    def get(code, yr):
        v = vals.get(str(nidx[code] * nt + tpos[str(yr)]))
        return None if v is None else round(float(v), 2)

    # 最新年：至少 10 個產業有值
    ok_years = [y for y in years if sum(1 for c in nidx if get(c, y) is not None) >= 10]
    if len(ok_years) < 2:
        raise RuntimeError("Eurostat 產業資料不足兩個年度")
    cur, prev = ok_years[-1], ok_years[-2]
    items = []
    for code in nidx:
        if code == IND_TOTAL:
            continue
        v = get(code, cur)
        if v is None:
            continue
        if code not in NACE_ZH:
            print("警告：NACE 代碼 %s 無中文對照，以代碼顯示" % code)
        items.append({"code": code, "label": NACE_ZH.get(code, code), "value": v,
                      "prev": get(code, prev), "agg": code in IND_AGG})
    items.sort(key=lambda x: -x["value"])
    if not items:
        raise RuntimeError("Eurostat 產業回應無數值")
    return {"src": IND_URL, "scope": IND_SCOPE, "year": cur, "prev_year": prev, "items": items}


def read_xlsx(raw):
    """標準庫解析 xlsx：回傳 {工作表名: [ {欄名: 值} ]}，不需 openpyxl。"""
    z = zipfile.ZipFile(io.BytesIO(raw))
    ss = [_html.unescape(re.sub(r"<[^>]+>", "", m)) for m in
          re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
    names = re.findall(r'<sheet name="([^"]*)"', z.read("xl/workbook.xml").decode("utf-8"))
    out = {}
    for i, name in enumerate(names, 1):
        x = z.read("xl/worksheets/sheet%d.xml" % i).decode("utf-8")
        rows = []
        for r in re.findall(r"<row [^>]*>(.*?)</row>", x, re.S):
            row = {}
            for m in re.finditer(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', r, re.S):
                col, attr, inner = m.groups()
                v = re.search(r"<v>(.*?)</v>", inner or "")
                if not v:
                    continue
                v = v.group(1)
                row[col] = ss[int(v)] if 't="s"' in attr else v
            rows.append(row)
        out[name] = rows
    return out


def pct(s):
    m = re.match(r"^\s*([\d.]+)%\s*$", s or "")
    return float(m.group(1)) if m else None


def btos_label(smpdt):
    return "%s 第 %d 期" % (smpdt[:4], int(smpdt[4:]))


BTOS_NAT = "https://www.census.gov/hfp/btos/downloads/National.xlsx"
BTOS_SEC = "https://www.census.gov/hfp/btos/downloads/Sector.xlsx"
BTOS_PAGE = "https://www.census.gov/hfp/btos/data_downloads"
US_SCOPE = ("有員工的非農業私部門企業，不分規模（含 1–4 人）；回顧過去兩週是否在任何業務功能使用 AI"
            "（問卷題目用語為 SI，原稱 AI）；比例分母含「不知道」回答。"
            "Sector 檔的 32.1% 為「XX」列，依 Census 資料字典僅指跨多部門或多州的多單位企業，"
            "並非全體，故與全國 24.6% 不矛盾，此處採 National")


def fetch_us(old):
    wb = read_xlsx(http_get(BTOS_NAT, timeout=120))
    rows = wb["Response Estimates"]
    hdr = rows[0]
    yes = next(r for r in rows[1:] if r.get("A") == "7" and r.get("D") == "Yes")
    fresh = {}
    for col, key in hdr.items():
        if len(col) > 0 and re.match(r"^\d{6}$", key or ""):
            v = pct(yes.get(col))
            if v is not None:
                fresh[btos_label(key)] = v
    if not fresh:
        raise RuntimeError("BTOS National 無 AI 題數值")
    merged = dict((p["period"], p["value"]) for p in (old or {}).get("series", []))
    merged.update(fresh)  # 不刪舊期（官方曾移除舊歷史）

    def sk(p):
        a = p.split(" ")
        return (int(a[0]), int(a[2]))
    series = [{"period": p, "value": merged[p]} for p in sorted(merged, key=sk)]
    latest = series[-1]
    return {"key": "US", "label": "美國", "value": latest["value"],
            "period": latest["period"] + "（雙週）", "scope": US_SCOPE,
            "src": BTOS_PAGE, "method": "auto", "series": series}


def fetch_us_info(old):
    wb = read_xlsx(http_get(BTOS_SEC, timeout=180))
    rows = wb["Response Estimates"]
    hdr = rows[0]
    r51 = next(r for r in rows[1:] if r.get("A") == "51" and r.get("B") == "7" and r.get("E") == "Yes")
    cols = sorted([c for c, k in hdr.items() if re.match(r"^\d{6}$", k or "")],
                  key=lambda c: -int(hdr[c]))
    for c in cols:
        v = pct(r51.get(c))
        if v is not None:
            return {"key": "US", "label": "美國", "sector": "資訊業（NAICS 51）", "value": v,
                    "period": btos_label(hdr[c]) + "（雙週）",
                    "scope": "BTOS 口徑：有員工企業、不分規模、過去兩週", "src": BTOS_SEC}
    raise RuntimeError("BTOS Sector 檔無 NAICS 51 數值")


OECD_URL = os.environ.get("INDICATORS_OECD_URL") or (
    "https://sdmx.oecd.org/public/rest/data/OECD.STI.DEP,DSD_ICT_B@DF_BUSINESSES,1.1/"
    "?startPeriod=2019&dimensionAtObservation=AllDimensions")
OECD_PAGE = "https://data-explorer.oecd.org/?lc=en&df[ds]=dsDisseminateFinalDMZ&df[id]=DSD_ICT_B%40DF_BUSINESSES"


def fetch_kr(old):
    req = Request(OECD_URL, headers={"User-Agent": BROWSER_UA,
                                     "Accept": "application/vnd.sdmx.data+csv;version=1.0.0"})
    with urlopen(req, timeout=300) as r:
        text = r.read().decode("utf-8")
    pts = []
    for row in csv.DictReader(io.StringIO(text)):
        if (row["REF_AREA"] == "KOR" and row["MEASURE"] == "G14_B" and row["ACTIVITY"] == "_T"
                and row["SIZE_CLASS"] == "S_GE10" and row["UNIT_MEASURE"] == "PT_ENT"):
            pts.append((int(row["TIME_PERIOD"]), float(row["OBS_VALUE"]), row["OBS_STATUS"]))
    if not pts:
        raise RuntimeError("OECD 無韓國 G14_B 資料")
    pts.sort()
    brk = [y for y, v, s in pts if s == "B"]
    start = max(brk) if brk else pts[0][0]  # 斷點（含）之後才是同一口徑
    pts = [p for p in pts if p[0] >= start]
    merged = dict((p["period"], p["value"]) for p in (old or {}).get("series", []))
    for y, v, s in pts:
        merged[str(y)] = round(v, 2)
    series = [{"period": k, "value": merged[k]} for k in sorted(merged)]
    return {"key": "KR", "label": "韓國", "value": series[-1]["value"], "period": series[-1]["period"],
            "scope": ("10 人以上企業（含金融）；當年度是否使用 AI；韓國 2022 年起調查方式改變"
                      "（OECD 標示斷點），故數列只放 2022 年之後"),
            "src": OECD_PAGE, "method": "auto", "series": series}


SG_URL = "https://tablebuilder.singstat.gov.sg/api/table/tabledata/M651551"
SG_PAGE = "https://tablebuilder.singstat.gov.sg/table/TS/M651551"


def fetch_sg(old):
    # SingStat 對沒有 Accept 標頭的請求回 403
    req = Request(SG_URL, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
    with urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"), strict=False)
    row = next(r for r in d["Data"]["row"] if r.get("rowText") == "Business Usage Of Artificial Intelligence")
    fresh = dict((c["key"], round(float(c["value"]), 2)) for c in row["columns"] if c.get("value") not in (None, "", "na"))
    if not fresh:
        raise RuntimeError("SingStat 無 AI 使用率數值")
    merged = dict((p["period"], p["value"]) for p in (old or {}).get("series", []))
    merged.update(fresh)
    series = [{"period": k, "value": merged[k]} for k in sorted(merged)]
    return {"key": "SG", "label": "新加坡", "value": series[-1]["value"], "period": series[-1]["period"],
            "scope": "全體企業（SingStat 定義，含小微）；年度調查，企業是否使用 AI（不限技術類型）",
            "src": SG_PAGE, "method": "auto", "series": series}


# 人工查證項目：腳本原樣保留不抓（值取自官方頁面／API，證據見 research/adoption_evidence.md）
MANUAL_REGIONS = [
    {"key": "CA", "label": "加拿大", "value": 19.2, "period": "2026 第 2 季",
     "scope": ("有員工的企業與組織，1 人以上不限規模（含非營利，私部門為 19.0%）；"
               "回顧過去 12 個月是否用 AI 生產商品或提供服務"),
     "src": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3310116701", "method": "manual",
     "series": [{"period": "2024 第 2 季", "value": 6.1}, {"period": "2025 第 2 季", "value": 12.2},
                {"period": "2026 第 2 季", "value": 19.2}]},
    {"key": "AU", "label": "澳洲", "value": 12, "period": "2024-25 財年",
     "scope": ("全體企業含微型（0–4 人）；財年度內是否使用 AI（為 ICT 選項之一，可複選）；"
               "雙年調查"),
     "src": "https://www.abs.gov.au/statistics/industry/technology-and-innovation/characteristics-australian-business/2024-25",
     "method": "manual",
     "series": [{"period": "2022-23 財年", "value": 1}, {"period": "2024-25 財年", "value": 12}]},
]
MANUAL_INFO = [
    {"key": "CA", "label": "加拿大", "sector": "資訊與文化產業（NAICS 51）", "value": 42.3,
     "period": "2026 第 2 季", "scope": "CSBC 口徑：1 人以上、過去 12 個月",
     "src": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3310116701"},
    {"key": "AU", "label": "澳洲", "sector": "資訊媒體與電信業", "value": 38,
     "period": "2024-25 財年", "scope": "ABS 口徑：全體企業含微型、財年度",
     "src": "https://www.abs.gov.au/statistics/industry/technology-and-innovation/characteristics-australian-business/2024-25"},
]


def keep_or_seed(old_items, seed):
    """人工項目：舊檔有就原樣保留，沒有才用腳本內建初始值。"""
    have = dict((i["key"], i) for i in old_items or [])
    return [have.get(s["key"], s) for s in seed]


def eu_region(eu):
    s = eu["series"]
    return {"key": "EU", "label": "歐盟", "value": s[-1]["value"], "period": str(s[-1]["year"]),
            "scope": ("EU27，10 人以上企業（不含農林漁、採礦、金融保險）；"
                      "過去 12 個月使用至少一種 AI 技術"),
            "src": EU_SRC, "method": "auto",
            "series": [{"period": str(p["year"]), "value": p["value"]} for p in s]}


def eu_info(ind):
    j = next((i for i in ind["items"] if i["code"] == "J"), None)
    if not j:
        raise RuntimeError("產業資料無 NACE J")
    return {"key": "EU", "label": "歐盟", "sector": "資訊及通訊業（NACE J）", "value": j["value"],
            "period": str(ind["year"]), "scope": "Eurostat 口徑：EU27，10 人以上企業、過去 12 個月",
            "src": ind["src"]}


def upsert(items, new):
    """用 key 取代同 key 項目，順序維持原樣。"""
    return [new if i["key"] == new["key"] else i for i in items]


# ---------- main ----------
def main():
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception as e:
            print("警告：舊檔無法解析（%s），視為空" % e)
    out = {"updated": dt.date.today().isoformat()}
    failures = []
    # csp_capex 改為每季人工查證填入（SEC API 需聯絡信箱，使用者決定不提供），本腳本只原樣保留
    if "csp_capex" in old:
        out["csp_capex"] = old["csp_capex"]
    old_regions = dict((i["key"], i) for i in (old.get("ai_adoption_regions") or {}).get("items", []))
    old_info = dict((i["key"], i) for i in (old.get("ai_adoption_info_sector") or {}).get("items", []))
    fresh_region = {}
    fresh_info = {}
    jobs = [
        ("tsmc_monthly", lambda: fetch_tsmc(old.get("tsmc_monthly")), "tsmc_monthly"),
        ("eu_ai_adoption", lambda: fetch_eu(old.get("eu_ai_adoption")), "eu_ai_adoption"),
        ("ai_adoption_industry", lambda: fetch_industry(old.get("ai_adoption_industry")),
         "ai_adoption_industry"),
        ("region.US", lambda: fetch_us(old_regions.get("US")), ("region", "US")),
        ("region.KR", lambda: fetch_kr(old_regions.get("KR")), ("region", "KR")),
        ("region.SG", lambda: fetch_sg(old_regions.get("SG")), ("region", "SG")),
        ("info_sector.US", lambda: fetch_us_info(old_info.get("US")), ("info", "US")),
    ]
    ok = 0
    for name, fn, dest in jobs:
        try:
            res = fn()
            ok += 1
            if isinstance(dest, tuple):
                (fresh_region if dest[0] == "region" else fresh_info)[dest[1]] = res
            else:
                out[dest] = res
        except Exception as e:
            msg = str(e).replace("\n", " ")[:200]
            failures.append("%s: %s" % (name, msg))
            print("警告：%s 失敗：%s" % (name, msg))
            if not isinstance(dest, tuple) and dest in old:
                out[dest] = old[dest]  # 保留舊值
    # 組裝各地區：EU 取自本次（或保留的）eu_ai_adoption，US/KR/SG 取本次或舊值，CA/AU 人工原樣保留
    regions = []
    if "eu_ai_adoption" in out:
        regions.append(eu_region(out["eu_ai_adoption"]))
    for k in ("US", "KR", "SG"):
        item = fresh_region.get(k) or old_regions.get(k)
        if item:
            regions.append(item)
    regions += keep_or_seed(list(old_regions.values()), MANUAL_REGIONS)
    out["ai_adoption_regions"] = {"items": regions}
    infos = []
    if "ai_adoption_industry" in out:
        try:
            infos.append(eu_info(out["ai_adoption_industry"]))
        except Exception as e:
            failures.append("info_sector.EU: %s" % str(e)[:200])
            if "EU" in old_info:
                infos.append(old_info["EU"])
    elif "EU" in old_info:
        infos.append(old_info["EU"])
    item = fresh_info.get("US") or old_info.get("US")
    if item:
        infos.append(item)
    infos += keep_or_seed(list(old_info.values()), MANUAL_INFO)
    out["ai_adoption_info_sector"] = {"note": "各地口徑不同，僅並列參考，不比高低", "items": infos}
    out["failures"] = failures
    if "csp_capex" in out:  # 維持慣用鍵序（csp_capex 在最後），避免 diff 雜訊
        out["csp_capex"] = out.pop("csp_capex")
    if ok == 0:
        print("錯誤：自動指標全部失敗")
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("已寫入 %s（成功 %d/%d，失敗 %d）" % (OUT, ok, len(jobs), len(failures)))


if __name__ == "__main__":
    main()
