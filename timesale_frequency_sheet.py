#!/usr/bin/env python3
"""「タイムセール頻度」シートをダッシュボード２の右に作成する。

商品ごとに過去 (今日まで) のセール実施状況を集計:
  Amazon: アマゾンイベント長沼 の連続期間をカテゴリ分類してカウント
  楽天:   楽天イベント長沼 (5と0のつく日は除外した期間イベント)
  Yahoo:  Yahooイベント長沼 (5のつく日/ゾロ目の日は除外)

列: タブ / 商品 / Am回数 / Am直近 / Am平均間隔 / Am直近5回係数平均 /
    楽天回数 / 楽天直近 / Yahoo回数 / Yahoo直近
"""
import sys, os, time, datetime, statistics
sys.path.insert(0, "/Users/aililuca/amazon")
os.chdir("/Users/aililuca/amazon")
from config.settings import Settings
import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
import oshima_tab_blocks_config

def retry(fn, *a, **k):
    delay = 15
    for i in range(9):
        try: return fn(*a, **k)
        except APIError as e:
            if any(x in str(e) for x in ("429", "500", "503")) and i < 8:
                time.sleep(delay); delay = min(delay * 2, 150)
            else: raise

def col_letter(n):
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26); r = chr(65 + x) + r
    return r

BASE = datetime.date(1899, 12, 30)
TABS = ["マウスピース(在庫)", "DS-01 (在庫) ", "TG-01(在庫)", "TG-02(在庫)",
        "GC-01(在庫)", "GC-02(在庫)", "PCI-01", "WB-01(在庫)", "WB-02",
        "TS-01", "PG-01"]
RECUR = {"5と0のつく日", "5のつく日", "ゾロ目の日"}
CH_ROWS = {"Amazon": ("アマゾンイベント長沼", "アマゾンイベント係数長沼"),
           "楽天": ("楽天イベント長沼", None),
           "Yahoo": ("Yahooイベント長沼", None)}

s = Settings()
creds = Credentials.from_service_account_file(
    s.google_credentials_file,
    scopes=["https://www.googleapis.com/auth/spreadsheets"])
gc = gspread.authorize(creds)
sp = retry(gc.open_by_key, "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU")
today = datetime.date.today()

rows_out = []
for tab in TABS:
    ws = retry(sp.worksheet, tab)
    row1 = (retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE') or [[]])[0]
    c2d = {i: BASE + datetime.timedelta(days=int(v))
           for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    max_c = max(c2d)
    END = col_letter(max_c)
    col_a = retry(ws.col_values, 1)
    blocks = oshima_tab_blocks_config.get_blocks(tab)
    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    meta, ranges = [], []
    for bi, b in enumerate(blocks):
        lo, hi = bounds[bi], bounds[bi + 1]
        lab2row = {}
        for r in range(lo, hi):
            v = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
            if v and v not in lab2row:
                lab2row[v] = r
        rows = {}
        for ch, (evl, cfl) in CH_ROWS.items():
            rows[ch] = (lab2row.get(evl), lab2row.get(cfl) if cfl else None)
        meta.append((b["code"], rows))
        for ch in CH_ROWS:
            er, cr = rows[ch]
            ranges.append(f"C{er}:{END}{er}" if er else "A1:A1")
            ranges.append(f"C{cr}:{END}{cr}" if cr else "A1:A1")
    got = []
    for i in range(0, len(ranges), 15):
        got += retry(ws.batch_get, ranges[i:i + 15],
                     value_render_option='UNFORMATTED_VALUE')
    gi = 0
    for code, rows in meta:
        rec = {"tab": tab.replace("(在庫)", "").strip(), "code": code}
        for ch in CH_ROWS:
            ev = got[gi][0] if got[gi] else []
            cf = got[gi + 1][0] if got[gi + 1] else []
            gi += 2
            lab, coef = {}, {}
            for i, v in enumerate(ev):
                d = c2d.get(i + 3)
                if d and d <= today and isinstance(v, str) and v.strip():
                    if v.strip() not in RECUR:
                        lab[d] = v.strip()
            for i, v in enumerate(cf):
                d = c2d.get(i + 3)
                if d and isinstance(v, (int, float)):
                    coef[d] = float(v)
            periods = []
            cur = None
            for d in sorted(lab):
                if cur and (d - cur[1]).days == 1 and lab[d] == cur[2]:
                    cur[1] = d
                else:
                    cur = [d, d, lab[d]]
                    periods.append(cur)
            n = len(periods)
            rec[f"{ch}_n"] = n
            rec[f"{ch}_last"] = periods[-1][1].strftime("%Y/%m/%d") if n else ""
            if n >= 2:
                gaps = [(periods[i][0] - periods[i - 1][1]).days
                        for i in range(1, n)]
                rec[f"{ch}_gap"] = round(statistics.mean(gaps))
            else:
                rec[f"{ch}_gap"] = ""
            if ch == "Amazon" and n:
                cvals = []
                for d0, d1, name in periods[-5:]:
                    vs = [coef[d] for d in coef if d0 <= d <= d1]
                    if vs:
                        cvals.append(max(vs))
                rec["Amazon_coef"] = round(statistics.mean(cvals), 2) if cvals else ""
        rows_out.append(rec)
    print(f"{tab} 集計済み", flush=True)

# シート作成 (ダッシュボード２の右)
title = "タイムセール頻度"
try:
    old = retry(sp.worksheet, title)
    exists = True
except Exception:
    exists = False
idx = None
for i, w in enumerate(sp.worksheets()):
    if w.title == "ダッシュボード２":
        idx = i + 1
        break
if not exists:
    ws2 = retry(sp.add_worksheet, title=title, rows=80, cols=12, index=idx)
else:
    ws2 = old
    print("既存シートを更新")

hdr = ["タブ", "商品", "Amazonセール回数", "Amazon直近セール", "Amazon平均間隔(日)",
       "Amazon直近5回係数平均", "楽天セール回数", "楽天直近セール",
       "Yahooセール回数", "Yahoo直近セール", "更新日"]
data = [hdr]
for r in rows_out:
    data.append([r["tab"], r["code"], r["Amazon_n"], r["Amazon_last"],
                 r["Amazon_gap"], r.get("Amazon_coef", ""),
                 r["楽天_n"], r["楽天_last"], r["Yahoo_n"], r["Yahoo_last"],
                 today.strftime("%Y/%m/%d") if r is rows_out[0] else ""])
retry(ws2.update, range_name=f"A1:K{len(data)}", values=data,
      value_input_option='USER_ENTERED')
# ヘッダー書式 + 回数降順の把握用に太字
retry(sp.batch_update, {"requests": [
    {"repeatCell": {
        "range": {"sheetId": ws2.id, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.95},
            "textFormat": {"bold": True}}},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
    {"updateSheetProperties": {
        "properties": {"sheetId": ws2.id,
                       "gridProperties": {"frozenRowCount": 1,
                                          "frozenColumnCount": 2}},
        "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}},
]})
print(f"✅ {title} シート作成完了 ({len(rows_out)}商品)")
