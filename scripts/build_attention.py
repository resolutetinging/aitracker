#!/usr/bin/env python3
"""新聞關注度趨勢：把 history.json 的卡片依 ISO 週彙整成 data/attention_trend.json。

用法：
  python scripts/build_attention.py             # 每日：合併現有 history.json（冪等）
  python scripts/build_attention.py --backfill  # 從 git 歷史逐日重建
去重 key = 卡片所屬日期 + "|" + 標題；source 為空字串的是佔位卡，排除。
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"
TREND = ROOT / "data" / "attention_trend.json"
ALIASES = ROOT / "scripts" / "attention_aliases.json"
SINCE = "2026-07-01"
SEEN_DAYS = 21
DIMS = ("hw", "corp", "app")
RATINGS = ("core", "opp", "noise")


def load_entities():
    data = json.loads(ALIASES.read_text(encoding="utf-8"))
    ents = []
    for e in data["entities"]:
        pats = []
        for a in e["aliases"]:
            if re.fullmatch(r"[\x20-\x7e]+", a):
                flags = 0 if len(a) <= 4 else re.I
                body = re.escape(a)
                if len(a) > 4:
                    body += "(?:s|es)?"
                pats.append(re.compile(
                    r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", flags))
            else:
                pats.append(re.compile(re.escape(a), re.I))
        excl = [re.compile(re.escape(x), re.I) for x in e.get("exclude", [])]
        ents.append((e, pats, excl))
    return ents


def mentions_of(text, ents):
    hit = []
    for e, pats, excl in ents:
        t = text
        for x in excl:
            t = x.sub(" ", t)
        if any(p.search(t) for p in pats):
            hit.append(e["key"])
    return hit


def iso_week(d):
    y, w, _ = dt.date.fromisoformat(d).isocalendar()
    monday = dt.date.fromisoformat(d) - dt.timedelta(days=dt.date.fromisoformat(d).weekday())
    return "%d-W%02d" % (y, w), monday.isoformat()


def iter_cards(history):
    """產出 (date, dim, card)；排除佔位卡與 SINCE 之前。"""
    if not isinstance(history, list):
        return
    for day in history:
        if not isinstance(day, dict):
            continue
        d = day.get("date")
        if not d or d < SINCE:
            continue
        for dim in DIMS:
            for c in day.get(dim) or []:
                if not isinstance(c, dict) or not c.get("source"):
                    continue
                if not c.get("title"):
                    continue
                yield d, dim, c


def add_cards(state, history, ents, seen_set):
    """把 history 的新卡片加進 state['weeks']；回傳新增張數。"""
    weeks = {w["week"]: w for w in state["weeks"]}
    added = 0
    for d, dim, c in iter_cards(history):
        key = d + "|" + c["title"]
        if key in seen_set:
            continue
        seen_set.add(key)
        wk, start = iso_week(d)
        w = weeks.get(wk)
        if w is None:
            w = {"week": wk, "start": start, "cards": 0,
                 "dims": {k: {r: 0 for r in RATINGS} for k in DIMS},
                 "mentions": {}}
            weeks[wk] = w
        w["cards"] += 1
        r = c.get("rating")
        if r in RATINGS:
            w["dims"][dim][r] += 1
        text = (c.get("title") or "") + "\n" + (c.get("body") or "")
        for k in mentions_of(text, ents):
            w["mentions"][k] = w["mentions"].get(k, 0) + 1
        added += 1
    state["weeks"] = sorted(weeks.values(), key=lambda x: x["week"])
    return added


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT)] + list(args), check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def backfill_histories():
    """每日取最後一個 commit 的 history.json 版本，依日期由舊到新。"""
    out = git("log", "--format=%H %cs", "--", "data/history.json")
    per_day = {}
    for line in out.splitlines():
        h, d = line.split()
        per_day.setdefault(d, h)  # log 由新到舊，第一個即當天最後一個
    for d in sorted(per_day):
        try:
            raw = git("show", per_day[d] + ":data/history.json")
            yield d, json.loads(raw, strict=False)
        except Exception as ex:  # 某版本壞檔就跳過
            print("skip %s: %s" % (d, ex), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()
    data = json.loads(ALIASES.read_text(encoding="utf-8"))
    ents = load_entities()
    state = {"updated": "", "since": SINCE,
             "entities": [{"key": e["key"], "label": e["label"], "type": e["type"]}
                          for e in data["entities"]],
             "weeks": [], "seen": []}
    seen = set()
    card_dates = []
    if args.backfill:
        n = 0
        for _day, hist in backfill_histories():
            n += add_cards(state, hist, ents, seen)
        print("backfill cards:", n)
    else:
        if TREND.exists():
            old = json.loads(TREND.read_text(encoding="utf-8"))
            state["weeks"] = old.get("weeks", [])
            seen = set(old.get("seen", []))
        hist = json.loads(HISTORY.read_text(encoding="utf-8"), strict=False)
        n = add_cards(state, hist, ents, seen)
        print("new cards:", n)
    latest = max([k.split("|", 1)[0] for k in seen] or [SINCE])
    cutoff = (dt.date.fromisoformat(latest) - dt.timedelta(days=SEEN_DAYS)).isoformat()
    state["seen"] = sorted(k for k in seen if k.split("|", 1)[0] >= cutoff)
    state["updated"] = latest
    TREND.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")


if __name__ == "__main__":
    main()
