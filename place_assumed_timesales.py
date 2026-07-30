#!/usr/bin/env python3
"""未来のセール(イベント)を「タイムセール仮」として仮置きする。

■ 目的
未来日にイベントが登録されていない商品は「セールをしない前提」で予測され、
需要を大幅に過小評価する (在庫残り日数・発注残り日数が実態より長く出る)。
そこで **未来のイベント実施率を、その商品の過去のイベント実施率に一致させる**。
これにより「未来の予測日販 ≒ 直近の実勢日販」に近づき、
残り日数と発注個数が同時に正しくなる。

■ 方式
  対象商品 : 過去180日にイベント (種類を問わない) が1日以上ある全商品
  実施率   : 過去180日のイベント日数 ÷ 180
  期間     : 過去のイベント期間の日数の中央値 (1〜14日にクランプ)
  係数     : 過去のイベント期間の係数の中央値 (種類を問わない/大型セールも含む)
  配置     : Amazon は未来を30日窓に区切り、窓ごとに
             必要日数 = 窓の日数 × 実施率 に対する不足分を等間隔で埋める。
             (全期間の合計だけを合わせると大型セールが集中する秋冬で足りて
              しまい、直近1〜2ヶ月が空のまま残る = 発注判断で最も危険な
              区間が過小評価されるため、窓ごとに合わせる)
             楽天・Yahoo は 5と0のつく日/ゾロ目の日/スーパーセール等が既に
             未来へ均等配置済みなので、未来実施率が過去実施率の9割を
             下回る場合だけ、全期間の不足分を等間隔で補う。
  既存イベント (大型セール等) が入っている日は絶対に上書きしない。
  過去日は一切触らない (未来日のみ操作)。

■ 冪等性
再実行時は既存の「タイムセール仮」を撤去してから引き直す。
実予定が入るほど仮置きが実データに置き換わっていく。

使い方:
  python3 place_assumed_timesales.py --tab "マウスピース(在庫)"
  python3 place_assumed_timesales.py --all
  python3 place_assumed_timesales.py --all --remove-only   # 仮置きを全撤去
"""

from __future__ import annotations

import argparse
import datetime
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
import oshima_tab_blocks_config

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)
HORIZON = datetime.date(2027, 3, 31)
LABEL = "タイムセール仮"
PALE = {"red": 0.85, "green": 0.93, "blue": 0.85}   # 仮置き (実セールより薄い緑)
BLACK = {"red": 0, "green": 0, "blue": 0}
WHITE = {"red": 1, "green": 1, "blue": 1}

LOOKBACK = 180        # 実施率を測る過去日数
WINDOW = 30           # Amazon の実施率をそろえる窓の日数
OTHER_TOL = 0.9       # 楽天/Yahoo は 過去実施率×これ を下回ったら補う
DUR_MIN, DUR_MAX = 1, 14
COEF_MAX = 10.0       # 係数中央値の上限 (実績欠損による異常値の暴走を防ぐ)

# (チャネル名, イベント行ラベル, 係数行ラベル, 窓方式か)
CHANNELS = [
    ("アマゾン", "アマゾンイベント", "アマゾンイベント係数", True),
    ("楽天", "楽天イベント", "楽天イベント係数", False),
    ("Yahoo", "Yahooイベント", "Yahooイベント係数", False),
]

TABS = ["マウスピース(在庫)", "DS-01 (在庫) ", "TG-01(在庫)", "TG-02(在庫)",
        "GC-01(在庫)", "GC-02(在庫)", "PCI-01", "WB-01(在庫)", "WB-02",
        "TS-01", "PG-01"]


def retry(fn, *a, **k):
    """Sheets API の一時エラー (429/500/502/503/409/404) を指数バックオフ。"""
    from gspread.exceptions import APIError
    delay = 20
    for i in range(9):
        try:
            return fn(*a, **k)
        except APIError as e:
            if (any(x in str(e) for x in ("429", "500", "502", "503", "409",
                                          "404"))
                    and i < 8):
                time.sleep(delay)
                delay = min(delay * 2, 180)
            else:
                raise
        except Exception as e:  # タイムアウト・接続断
            msg = f"{type(e).__name__} {e}".lower()
            if (any(x in msg for x in ("timeout", "timed out", "connection",
                                       "ssl", "remote end closed"))
                    and i < 8):
                time.sleep(delay)
                delay = min(delay * 2, 180)
            else:
                raise


def col_letter(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def to_periods(days: dict) -> list[list[datetime.date]]:
    """連続日+同一ラベルを1期間にまとめる。"""
    out, cur = [], None
    for d in sorted(days):
        if cur and (d - cur[1]).days == 1 and days[d][0] == days[cur[0]][0]:
            cur[1] = d
        else:
            cur = [d, d]
            out.append(cur)
    return out


def find_runs(cands: list[datetime.date], busy: set, need: int,
              dur: int) -> list[tuple]:
    """cands (連続した日付列) の中に、busy を避けて計 need 日を
    dur 日ずつのまとまりで等間隔に配置する。"""
    if need <= 0 or not cands:
        return []
    n = len(cands)
    k = max(1, -(-need // dur))            # 配置回数 (切り上げ)
    runs, placed, used = [], 0, set()
    starts = [min(n - 1, int((j + 0.5) * n / k)) for j in range(k)]
    for si in starts:
        if placed >= need:
            break
        want = min(dur, need - placed)
        # si から前方に空き日を探し、見つからなければ先頭から再走査
        order = list(range(si, n)) + list(range(0, si))
        i0 = next((i for i in order
                   if cands[i] not in busy and cands[i] not in used), None)
        if i0 is None:
            break
        run = []
        i = i0
        while i < n and len(run) < want:
            d = cands[i]
            if d in busy or d in used:
                break
            run.append(d)
            i += 1
        if not run:
            continue
        used.update(run)
        placed += len(run)
        runs.append((run[0], run[-1]))
    return runs


def plan_channel(lab: dict, coefs: dict, dates: list[datetime.date],
                 today: datetime.date, windowed: bool) -> dict:
    """1チャネル分の仮置き計画を返す。"""
    # 実施率は「実際に行われたイベント」だけで測る (仮置きは除外)
    past = {d: v for d, v in lab.items()
            if today - datetime.timedelta(days=LOOKBACK) <= d < today
            and LABEL not in v[0]}
    info = {"past_days": len(past), "rate": 0.0, "dur": 0, "coef": 0.0,
            "runs": [], "old": sorted(d for d, v in lab.items()
                                      if LABEL in v[0] and d > today),
            "fut_days": 0, "fut_existing": 0, "need": 0, "skip": ""}
    if not past:
        info["skip"] = "過去180日にイベントなし"
        return info
    rate = len(past) / LOOKBACK
    info["rate"] = rate

    periods = to_periods(past)
    lens = [(p1 - p0).days + 1 for p0, p1 in periods]
    dur = max(DUR_MIN, min(DUR_MAX, int(round(statistics.median(lens)))))
    pcoefs = []
    for p0, p1 in periods:
        vs = [coefs[d] for d in coefs if p0 <= d <= p1]
        if vs:
            pcoefs.append(max(vs))
    coef = round(statistics.median(pcoefs), 2) if pcoefs else 1.2
    if coef > COEF_MAX:      # 異常係数 (実績欠損等) の暴走を防ぐ
        info["skip"] = f"係数中央値{coef}が上限{COEF_MAX}超のため{COEF_MAX}に制限"
        coef = COEF_MAX
    info["dur"], info["coef"] = dur, coef
    if coef <= 0:
        info["skip"] = "過去の係数中央値が0以下"
        return info

    # 未来日 (明日〜HORIZON)。既存の仮置きは「空き」として扱う
    fut = [d for d in dates if today < d <= HORIZON]
    info["fut_days"] = len(fut)
    real = {d for d, v in lab.items() if LABEL not in v[0]}
    info["fut_existing"] = sum(1 for d in fut if d in real)

    if windowed:
        runs, need = [], 0
        i = 0
        while i < len(fut):
            win = fut[i:i + WINDOW]
            quota = int(round(len(win) * rate))
            have = sum(1 for d in win if d in real)
            short = quota - have
            if short > 0:
                need += short
                runs += find_runs(win, real, short, dur)
            i += WINDOW
        info["need"] = need
        info["runs"] = runs
    else:
        quota = int(round(len(fut) * rate))
        have = info["fut_existing"]
        fut_rate = have / len(fut) if fut else 0
        if fut_rate >= rate * OTHER_TOL:
            info["skip"] = (f"未来実施率{fut_rate:.3f} >= "
                            f"過去{rate:.3f}×{OTHER_TOL} → 補正不要")
        else:
            info["need"] = quota - have
            info["runs"] = find_runs(fut, real, quota - have, dur)
    return info


def rows_of_block(col_a, lo, hi) -> dict:
    out = {}
    for r in range(lo, hi):
        v = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
        if v and v not in out:
            out[v] = r
    return out


def process_tab(sp, tab, records, stats, remove_only=False, dry_run=False):
    ws = retry(sp.worksheet, tab)
    today = datetime.date.today()
    row1 = (retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
            or [[]])[0]
    c2d = {i: BASE + datetime.timedelta(days=int(v))
           for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    d2c = {d: c for c, d in c2d.items()}
    dates = sorted(c2d.values())
    END = col_letter(max(c2d))
    col_a = retry(ws.col_values, 1)
    blocks = oshima_tab_blocks_config.get_blocks(tab)
    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    print(f"[{tab}] {len(blocks)}ブロック", flush=True)

    values, reqs = [], []

    def fmt(row, c0, c1, bg, fg):
        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": c0 - 1,
                      "endColumnIndex": c1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg,
                "textFormat": {"foregroundColor": fg}}},
            "fields": ("userEnteredFormat(backgroundColor,"
                       "textFormat.foregroundColor)")}})

    for bi, b in enumerate(blocks):
        lo, hi = bounds[bi], bounds[bi + 1]
        rmap = rows_of_block(col_a, lo, hi)
        need_rows = [rmap[k] for _, e, c, _ in CHANNELS for k in (e, c)
                     if k in rmap]
        if not need_rows:
            continue
        # ブロック内の必要行をまとめて1回で読む (API呼び出し削減)
        r0, r1 = min(need_rows), max(need_rows)
        got_blk = retry(ws.get, f"C{r0}:{END}{r1}",
                        value_render_option='UNFORMATTED_VALUE')
        grid = {r0 + i: row for i, row in enumerate(got_blk)}

        for chname, evlab, cflab, windowed in CHANNELS:
            ev, cf = rmap.get(evlab), rmap.get(cflab)
            if not ev or not cf:
                continue
            evr = grid.get(ev, [])
            cfr = grid.get(cf, [])
            lab, coefs = {}, {}
            for i, v in enumerate(evr):
                d = c2d.get(i + 3)
                if d and isinstance(v, str) and v.strip():
                    lab[d] = (v.strip(),)
            for i, v in enumerate(cfr):
                d = c2d.get(i + 3)
                if d and isinstance(v, (int, float)):
                    coefs[d] = float(v)
            stale = [d for d, v in lab.items() if LABEL in v[0] and d <= today]
            if stale:
                print(f"  ! {b['code']}/{chname}: 過去日に仮置き{len(stale)}日 "
                      f"(過去日は変更しないため残置)", flush=True)

            if remove_only:
                info = {"old": sorted(d for d, v in lab.items()
                                      if LABEL in v[0] and d > today),
                        "runs": [], "rate": 0, "dur": 0, "coef": 0,
                        "past_days": 0, "fut_days": 0, "fut_existing": 0,
                        "need": 0, "skip": "remove-only"}
            else:
                info = plan_channel(lab, coefs, dates, today, windowed)

            new_days = set()
            for p0, p1 in info["runs"]:
                d = p0
                while d <= p1:
                    new_days.add(d)
                    d += datetime.timedelta(days=1)

            # 1) 不要になった仮置きを撤去
            drop = sorted(d for d in info["old"] if d not in new_days)
            for d in drop:
                c = d2c[d]
                values.append({"range": f"{col_letter(c)}{ev}",
                               "values": [[""]]})
                values.append({"range": f"{col_letter(c)}{cf}",
                               "values": [[1]]})
                fmt(ev, c, c, WHITE, BLACK)

            # 2) 仮置きを書き込み (先頭セルのみ黒文字、2日目以降は隠し文字)
            for p0, p1 in info["runs"]:
                c0, c1 = d2c[p0], d2c[p1]
                n = c1 - c0 + 1
                values.append({
                    "range": f"{col_letter(c0)}{ev}:{col_letter(c1)}{ev}",
                    "values": [[LABEL] * n]})
                values.append({
                    "range": f"{col_letter(c0)}{cf}:{col_letter(c1)}{cf}",
                    "values": [[info["coef"]] * n]})
                fmt(ev, c0, c1, PALE, PALE)
                fmt(ev, c0, c0, PALE, BLACK)
                records.append([tab.replace("(在庫)", "").strip(), b["code"],
                                chname, p0.strftime("%Y/%m/%d"),
                                p1.strftime("%Y/%m/%d"), n, info["coef"]])

            msg = (f"  {b['code']}/{chname}: 撤去{len(drop)}日 / "
                   f"仮置き{len(new_days)}日 ({len(info['runs'])}回, "
                   f"期間{info['dur']}日, 係数{info['coef']}, "
                   f"実施率{info['rate']:.3f}, 既存未来{info['fut_existing']}日"
                   f"/{info['fut_days']}日)")
            if info["skip"]:
                msg += f" [{info['skip']}]"
            print(msg, flush=True)
            stats.append({"tab": tab, "code": b["code"], "ch": chname,
                          "rate": round(info["rate"], 4), "dur": info["dur"],
                          "coef": info["coef"], "dropped": len(drop),
                          "placed": len(new_days), "runs": len(info["runs"]),
                          "fut_existing": info["fut_existing"],
                          "fut_days": info["fut_days"], "skip": info["skip"]})

    # まとめて書き込み (レンジ数を絞って API 呼び出しを削減)
    if dry_run:
        for r in records:
            if r[0] == tab.replace("(在庫)", "").strip():
                print(f"    仮置き予定 {r[1]}/{r[2]} {r[3]}〜{r[4]} "
                      f"({r[5]}日, 係数{r[6]})")
        print(f"[{tab}] dry-run: 値{len(values)}レンジ / 書式{len(reqs)}件 "
              f"(書き込みなし)", flush=True)
        return
    for i in range(0, len(values), 200):
        retry(ws.batch_update, [dict(x) for x in values[i:i + 200]],
              value_input_option='USER_ENTERED')
    for i in range(0, len(reqs), 200):
        retry(sp.batch_update, {"requests": reqs[i:i + 200]})
    print(f"[{tab}] 書き込み完了 (値{len(values)}レンジ / 書式{len(reqs)}件)",
          flush=True)


def write_index(sp, records):
    title = "タイムセール仮置き一覧"
    try:
        wsl = retry(sp.worksheet, title)
    except Exception:
        idx = next((i + 1 for i, w in enumerate(sp.worksheets())
                    if w.title == "タイムセール入力"), None)
        wsl = retry(sp.add_worksheet, title=title,
                    rows=max(50, len(records) + 10), cols=8, index=idx)
    retry(wsl.clear)
    today = datetime.date.today().strftime("%Y/%m/%d")
    hdr = [[f"タイムセール仮置き一覧 (過去180日の実施率に合わせて自動配置 / "
            f"更新: {today})", "", "", "", "", "", ""],
           ["タブ", "商品", "チャネル", "開始日", "終了日", "日数", "係数"]]
    data = hdr + records
    if wsl.row_count < len(data) + 5:
        retry(wsl.resize, rows=len(data) + 10)
    retry(wsl.update, range_name=f"A1:G{len(data)}", values=data,
          value_input_option='USER_ENTERED')
    retry(sp.batch_update, {"requests": [
        {"repeatCell": {
            "range": {"sheetId": wsl.id, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": {
                "backgroundColor": PALE, "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": wsl.id,
                           "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
    ]})
    print(f"一覧シート更新: {len(records)}件")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--remove-only", action="store_true",
                    help="「タイムセール仮」を全撤去して元に戻す")
    ap.add_argument("--dry-run", action="store_true",
                    help="計画だけ表示してシートは書き換えない")
    ap.add_argument("--stats-json", help="配置サマリーの出力先")
    args = ap.parse_args()
    if not args.all and not args.tab:
        ap.error("--tab か --all を指定してください")
    settings = Settings()
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = retry(gc.open_by_key, DEST_ID)
    tabs = TABS if args.all else [args.tab]
    records, stats = [], []
    for tab in tabs:
        process_tab(sp, tab, records, stats, remove_only=args.remove_only,
                    dry_run=args.dry_run)

    if args.all and not args.dry_run:
        write_index(sp, records)
    if args.stats_json:
        import json
        with open(args.stats_json, "w") as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=1)
    tot = sum(s["placed"] for s in stats)
    dro = sum(s["dropped"] for s in stats)
    print(f"合計: 撤去{dro}日 / 仮置き{tot}日")
    print("✅ 仮置き完了")


if __name__ == "__main__":
    main()
