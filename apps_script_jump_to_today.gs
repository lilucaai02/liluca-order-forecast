/**
 * 商品タブを開く / 選択するたびに「今日の3列前」へ自動スクロールする。
 *
 * 【設置方法】
 *  1. スプレッドシートを開く
 *  2. メニュー「拡張機能」→「Apps Script」
 *  3. 既存コードを全部消して、このファイルの内容を貼り付け
 *  4. 保存（フロッピーアイコン or Ctrl+S）
 *  5. スプレッドシートに戻る
 *  → onOpen / onSelectionChange はシンプルトリガーなので、承認不要で動きます。
 *     （初回だけ承認を求められたら許可してください）
 *
 * これで「今日へ」ボタンは不要になります（B1の数式は削除してOK）。
 */

const TARGET_TABS = [
  "マウスピース(在庫)",
  "DS-01 (在庫) ",   // ← 末尾に半角スペースあり
  "GC-01(在庫)",
  "GC-02(在庫)",
  "TG-01(在庫)",
  "TG-02(在庫)",
  "PCI-01",
  "WB-01(在庫)",
  "WB-02",
  "TS-01",
  "PG-01",
];

const OFFSET = 3;  // 今日の何列前を左端に表示するか

/** 指定シートの「今日の列 - OFFSET」へアクティブセルを移動 */
function jumpToToday_(sheet) {
  const name = sheet.getName();
  if (TARGET_TABS.indexOf(name) === -1) return;

  const lastCol = sheet.getLastColumn();
  if (lastCol < 3) return;

  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  const now = new Date();
  const todayTime = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();

  let targetCol = -1;
  for (let i = 2; i < headers.length; i++) {  // C列(index2)以降
    const v = headers[i];
    if (Object.prototype.toString.call(v) === '[object Date]') {
      const d = new Date(v.getFullYear(), v.getMonth(), v.getDate()).getTime();
      if (d === todayTime) {
        targetCol = i + 1;  // 1始まりの列番号
        break;
      }
    }
  }
  if (targetCol === -1) return;

  const jumpCol = Math.max(1, targetCol - OFFSET);
  sheet.setActiveSelection(sheet.getRange(1, jumpCol));
}

/** スプレッドシートを開いたとき */
function onOpen(e) {
  jumpToToday_(SpreadsheetApp.getActiveSpreadsheet().getActiveSheet());
}

/** セル選択が変わったとき（＝タブを切り替えたとき）に発火 */
function onSelectionChange(e) {
  const sheet = e.source.getActiveSheet();
  const name = sheet.getName();

  const props = PropertiesService.getDocumentProperties();
  const last = props.getProperty('lastSheet');
  if (last === name) return;            // 同じシート内のセル移動では何もしない
  props.setProperty('lastSheet', name); // シートが切り替わった瞬間だけ実行
  jumpToToday_(sheet);
}
