#!/usr/bin/env python3
"""全球 AI 實際指標資料管線（只採官方原始來源，無需金鑰）。

多類自動抓取，任一類失敗不影響其他類，失敗時保留舊值：
  1. TSMC 月營收：TWSE OpenAPI（最新月）＋ MOPS 靜態頁（歷史回補）
  2. 四大雲端業者每季 CapEx：每季人工依官方財報查證填入，本腳本不抓、只保留
  3. Eurostat isoc_eb_ai：EU27、10 人以上企業使用 AI 比例
  4. 企業 AI 採用深化：Eurostat isoc_eb_ain2 產業排行；各地區（美 BTOS、日韓紐 OECD、星 SingStat 自動，
     加 StatCan、澳 ABS、中國統計局、印度世銀 人工查證原樣保留）；跨區資訊業（歐／美自動，加／澳人工）；
     各地區採用率前 5 產業（歐 Eurostat、美 BTOS Sector、日韓紐 OECD 自動，加／澳人工）

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
TSMC_SRC = "https://mopsov.twse.com.tw/nas/t21/sii/"
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


_CACHE = {}


def cached(name, fn):
    """同一次執行內共用下載結果（成功或失敗都只做一次）。"""
    if name not in _CACHE:
        try:
            _CACHE[name] = (True, fn())
        except Exception as e:
            _CACHE[name] = (False, e)
    ok, val = _CACHE[name]
    if not ok:
        raise val
    return val


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


def btos_sector_wb():
    return cached("btos_sector", lambda: read_xlsx(http_get(BTOS_SEC, timeout=180)))


def fetch_us_info(old):
    wb = btos_sector_wb()
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


def _oecd_download():
    req = Request(OECD_URL, headers={"User-Agent": BROWSER_UA,
                                     "Accept": "application/vnd.sdmx.data+csv;version=1.0.0"})
    with urlopen(req, timeout=300) as r:
        text = r.read().decode("utf-8")
    # 只留 G14_B（使用 AI 的企業比例，企業百分比）的列，供韓國總計與產業別共用
    return [row for row in csv.DictReader(io.StringIO(text))
            if row["MEASURE"] == "G14_B" and row["UNIT_MEASURE"] == "PT_ENT"]


def oecd_rows():
    return cached("oecd", _oecd_download)


def fetch_kr(old):
    pts = []
    for row in oecd_rows():
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


def _oecd_region(old, area, key, label, scope):
    """OECD G14_B：單一國家、10 人以上、全產業的完整年份數列（不做斷點截斷）。"""
    pts = []
    for row in oecd_rows():
        if (row["REF_AREA"] == area and row["MEASURE"] == "G14_B" and row["ACTIVITY"] == "_T"
                and row["SIZE_CLASS"] == "S_GE10" and row["UNIT_MEASURE"] == "PT_ENT"):
            pts.append((int(row["TIME_PERIOD"]), float(row["OBS_VALUE"])))
    if not pts:
        raise RuntimeError("OECD 無%s G14_B 資料" % label)
    merged = dict((p["period"], p["value"]) for p in (old or {}).get("series", []))
    for y, v in pts:
        merged[str(y)] = round(v, 2)
    series = [{"period": k, "value": merged[k]} for k in sorted(merged)]
    return {"key": key, "label": label, "value": series[-1]["value"], "period": series[-1]["period"],
            "scope": scope, "src": OECD_PAGE, "method": "auto", "series": series}


def fetch_jp(old):
    return _oecd_region(old, "JPN", "JP", "日本", (
        "OECD 資料庫轉載（原始調查來源 OECD 未標示）；10 人以上企業；當年度是否使用 AI；"
        "調查為隔年一次；OECD 標示日本此數列為 D（定義略有差異）"))


def fetch_nz(old):
    return _oecd_region(old, "NZL", "NZ", "紐西蘭", (
        "OECD 資料庫轉載（原始調查來源 OECD 未標示）；10 人以上企業；當年度是否使用 AI；"
        "目前僅 2022 年一期"))


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
    {"key": "CN", "label": "中國", "value": 16.4, "period": "2023",
     "scope": ("國家統計局第五次全國經濟普查；規模以上企業（148.2 萬家法人單位）；"
               "是否應用人工智能（企業可複選多項數位技術）；普查不定期，非逐年"),
     "src": "https://www.stats.gov.cn/sj/sjjd/202502/t20250227_1958814.html", "method": "manual",
     "series": [{"period": "2023", "value": 16.4}]},
    {"key": "IN", "label": "印度", "value": 23.4, "period": "2025–26 調查（世界銀行）",
     "scope": ("世界銀行企業調查（非印度政府官方統計）；正式企業 5 人以上，樣本 1,355 家，"
               "電話訪問；是否使用任何形式的 AI；世界銀行《南亞經濟更新》2026 年 10 月報告"),
     "src": ("https://openknowledge.worldbank.org/server/api/core/bitstreams/"
             "a3de4d6c-513c-462a-83a0-f8f946954a69/content"), "method": "manual",
     "series": [{"period": "2025–26", "value": 23.4}]},
]
MANUAL_INFO = [
    {"key": "CA", "label": "加拿大", "sector": "資訊與文化產業（NAICS 51）", "value": 42.3,
     "period": "2026 第 2 季", "scope": "CSBC 口徑：1 人以上、過去 12 個月",
     "src": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3310116701"},
    {"key": "AU", "label": "澳洲", "sector": "資訊媒體與電信業", "value": 38,
     "period": "2024-25 財年", "scope": "ABS 口徑：全體企業含微型、財年度",
     "src": "https://www.abs.gov.au/statistics/industry/technology-and-innovation/characteristics-australian-business/2024-25"},
]


# ---------- 4b. 各地區採用率前 5 產業 ----------
TOP_N = 5
BTOS_SEC_ZH = {
    "11": "農林漁牧業", "21": "採礦業", "22": "公用事業", "23": "營建業",
    "31": "製造業", "42": "批發業", "44": "零售業", "48": "運輸與倉儲",
    "51": "資訊業", "52": "金融保險業", "53": "不動產與租賃", "54": "專業、科學及技術服務",
    "55": "企業總管理", "56": "行政支援與廢棄物處理", "61": "教育服務",
    "62": "醫療保健與社會救助", "71": "藝術、娛樂與休閒", "72": "住宿與餐飲",
    "81": "其他服務業",
}
KR_ZH = {
    "C": "製造業", "F": "營建業", "G46": "批發業（不含汽機車）", "G47": "零售業（不含汽機車）",
    "H": "運輸與倉儲", "I": "住宿與餐飲", "J": "資訊及通訊業", "K": "金融保險業",
    "L": "不動產業", "M": "專業、科學及技術服務", "N": "行政支援服務",
}
EU_PAGE = "https://ec.europa.eu/eurostat/databrowser/view/isoc_eb_ain2/default/table"


def top_n(items):
    return sorted(items, key=lambda x: -x["value"])[:TOP_N]


def eu_industries(ind):
    rows = [i for i in ind["items"] if not i.get("agg")]
    if len(rows) < TOP_N:
        raise RuntimeError("EU 非彙總產業不足 %d 筆" % TOP_N)
    inds = []
    for i in top_n(rows):
        e = {"label": i["label"], "value": i["value"]}
        if i.get("prev") is not None:
            e["prev"] = i["prev"]
        inds.append(e)
    return {"key": "EU", "label": "歐盟", "period": str(ind["year"]),
            "short": "過去 12 個月・10 人以上", "scope": IND_SCOPE + "；排行已排除上層彙總類別",
            "src": EU_PAGE, "method": "auto", "industries": inds}


def us_industries(old):
    wb = btos_sector_wb()
    rows = wb["Response Estimates"]
    hdr = rows[0]
    cols = sorted([c for c, k in hdr.items() if re.match(r"^\d{6}$", k or "")],
                  key=lambda c: -int(hdr[c]))
    # Sector 代碼 XX 僅指跨部門／跨州多單位企業，非全體，排除；只取 Q7（AI 題）Yes 列
    q7 = dict((r["A"], r) for r in rows[1:]
              if r.get("B") == "7" and r.get("E") == "Yes" and r.get("A") in BTOS_SEC_ZH)
    cur = next((c for c in cols if sum(1 for r in q7.values() if pct(r.get(c)) is not None) >= 10),
               None)
    if cur is None:
        raise RuntimeError("BTOS Sector 檔 AI 題無足夠產業數值")
    nxt = cols.index(cur) + 1
    prev = cols[nxt] if nxt < len(cols) else None
    cand = []
    for code, r in q7.items():
        v = pct(r.get(cur))
        if v is None:
            continue
        e = {"label": BTOS_SEC_ZH[code], "value": v}
        # 不輸出 prev：雙週樣本波動大，與他地年度前期並列易被誤讀為年成長
        cand.append(e)
    return {"key": "US", "label": "美國", "period": btos_label(hdr[cur]) + "（雙週）",
            "short": "過去兩週・不分規模",
            "scope": ("BTOS Sector 檔（NAICS 2 位碼）；與全國檔同一題目（過去兩週是否在任何業務功能使用 AI，"
                      "題目用語為 SI，原稱 AI）、同一期別；有員工的非農業私部門企業，不分規模；"
                      "已排除「XX」列（跨部門多單位企業）；不列前期變化（雙週樣本波動大）"),
            "src": BTOS_SEC, "method": "auto", "industries": top_n(cand)}


def kr_industries(old):
    allrows = oecd_rows()
    pts = {}
    for row in allrows:
        if (row["REF_AREA"] == "KOR" and row["SIZE_CLASS"] == "S_GE10"
                and row["ACTIVITY"] in KR_ZH):
            pts.setdefault(int(row["TIME_PERIOD"]), {})[row["ACTIVITY"]] = (
                float(row["OBS_VALUE"]), row["OBS_STATUS"])
    ok_years = sorted(y for y, d in pts.items() if len(d) >= TOP_N)
    if not ok_years:
        raise RuntimeError("OECD 韓國產業別資料不足")
    cur = ok_years[-1]
    # 韓國 2022 起才是同一口徑（OECD 標斷點 B），前一期只在斷點（含）之後才採用
    brk = [int(r["TIME_PERIOD"]) for r in allrows if r["REF_AREA"] == "KOR"
           and r["ACTIVITY"] == "_T" and r["SIZE_CLASS"] == "S_GE10" and r["OBS_STATUS"] == "B"]
    start = max(brk) if brk else min(ok_years)
    cand = []
    for code, (v, st) in pts[cur].items():
        e = {"label": KR_ZH[code], "value": round(v, 2), "_st": st}
        pv = pts.get(cur - 1, {}).get(code)
        if pv and cur - 1 >= start:
            e["prev"] = round(pv[0], 2)
        cand.append(e)
    top = top_n(cand)
    flagged = [e["label"] for e in top if e["_st"] == "D"]
    for e in top:
        del e["_st"]
    scope = ("10 人以上企業（含金融）；當年度是否使用 AI；韓國 2022 年起調查方式改變"
             "（OECD 標示斷點），前一期僅在斷點之後才列")
    if flagged:
        scope += "；%s該年度 OECD 標為 D（定義與標準不同）" % "、".join(flagged)
    return {"key": "KR", "label": "韓國", "period": str(cur), "short": "年度・10 人以上",
            "scope": scope, "src": OECD_PAGE, "method": "auto", "industries": top}


def _oecd_industries(area, key, label, short, scope):
    """OECD G14_B 產業別前 5 名（10 人以上）；不放前期（調查非逐年，口徑未必連續）。"""
    pts = {}
    for row in oecd_rows():
        if (row["REF_AREA"] == area and row["SIZE_CLASS"] == "S_GE10"
                and row["ACTIVITY"] in KR_ZH):
            pts.setdefault(int(row["TIME_PERIOD"]), {})[row["ACTIVITY"]] = (
                float(row["OBS_VALUE"]), row["OBS_STATUS"])
    ok_years = sorted(y for y, d in pts.items() if len(d) >= TOP_N)
    if not ok_years:
        raise RuntimeError("OECD %s產業別資料不足" % label)
    cur = ok_years[-1]
    cand = [{"label": KR_ZH[c], "value": round(v, 2)} for c, (v, st) in pts[cur].items()]
    return {"key": key, "label": label, "period": str(cur), "short": short,
            "scope": scope, "src": OECD_PAGE, "method": "auto", "industries": top_n(cand)}


def jp_industries(old):
    return _oecd_industries("JPN", "JP", "日本", "隔年・10 人以上", (
        "OECD 資料庫轉載；10 人以上企業；當年度是否使用 AI；OECD 標示日本此數列為 D（定義略有差異）；"
        "調查隔年一次，不列前期變化"))


def nz_industries(old):
    return _oecd_industries("NZL", "NZ", "紐西蘭", "年度・10 人以上", (
        "OECD 資料庫轉載；10 人以上企業；當年度是否使用 AI；目前僅 2022 年一期，無前期可比"))


# 人工查證項目：值取自官方 API／頁面（證據見 research/adoption_evidence.md 第 8 節）
MANUAL_REGION_INDUSTRIES = [
    {"key": "CA", "label": "加拿大", "period": "2026 第 2 季", "short": "過去 12 個月・1 人以上",
     "scope": ("CSBC 表 33-10-1167-01，加拿大全國；有員工的企業與組織（含非營利），1 人以上不限規模；"
               "回顧過去 12 個月是否用 AI 生產商品或提供服務；prev 為 2025 第 2 季（表 33-10-1004-01，同題）"),
     "src": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3310116701", "method": "manual",
     "industries": [
         {"label": "資訊與文化產業", "value": 42.3, "prev": 35.6},
         {"label": "金融保險業", "value": 40.4, "prev": 30.6},
         {"label": "專業、科學及技術服務", "value": 32.4, "prev": 31.7},
         {"label": "醫療保健與社會救助", "value": 30.0, "prev": 17.4},
         {"label": "藝術、娛樂與休閒", "value": 25.3, "prev": 14.0}]},
    {"key": "AU", "label": "澳洲", "period": "2024-25 財年", "short": "財年度・含微型企業",
     "scope": ("ABS 企業特性調查 2024-25 資料表 BCSDC06 Table 2；全體企業含微型（0–4 人）；"
               "財年度內是否使用 AI（為 ICT 選項之一，可複選）；雙年調查，產業別無前期可比值"),
     "src": ("https://www.abs.gov.au/statistics/industry/technology-and-innovation/"
             "characteristics-australian-business/2024-25"),
     "method": "manual",
     "industries": [
         {"label": "資訊媒體與電信業", "value": 38.1},
         {"label": "金融保險業", "value": 24.1},
         {"label": "專業、科學及技術服務", "value": 24.1},
         {"label": "採礦業", "value": 17.8},
         {"label": "醫療保健與社會救助", "value": 17.4}]},
]


# 地區顯示順序：歐盟、美國、加拿大；亞太（日、韓、中、星、澳、紐）；最後印度
REGION_ORDER = ["EU", "US", "CA", "JP", "KR", "CN", "SG", "AU", "NZ", "IN"]


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
    old_rind = dict((i["key"], i) for i in (old.get("ai_adoption_region_industries") or {}).get("items", []))
    fresh_region = {}
    fresh_info = {}
    fresh_rind = {}
    jobs = [
        ("tsmc_monthly", lambda: fetch_tsmc(old.get("tsmc_monthly")), "tsmc_monthly"),
        ("eu_ai_adoption", lambda: fetch_eu(old.get("eu_ai_adoption")), "eu_ai_adoption"),
        ("ai_adoption_industry", lambda: fetch_industry(old.get("ai_adoption_industry")),
         "ai_adoption_industry"),
        ("region.US", lambda: fetch_us(old_regions.get("US")), ("region", "US")),
        ("region.KR", lambda: fetch_kr(old_regions.get("KR")), ("region", "KR")),
        ("region.SG", lambda: fetch_sg(old_regions.get("SG")), ("region", "SG")),
        ("region.JP", lambda: fetch_jp(old_regions.get("JP")), ("region", "JP")),
        ("region.NZ", lambda: fetch_nz(old_regions.get("NZ")), ("region", "NZ")),
        ("info_sector.US", lambda: fetch_us_info(old_info.get("US")), ("info", "US")),
        ("region_industries.US", lambda: us_industries(old_rind.get("US")), ("rind", "US")),
        ("region_industries.KR", lambda: kr_industries(old_rind.get("KR")), ("rind", "KR")),
        ("region_industries.JP", lambda: jp_industries(old_rind.get("JP")), ("rind", "JP")),
        ("region_industries.NZ", lambda: nz_industries(old_rind.get("NZ")), ("rind", "NZ")),
    ]
    ok = 0
    for name, fn, dest in jobs:
        try:
            res = fn()
            ok += 1
            if isinstance(dest, tuple):
                {"region": fresh_region, "info": fresh_info, "rind": fresh_rind}[dest[0]][dest[1]] = res
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
    for k in ("US", "KR", "SG", "JP", "NZ"):
        item = fresh_region.get(k) or old_regions.get(k)
        if item:
            regions.append(item)
    regions += keep_or_seed(list(old_regions.values()), MANUAL_REGIONS)
    regions.sort(key=lambda i: REGION_ORDER.index(i["key"]) if i["key"] in REGION_ORDER else 99)
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
    # 各地區採用率前 5 產業：歐盟取自本次產業排行，美／韓取本次或舊值，加／澳人工原樣保留
    rinds = []
    if "ai_adoption_industry" in out:
        try:
            rinds.append(eu_industries(out["ai_adoption_industry"]))
        except Exception as e:
            failures.append("region_industries.EU: %s" % str(e)[:200])
            if "EU" in old_rind:
                rinds.append(old_rind["EU"])
    elif "EU" in old_rind:
        rinds.append(old_rind["EU"])
    for k in ("US", "KR", "JP", "NZ"):
        item = fresh_rind.get(k) or old_rind.get(k)
        if item:
            rinds.append(item)
    rinds += keep_or_seed(list(old_rind.values()), MANUAL_REGION_INDUSTRIES)
    rinds.sort(key=lambda i: REGION_ORDER.index(i["key"]) if i["key"] in REGION_ORDER else 99)
    out["ai_adoption_region_industries"] = {"items": rinds}
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
