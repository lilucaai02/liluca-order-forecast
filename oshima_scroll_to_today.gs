/**
 * 発注予測 のコピー20260722大島 用
 * スプレッドシートを開いたときに、各商品タブで「今日の日付列」が
 * ビューポートの中央付近に来るようにスクロール位置を合わせる。
 *
 * このスクリプトは **スタンドアロン Apps Script プロジェクト** でも動作するように
 * SpreadsheetApp.openById(...) 方式で書いてある。
 *
 * ★セットアップ手順★
 *   1. Apps Script エディタでコード貼り付け → 保存 (Cmd+S)
 *   2. 関数プルダウンで `installOpenTrigger` を選択 → ▶実行
 *      → 権限承認ダイアログが出るので許可
 *      → シートを開いたときの自動発火トリガーが登録される
 *   3. 一度スプレッドシートをリロードして動作確認
 *
 * 動作テスト用:
 *   - `runOnceNow`  : 手動で1回全タブ実行 (即座に確認したい時)
 *   - `debugLog`    : Logger にどのタブで何列目が今日か出力
 */

const SPREADSHEET_ID = '1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU';

const TARGET_TABS = [
  'マウスピース(在庫)',
  'DS-01 (在庫) ',
  'GC-01(在庫)',
  'GC-02(在庫)',
  'TG-01(在庫)',
  'TG-02(在庫)',
  'PCI-01',
  'WB-01(在庫)',
  'WB-02',
  'TS-01',
  'PG-01',
];

// 中央寄せのため、今日の列より何列先を先にアクティブ化するか
const CENTER_OFFSET = 7;

/**
 * ★1回だけ実行してください★
 * スプレッドシートを開いたときに自動発火するトリガーを登録する。
 */
function installOpenTrigger() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  // 既存の同名トリガーを削除（重複防止）
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'scrollToTodayAllTabs') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('scrollToTodayAllTabs')
    .forSpreadsheet(ss)
    .onOpen()
    .create();
  Logger.log('✅ onOpen トリガーを登録しました');
}

/**
 * スプレッドシートを開いたときにトリガーから呼ばれる本体。
 * 全対象タブについて、今日の日付列を中央付近にスクロールする。
 */
function scrollToTodayAllTabs() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const originalActive = ss.getActiveSheet();
  const targetSerial = _todaySerial(ss);

  const results = [];
  TARGET_TABS.forEach(tabName => {
    const ws = ss.getSheetByName(tabName);
    if (!ws) { results.push(tabName + ': タブなし'); return; }
    const col = _findDateCol(ws, targetSerial);
    if (col < 3) { results.push(tabName + ': 日付列なし'); return; }
    _scrollTab(ss, ws, col);
    results.push(tabName + ': col=' + col);
  });

  ss.setActiveSheet(originalActive);
  SpreadsheetApp.flush();
  Logger.log('targetSerial=' + targetSerial + '\n' + results.join('\n'));
}

/**
 * 手動テスト用: いますぐ全タブに反映（アラート/UI無し）
 * 実行後、スプレッドシートのタブに戻って確認
 */
function runOnceNow() {
  scrollToTodayAllTabs();
}

/**
 * デバッグ用: 各タブで今日の列が何列目か Logger に出す
 */
function debugLog() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const targetSerial = _todaySerial(ss);
  const results = ['targetSerial=' + targetSerial];
  TARGET_TABS.forEach(tabName => {
    const ws = ss.getSheetByName(tabName);
    if (!ws) { results.push(tabName + ': タブなし'); return; }
    const col = _findDateCol(ws, targetSerial);
    results.push(tabName + ': ' + (col > 0 ? 'col=' + col : '日付列なし'));
  });
  const msg = results.join('\n');
  Logger.log(msg);
  console.log(msg);
}

/** 特定のタブで指定列を中央付近にスクロール */
function _scrollTab(ss, ws, col) {
  ss.setActiveSheet(ws);
  const rightCol = Math.min(col + CENTER_OFFSET, ws.getMaxColumns());
  ws.setActiveRange(ws.getRange(1, rightCol));
  ws.setActiveRange(ws.getRange(1, col));
}

/** 1行目のシリアル値から今日の列番号を探す。見つからなければ -1。 */
function _findDateCol(ws, targetSerial) {
  const lastCol = ws.getLastColumn();
  if (lastCol < 3) return -1;
  const row1 = ws.getRange(1, 1, 1, lastCol).getValues()[0];
  for (let i = 0; i < row1.length; i++) {
    const v = row1[i];
    let s = null;
    if (v instanceof Date) {
      s = _toSerial(v.getFullYear(), v.getMonth() + 1, v.getDate());
    } else if (typeof v === 'number') {
      s = Math.floor(v);
    }
    if (s === targetSerial) return i + 1;
  }
  return -1;
}

/** 今日のシリアル値（1899-12-30 起点）をシートのタイムゾーン基準で */
function _todaySerial(ss) {
  const tz = ss.getSpreadsheetTimeZone();
  const todayStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');
  const [y, m, d] = todayStr.split('-').map(Number);
  return _toSerial(y, m, d);
}

function _toSerial(y, m, d) {
  const MS_PER_DAY = 86400000;
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(1899, 11, 30)) / MS_PER_DAY);
}
