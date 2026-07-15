#!/usr/bin/env python3
"""
Yahoo!ショッピング OAuth 2.0 認証ヘルパー

使い方:
  1. .env に YAHOO_1_CLIENT_ID と YAHOO_1_CLIENT_SECRET を設定済みであること
     （未設定なら先に python3 set_yahoo_creds.py で入れる）
  2. python3 yahoo_auth.py
  3. 表示URL（自動でブラウザも開く）で【ストアの管理者Yahoo ID】でログイン・認可
  4. 認可後 http://localhost/?code=... にリダイレクト（ページは開けなくてOK）
     → ブラウザのアドレスバーのURL全体をコピーしてターミナルに貼り付け
  5. refresh_token を取得し、.env に自動保存（画面には値を表示しません）
"""

import os
import re
import sys
import base64
import urllib.parse
import urllib.request
import json
import webbrowser
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Yahoo OAuth 2.0 エンドポイント
YAHOO_AUTH_URL   = "https://auth.login.yahoo.co.jp/yconnect/v2/authorization"
YAHOO_TOKEN_URL  = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
REDIRECT_URI     = "http://localhost"

# スコープ。Yahoo! ID連携v2 は openid で refresh_token が返る。
# 返らない場合は "openid offline_access" に変更して再実行。
SCOPES = "openid"

ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_env_value(key: str) -> str:
    """.env から key の値を読む（このスクリプト内だけで扱い、表示しない）。"""
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _save_env_value(key: str, value: str) -> None:
    """.env の key= 行を更新（無ければ追記）。値は表示しない。"""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    new_line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        sep = "" if text.endswith("\n") or text == "" else "\n"
        text = text + sep + new_line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def get_auth_url(client_id: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "bail": "1",
    }
    return f"{YAHOO_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str) -> dict:
    """認証コードをアクセストークン・リフレッシュトークンに交換."""
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        YAHOO_TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    print("=" * 60)
    print("Yahoo!ショッピング OAuth 2.0 認証ヘルパー")
    print("=" * 60)

    client_id = _load_env_value("YAHOO_1_CLIENT_ID")
    client_secret = _load_env_value("YAHOO_1_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("\nエラー: .env に YAHOO_1_CLIENT_ID / YAHOO_1_CLIENT_SECRET が必要です。")
        print("  → 先に  python3 set_yahoo_creds.py  で設定してください。")
        sys.exit(1)

    auth_url = get_auth_url(client_id)
    print("\n▼ 次のURLをブラウザで開き、")
    print("  【ストア(coconem-kktrading)の管理者Yahoo ID】でログイン・認可してください:")
    print(f"\n  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
        print("(ブラウザが自動で開きます)")
    except Exception:
        print("(手動でブラウザに貼り付けてください)")

    print("\n認可後、http://localhost/?code=... にリダイレクトされます（ページが開けなくてもOK）。")
    print("ブラウザのアドレスバーのURL全体をコピーして貼り付けてください。")
    redirect_url = input("\nリダイレクトURL: ").strip()

    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)
    if "code" not in params:
        print("\nエラー: URLに code パラメータが見つかりません:", redirect_url)
        sys.exit(1)

    code = params["code"][0]
    print("\n認可コードを取得しました。トークンに交換中...")
    try:
        tokens = exchange_code_for_tokens(client_id, client_secret, code)
    except Exception as e:
        print(f"\nエラー: トークン取得に失敗しました: {e}")
        print("  client_secret が正しいか、認可コードの期限切れ(数分)でないか確認してください。")
        sys.exit(1)

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)

    print("\n" + "=" * 60)
    if access_token:
        print(f"✅ アクセストークン取得成功（{len(access_token)}文字, 有効{expires_in}秒）")
    if refresh_token:
        _save_env_value("YAHOO_1_REFRESH_TOKEN", refresh_token)
        print(f"✅ リフレッシュトークン取得成功（{len(refresh_token)}文字）")
        print("   → .env の YAHOO_1_REFRESH_TOKEN に保存しました（値は非表示）")
        print("\n次に、AIに「refresh_token取れた」と伝えてください。")
    else:
        print("⚠ リフレッシュトークンが発行されませんでした。")
        print("  yahoo_auth.py の SCOPES を 'openid offline_access' に変えて再実行するか、")
        print("  認可したYahoo IDにストア管理権限があるか確認してください。")
    print("=" * 60)


if __name__ == "__main__":
    main()
