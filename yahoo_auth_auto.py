#!/usr/bin/env python3
"""
Yahoo OAuth 2.0 認証（全自動版）

localhost:80 に小さなHTTPサーバーを立て、Yahoo からのコールバックを直接受け取る。
手動でURLをコピペする必要なし。

使い方:
  sudo python3 yahoo_auth_auto.py
    ↑ ポート80を使うため sudo が必要（コールバックURLが http://localhost の場合）

  もしくは、コールバックURLに http://localhost:8765 を登録しているなら:
  python3 yahoo_auth_auto.py --port 8765

流れ:
  1. ローカルHTTPサーバー起動（デフォルト port 80）
  2. Yahoo認可URLをブラウザで開く
  3. Yahooで認可すると http://localhost/?code=... にリダイレクトされる
  4. サーバーが code を受け取り、自動でトークン交換 → .env に保存
"""

import argparse
import base64
import http.server
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

YAHOO_AUTH_URL   = "https://auth.login.yahoo.co.jp/yconnect/v2/authorization"
YAHOO_TOKEN_URL  = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
SCOPES = "openid"
ENV_PATH = Path(__file__).resolve().parent / ".env"

# 受け取り用グローバル（サーバー→メインへ受け渡し）
received = {"code": None, "error": None}


def _load_env_value(key: str) -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _save_env_value(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    new_line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        sep = "" if text.endswith("\n") or text == "" else "\n"
        text = text + sep + new_line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            received["code"] = params["code"][0]
            body = "<html><body><h1>✅ 認可コード受信成功</h1><p>ターミナルに戻ってください。このタブは閉じてOKです。</p></body></html>"
        elif "error" in params:
            received["error"] = params.get("error_description", [params["error"][0]])[0]
            body = f"<html><body><h1>❌ エラー</h1><p>{received['error']}</p></body></html>"
        else:
            body = "<html><body><h1>コールバック待機中...</h1><p>URLに code パラメータが含まれていません。</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass  # 静音化


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(
        YAHOO_TOKEN_URL, data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=80, help="ローカルポート (default: 80)")
    parser.add_argument("--timeout", type=int, default=180, help="待機秒数 (default: 180)")
    args = parser.parse_args()

    client_id = _load_env_value("YAHOO_1_CLIENT_ID")
    client_secret = _load_env_value("YAHOO_1_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("エラー: .env に YAHOO_1_CLIENT_ID / YAHOO_1_CLIENT_SECRET が必要", file=sys.stderr)
        sys.exit(1)

    redirect_uri = f"http://localhost" if args.port == 80 else f"http://localhost:{args.port}"

    # サーバー起動
    try:
        server = http.server.HTTPServer(("localhost", args.port), CallbackHandler)
    except PermissionError:
        print(f"❌ ポート{args.port}を使うには sudo が必要です:", file=sys.stderr)
        print(f"   sudo python3 yahoo_auth_auto.py --port {args.port}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"❌ ポート{args.port}が使えません: {e}", file=sys.stderr)
        print(f"   別のプロセスが使用中の可能性。lsof -i :{args.port} で確認してください。", file=sys.stderr)
        sys.exit(1)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"✓ ローカルサーバー起動: {redirect_uri}")

    # 認可URL構築
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "bail": "1",
    }
    auth_url = f"{YAHOO_AUTH_URL}?{urllib.parse.urlencode(auth_params)}"
    print(f"\n▼ 次のURLをブラウザで開いて認可してください（自動でも開きます）:")
    print(f"  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # コード受信待ち
    print(f"認可を待っています（最大{args.timeout}秒）...")
    for _ in range(args.timeout):
        if received["code"] or received["error"]:
            break
        time.sleep(1)
    server.shutdown()

    if received["error"]:
        print(f"\n❌ 認可エラー: {received['error']}", file=sys.stderr)
        sys.exit(1)
    if not received["code"]:
        print(f"\n❌ タイムアウト（{args.timeout}秒以内に認可されませんでした）", file=sys.stderr)
        sys.exit(1)

    print(f"\n✓ 認可コード受信")
    print(f"トークン交換中...")
    try:
        tokens = exchange_code_for_tokens(client_id, client_secret, received["code"], redirect_uri)
    except Exception as e:
        print(f"\n❌ トークン取得失敗: {e}", file=sys.stderr)
        sys.exit(1)

    at = tokens.get("access_token", "")
    rt = tokens.get("refresh_token", "")
    exp = tokens.get("expires_in", 3600)

    print()
    if at:
        print(f"✅ アクセストークン取得成功（{len(at)}文字, 有効{exp}秒）")
    if rt:
        _save_env_value("YAHOO_1_REFRESH_TOKEN", rt)
        print(f"✅ リフレッシュトークン取得成功（{len(rt)}文字）")
        print(f"   → .env の YAHOO_1_REFRESH_TOKEN に保存しました（値は非表示）")
    else:
        print("⚠ refresh_token が発行されませんでした。SCOPES を 'openid offline_access' に変更してください。")


if __name__ == "__main__":
    main()
