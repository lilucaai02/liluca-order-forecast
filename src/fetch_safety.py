#!/usr/bin/env python3
"""日次取得スクリプト共通の安全装置。

背景:
  外部API (SP-API / 楽天RMS / Yahooストア管理) の取得失敗を
  「販売0」「在庫0」として扱ってシートに書き込むと、正しい実績が
  0で上書きされて壊れる。実際に daily_amazon_sales.py で発生した
  (commit 8bb0620)。

このモジュールは各スクリプトが共通で使う:
  - is_retryable()      : 一時障害 (429 / 5xx / ネットワーク) の判定
  - retry_call()        : 指数バックオフ付きリトライ (初期5秒 → 最大60秒, 5回)
  - Pacer               : 呼び出し間隔の下限を保証するペーシング
  - sheets_retry()      : Sheets API (429/500/503/409/404) のリトライ
  - sheets_batch_update(): gspread の batch_update 専用リトライ

原則:
  「取得成功して0」と「取得失敗」は必ず区別すること。
  取得失敗の対象はシートに書き込まず、既存値を保持すること。
"""

from __future__ import annotations

import socket
import sys
import time
from typing import Any, Callable, Iterable

# --- ソケットのハング対策 ---------------------------------------------------
DEFAULT_SOCKET_TIMEOUT = 120.0  # 秒


def set_default_socket_timeout(seconds: float = DEFAULT_SOCKET_TIMEOUT) -> None:
    """全ソケットに読み取りタイムアウトを設定する。

    requests / gspread / google-auth は既定でタイムアウトを設定しないため、
    応答が返らない接続を掴むとプロセスが無限に待ち続ける。cron 実行では
    翌日の実行まで居座ってしまうので、必ずタイムアウトを入れて
    retry_call / sheets_retry のリトライに載せる。
    """
    socket.setdefaulttimeout(seconds)

# --- リトライ設定 (daily_amazon_sales.py と同一方針) ------------------------
MAX_ATTEMPTS = 5        # 初回 + リトライ4回
BACKOFF_START = 5.0     # 秒
BACKOFF_MAX = 60.0      # 秒

# 一時障害と判断するエラー文字列 (小文字で比較)
RETRYABLE_MARKERS = (
    "quotaexceeded",
    "429",
    "too many requests",
    "throttl",
    "rate limit",
    "500",
    "502",
    "503",
    "504",
    "internalerror",
    "internalfailure",
    "internal server error",
    "serviceunavailable",
    "service unavailable",
    "bad gateway",
    "gateway",
    "timeout",
    "timed out",
    "connection",
    "connectionreset",
    "temporarily",
    "remote end closed",
    "ssl",
)


def is_retryable(exc: BaseException) -> bool:
    """スロットリング/一時障害なら True。権限エラー等の恒久的失敗は False。"""
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(marker in msg for marker in RETRYABLE_MARKERS)


class RetryableStatus(RuntimeError):
    """HTTPステータスコードが一時障害だったことを示す例外。

    requests のように例外を投げず status_code を返すAPI呼び出しで、
    retry_call() にリトライさせたい場合に呼び出し側から送出する。
    """


def retry_call(
    fn: Callable[[], Any],
    label: str,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_start: float = BACKOFF_START,
    backoff_max: float = BACKOFF_MAX,
) -> Any:
    """fn() を指数バックオフでリトライする。

    max_attempts 回試しても駄目なら最後の例外をそのまま送出する。
    呼び出し側は必ず捕捉して「取得失敗」として記録すること
    (取得失敗を0として書き込んではいけない)。
    """
    delay = backoff_start
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 種別は is_retryable で判定
            retryable = isinstance(e, RetryableStatus) or is_retryable(e)
            if attempt >= max_attempts or not retryable:
                raise
            print(f"  {label} 一時エラー ({attempt}/{max_attempts - 1}) "
                  f"{delay:.0f}秒待機: {e}", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, backoff_max)


class Pacer:
    """前回呼び出しから min_interval 秒空けるペーシング。

    スロットリングを起こしてからリトライするより、そもそも起こさない方が速い。
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        if self._last:
            elapsed = time.monotonic() - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


# --- Google Sheets API 側のリトライ ----------------------------------------
SHEETS_RETRY_CODES = ("429", "500", "502", "503", "409", "404")


def sheets_retry(fn: Callable[..., Any], *args, **kwargs) -> Any:
    """Sheets API の一時エラーを指数バックオフでリトライ。

    対象:
      - APIError のうち 429 / 500 / 502 / 503 / 409 / 404
      - タイムアウト・接続断などのネットワーク系例外 (is_retryable で判定)

    注意: gspread の batch_update は渡した data の range をシート名付きに
    書き換える (破壊的変更) ため、同じリストを再利用してはいけない。
    batch_update には sheets_batch_update() を使うこと。
    """
    from gspread.exceptions import APIError

    delay = 30
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 下で種別を判定して再送出
            if isinstance(e, APIError):
                retryable = any(c in str(e) for c in SHEETS_RETRY_CODES)
            else:
                # requests / socket のタイムアウト・接続断など
                retryable = is_retryable(e)
            if not retryable or attempt >= 5:
                raise
            print(f"  [Sheets一時エラー] {delay}秒待機してリトライ "
                  f"({attempt + 1}/5): {str(e)[:120]}", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 120)


def sheets_batch_update(ws, chunk: Iterable[dict], **kwargs) -> Any:
    """batch_update 専用リトライ。毎試行で chunk のコピーを渡す。"""
    data = [dict(u) for u in chunk]
    return sheets_retry(lambda: ws.batch_update([dict(u) for u in data], **kwargs))
