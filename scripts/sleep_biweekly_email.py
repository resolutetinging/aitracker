#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""睡眠兩週總覽信：下載公開的 sleep_v3_history.json，純規則計算（不用 LLM），寄 HTML 信。
任何失敗（下載、格式、資料不足、寄信）都印錯誤並 exit 非 0，讓 workflow 變紅。"""
import argparse
import html
import json
import math
import os
import smtplib
import sys
import time
import urllib.request


def rnd(x):
    """四捨五入（與網頁卡片 JS Math.round 一致；Python round 為銀行家捨入）"""
    return int(math.floor(x + 0.5))
from datetime import date, datetime, timedelta, timezone

URL = ('https://raw.githubusercontent.com/resolutetinging/'
       'sleep-dashboard/main/sleep_v3_history.json')
MIN_NIGHTS = 3
NOTE = 'Apple Watch 深睡判讀最不準，只看趨勢；非醫療診斷'


def fail(msg):
    print('錯誤：' + msg, file=sys.stderr)
    sys.exit(1)


def esc(s):
    return html.escape(str(s), quote=True)


# ---------- 載入與解析 ----------
def load_json(path):
    if path:
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            fail('讀取本機檔失敗：%s' % e)
    last = None
    for i in range(3):  # 首次 + 重試 2 次
        try:
            with urllib.request.urlopen(URL, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'), strict=False)
        except Exception as e:  # noqa: BLE001
            last = e
            print('下載失敗（第 %d 次）：%s' % (i + 1, e), file=sys.stderr)
            time.sleep(3)
    fail('下載睡眠資料失敗：%s' % last)


def parse_nights(data):
    if not isinstance(data, dict) or not isinstance(data.get('nights'), dict):
        fail('JSON 格式錯誤：缺少 nights 物件')
    out = []
    for k, v in data['nights'].items():
        try:
            d = date.fromisoformat(k)
            bt = datetime.fromisoformat(v['bedtime'])
            wk = datetime.fromisoformat(v['wake'])
            deep = float(v['deep_min'])
            rem = float(v['rem_min'])
            core = float(v['core_min'])
            awake = float(v.get('awake_min') or 0)
            eff = v.get('efficiency')
            eff = float(eff) if eff is not None else None
            if not all(math.isfinite(x) for x in (deep, rem, core, awake)) or (eff is not None and not math.isfinite(eff)):
                continue
        except (KeyError, TypeError, ValueError):
            continue  # 欄位不全的夜晚略過
        bm = bt.hour * 60 + bt.minute
        if bm > 720:
            bm -= 1440
        out.append({
            'date': d, 'sleep': deep + rem + core, 'bed': bm,
            'wake': wk.hour * 60 + wk.minute, 'deep': deep,
            'awake': awake, 'eff': eff,
        })
    out.sort(key=lambda n: n['date'])
    if len(out) < MIN_NIGHTS:
        fail('可用睡眠資料不足 %d 晚（目前 %d 晚）' % (MIN_NIGHTS, len(out)))
    return out


# ---------- 統計 ----------
def mean(xs):
    return sum(xs) / len(xs) if xs else None


def pstd(xs):
    if not xs:
        return None
    m = mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def longest_short_streak(ns):
    best = cur = 0
    prev = None
    for n in ns:
        if n['sleep'] < 360:
            cur = cur + 1 if prev and (n['date'] - prev).days == 1 else 1
            prev = n['date']
            best = max(best, cur)
        else:
            cur = 0
            prev = None
    return best


def stats(nights, start, end):
    ns = [n for n in nights if start <= n['date'] <= end]
    if not ns:
        return None
    sl = [n['sleep'] for n in ns]
    deep = [n['deep'] for n in ns]
    effs = [n['eff'] for n in ns if n['eff'] is not None]
    beds = [n['bed'] for n in ns]
    avg_sleep = mean(sl)
    return {
        'n': len(ns), 'start': start, 'end': end,
        'sleep': avg_sleep,
        'full7': sum(1 for x in sl if x >= 420),
        'short6': sum(1 for x in sl if x < 360),
        'bed': mean(beds), 'bed_std': pstd(beds),
        'bed_min': min(beds), 'bed_max': max(beds),
        'wake': mean([n['wake'] for n in ns]),
        'deep': mean(deep),
        'deep_pct': (sum(deep) / sum(sl) * 100) if sum(sl) else 0.0,
        'awake': mean([n['awake'] for n in ns]),
        'eff': mean(effs) if effs else None,
        'streak': longest_short_streak(ns),
    }


# ---------- 格式 ----------
def hhmm(m):
    m = int(rnd(m)) % 1440
    return '%02d:%02d' % (m // 60, m % 60)


def hours(m):
    return '%.1f 小時' % (m / 60)


def md(d):
    return '%d/%d' % (d.month, d.day)


def rng(a, b):
    return '%s–%s' % (md(a), md(b))


def f_or_dash(s, fn):
    return '–' if s is None else fn(s)


# ---------- 判斷 ----------
def judge(cur, prev):
    good, warn = [], []  # (主題, 句子)
    hd = ''
    if prev:
        d = cur['sleep'] - prev['sleep']
        if d >= 15:
            good.append(('睡眠時數', '平均睡著 %s，比前 7 天多 %d 分鐘'
                         % (hours(cur['sleep']), rnd(d))))
        elif d <= -15:
            warn.append(('睡眠時數', '平均睡著 %s，比前 7 天少 %d 分鐘'
                         % (hours(cur['sleep']), rnd(-d))))
    if cur['full7'] <= 2:
        warn.append(('睡滿 7 小時', '近 7 天只有 %d 晚睡滿 7 小時' % cur['full7']))
    if cur['short6'] >= 2 or cur['streak'] >= 2:
        warn.append(('短睡', '近 7 天有 %d 晚睡不到 6 小時，最長連續 %d 晚'
                     % (cur['short6'], cur['streak'])))
    if prev:
        bd = cur['bed'] - prev['bed']
        if bd <= -15:
            good.append(('入睡時間', '平均入睡 %s，比前 7 天提早 %d 分鐘'
                         % (hhmm(cur['bed']), rnd(-bd))))
        elif bd >= 15:
            warn.append(('入睡時間', '平均入睡 %s，比前 7 天延後 %d 分鐘'
                         % (hhmm(cur['bed']), rnd(bd))))
    if cur['bed_std'] > 30:
        warn.append(('入睡波動', '入睡時間波動 ±%d 分（最早 %s、最晚 %s）'
                     % (rnd(cur['bed_std']), hhmm(cur['bed_min']),
                        hhmm(cur['bed_max']))))
    elif prev and cur['bed_std'] < 20 and prev['bed_std'] > 30:
        good.append(('入睡波動', '入睡時間波動降到 ±%d 分（前 7 天 ±%d 分）'
                     % (rnd(cur['bed_std']), rnd(prev['bed_std']))))
    if cur['deep_pct'] < 10:
        warn.append(('深睡', '深睡占比 %.1f%%，偏低（手錶判讀僅供參考）'
                     % cur['deep_pct']))
    if prev and cur['deep_pct'] - prev['deep_pct'] >= 3:
        good.append(('深睡', '深睡占比 %.1f%%，比前 7 天高 %.1f 個百分點'
                     % (cur['deep_pct'], cur['deep_pct'] - prev['deep_pct'])))
    awake_up = prev and cur['awake'] - prev['awake'] >= 10
    low_eff = cur['eff'] is not None and cur['eff'] < 90
    if cur['awake'] > 30 or awake_up or low_eff:
        parts = ['夜間平均清醒 %d 分鐘' % rnd(cur['awake'])]
        if awake_up:
            parts.append('比前 7 天多 %d 分鐘' % rnd(cur['awake'] - prev['awake']))
        if cur['eff'] is not None:
            parts.append('效率 %.1f%%' % cur['eff'])
        warn.append(('夜間清醒', '，'.join(parts)))
    return good, warn, awake_up


def recommend(cur, warn_themes, awake_flag):
    recs = []
    if cur['bed'] > 30:
        target = cur['wake'] - 420 - cur['awake']
        target = int(rnd(target / 5.0) * 5)
        recs.append('你平均 %s 起床、夜裡約醒 %d 分鐘，'
                    '%s 前入睡才能睡滿 7 小時，'
                    '建議固定在這個時間前上床' % (hhmm(cur['wake']), rnd(cur['awake']), hhmm(target)))
    if cur['bed_std'] > 30:
        recs.append('固定上床時間，平日與假日盡量差在 30 分鐘內')
    if cur['streak'] >= 2:
        recs.append('連續短睡 %d 晚，優先補眠，先把今晚的入睡時間提早' % cur['streak'])
    if awake_flag:
        recs.append('夜間清醒變多，留意睡前咖啡、壓力與睡眠環境')
    recs = recs[:2]
    return recs or ['維持目前作息']


def conclusion(good, warn):
    g = '、'.join(dict.fromkeys(t for t, _ in good))
    w = '、'.join(dict.fromkeys(t for t, _ in warn))
    if g and w:
        return '這兩週有進步的是「%s」，需要注意的是「%s」。' % (g, w)
    if w:
        return '這兩週沒有明顯進步項目，需要注意的是「%s」。' % w
    if g:
        return '這兩週整體穩定，進步的是「%s」，沒有需要特別注意的項目。' % g
    return '這兩週睡眠大致持平，沒有明顯進步或需要注意的項目。'


# ---------- HTML ----------
TD = 'padding:6px 8px;border-bottom:1px solid #e8e2d6;font-size:13px;'


def table(headers, rows):
    h = ''.join('<th style="%stext-align:left;color:#6a6460;">%s</th>'
                % (TD, esc(x)) for x in headers)
    body = ''
    for r in rows:
        body += '<tr>' + ''.join(
            '<td style="%s">%s</td>' % (TD, esc(c)) for c in r) + '</tr>'
    return ('<table width="100%%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;"><tr>%s</tr>%s</table>' % (h, body))


def metric_cells(s):
    if s is None:
        return ['–'] * 10
    return [
        hours(s['sleep']), '%d 晚' % s['full7'], '%d 晚' % s['short6'],
        hhmm(s['bed']), '±%d 分' % rnd(s['bed_std']), hhmm(s['wake']),
        '%d 分（%.1f%%）' % (rnd(s['deep']), s['deep_pct']),
        '%d 分' % rnd(s['awake']),
        f_or_dash(s['eff'], lambda e: '%.1f%%' % e), '%d' % s['n'],
    ]


LABELS = ['平均時數', '睡滿 7h', '6h 以下', '平均入睡', '入睡波動',
          '平均起床', '深睡', '夜間清醒', '效率', '晚數']


def section(title, items):
    if items:
        lines = ''.join('<div style="margin:3px 0;">・%s</div>' % esc(x) for x in items)
    else:
        lines = '<div style="margin:3px 0;">無</div>'
    return ('<div style="margin-top:16px;font-weight:700;font-size:14px;'
            'color:#4a4540;">%s</div><div style="font-size:13px;'
            'color:#4a4540;">%s</div>' % (esc(title), lines))


def build(nights, today):
    L = nights[-1]['date']
    one = timedelta(days=1)
    cur = stats(nights, L - 6 * one, L)
    prev = stats(nights, L - 13 * one, L - 7 * one)
    total = stats(nights, L - 27 * one, L)
    weeks = [stats(nights, L - (7 * i + 6) * one, L - 7 * i * one) for i in range(4)]
    good, warn, awake_flag = judge(cur, prev)
    recs = recommend(cur, [t for t, _ in warn], awake_flag)
    concl = conclusion(good, warn)
    subject = '睡眠兩週總覽 %s' % rng(L - 13 * one, L)

    stale = ''
    if L < today - one:
        stale = ('<div style="background:#fbeed5;color:#7a5a10;padding:8px 10px;'
                 'font-size:13px;margin-bottom:12px;">最新睡眠資料停在 %s，'
                 '近期幾晚可能尚未同步</div>' % esc(md(L)))

    cols = [metric_cells(cur), metric_cells(prev), metric_cells(total)]
    rows = [[LABELS[i]] + [c[i] for c in cols] for i in range(len(LABELS))]
    t1 = table(['項目', '近 7 天 ' + rng(L - 6 * one, L),
                '前 7 天 ' + rng(L - 13 * one, L - 7 * one),
                '近 4 週 ' + rng(L - 27 * one, L)], rows)

    wrows = []
    for i, w in enumerate(weeks):
        label = rng(L - (7 * i + 6) * one, L - 7 * i * one)
        if w is None:
            wrows.append([label, '–', '–', '–', '–', '0'])
        else:
            wrows.append([label, hours(w['sleep']), '%d 晚' % w['full7'],
                          hhmm(w['bed']), '%d 分' % rnd(w['awake']), '%d' % w['n']])
    t2 = table(['週別', '平均時數', '睡滿 7h', '平均入睡', '夜間清醒', '晚數'], wrows)

    doc = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>%s</title></head>'
        '<body style="margin:0;padding:0;background:#faf6ee;">'
        '<table width="100%%" cellpadding="0" cellspacing="0"><tr><td align="center">'
        '<table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;'
        'background:#ffffff;font-family:-apple-system,Helvetica,Arial,sans-serif;">'
        '<tr><td style="padding:20px;color:#4a4540;">'
        '%s'
        '<div style="font-size:18px;font-weight:700;">%s</div>'
        '<div style="margin:10px 0 16px;font-size:14px;line-height:1.6;">%s</div>'
        '%s'
        '<div style="margin-top:20px;font-weight:700;font-size:14px;">近 4 週逐週</div>'
        '%s%s%s%s'
        '<div style="margin-top:20px;font-size:11px;color:#9e9890;">%s</div>'
        '</td></tr></table></td></tr></table></body></html>'
    ) % (esc(subject), stale, esc(subject), esc(concl), t1, t2,
         section('有進步', [s for _, s in good]),
         section('需要注意', [s for _, s in warn]),
         section('建議', recs), esc(NOTE))
    return subject, doc, cur, prev


# ---------- 寄信 ----------
def send_email(subject, body):
    from email.header import Header
    from email.mime.text import MIMEText
    clean = lambda s: os.environ.get(s, '').replace('\xa0', '').replace(' ', '').strip()  # noqa: E731
    user = clean('GMAIL_USER')
    pwd = clean('GMAIL_APP_PASSWORD')
    to = clean('NOTIFY_EMAIL') or user
    rcpt = [a.strip() for a in to.split(',') if a.strip()]
    if not user or not pwd or not rcpt:
        fail('寄信設定缺少（GMAIL_USER / GMAIL_APP_PASSWORD / NOTIFY_EMAIL）')
    msg = MIMEText(body, 'html', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = user
    msg['To'] = ', '.join(rcpt)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=60) as s:
            s.login(user, pwd)
            s.sendmail(user, rcpt, msg.as_string())
    except Exception as e:  # noqa: BLE001
        fail('寄信失敗：%s' % e)
    print('已寄出：%s → %d 位收件人' % (subject, len(rcpt)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--history', help='改讀本機 JSON')
    ap.add_argument('--dry-run', action='store_true', help='只輸出 HTML 不寄')
    ap.add_argument('--out', help='dry-run 輸出路徑')
    ap.add_argument('--today', help='測試用，台北今天 YYYY-MM-DD')
    a = ap.parse_args()
    if a.today:
        try:
            today = date.fromisoformat(a.today)
        except ValueError:
            fail('--today 格式需為 YYYY-MM-DD')
    else:
        today = datetime.now(timezone(timedelta(hours=8))).date()
    nights = parse_nights(load_json(a.history))
    subject, doc, cur, prev = build(nights, today)
    print('主旨：%s（錨點 %s，近 7 共 %d 晚）' % (subject, nights[-1]['date'], cur['n']))
    if a.dry_run:
        if not a.out:
            fail('--dry-run 需搭配 --out 路徑')
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(doc)
        print('已輸出 HTML：%s' % a.out)
        return
    send_email(subject, doc)


if __name__ == '__main__':
    main()
