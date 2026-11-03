#!/usr/bin/env python3
"""全球 AI 實際指標資料管線（只採官方原始來源，無需金鑰）。

兩類自動抓取（TSMC、Eurostat），任一類失敗不影響其他類，失敗時保留舊值：
  1. TSMC 月營收：TWSE OpenAPI（最新月）＋ MOPS 靜態頁（歷史回補）
  2. 四大雲端業者每季 CapEx：每季人工依官方財報查證填入，本腳本不抓、只保留
  3. Eurostat isoc_eb_ai：EU27、10 人以上企業使用 AI 比例

輸出 data/global_indicators.json。兩類全失敗 exit 1，部分失敗 exit 0 並印警告。
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
    jobs = [("tsmc_monthly", fetch_tsmc), ("eu_ai_adoption", fetch_eu)]
    ok = 0
    for key, fn in jobs:
        try:
            out[key] = fn(old.get(key))
            ok += 1
        except Exception as e:
            msg = str(e).replace("\n", " ")[:200]
            failures.append("%s: %s" % (key, msg))
            print("警告：%s 失敗：%s" % (key, msg))
            if key in old:
                out[key] = old[key]  # 保留舊值
    out["failures"] = failures
    if ok == 0:
        print("錯誤：自動指標全部失敗")
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("已寫入 %s（成功 %d/2，失敗 %d）" % (OUT, ok, len(failures)))


if __name__ == "__main__":
    main()
