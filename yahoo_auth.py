#!/usr/bin/env python3
"""
Yahoo!ショッピング OAuth 2.0 認証ヘルパー

使い方:
  1. .env に YAHOO_1_CLIENT_ID と YAHOO_1_CLIENT_SECRET を設定
  2. python3 yahoo_auth.py
  3. 表示されたURLをブラウザで開いて認証
  4. リダイレクト後のURLをコピーしてターミナルに貼り付け
  5. 取得したリフレッシュトークンを .env に保存

取得できるもの:
  - アクセストークン (1時間有効)
  - リフレッシュトークン (長期有効・在庫書き込みに使用)
"""

import os
import sys
import base64
import urllib.parse
import urllib.request
import json
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Yahoo OAuth 2.0 エンドポイント
YAHOO_AUTH_URL   = "https://auth.login.yahoo.co.jp/yconnect/v2/authorization"
YAHOO_TOKEN_URL  = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
REDIRECT_URI     = "http://localhost"

# Yahoo!ショッピング ストア管理スコープ
SCOPES = "openid"


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

    # クレデンシャルの取得
    client_id = os.environ.get("YAHOO_1_CLIENT_ID") or input("\nクライアントID: ").strip()
    client_secret = os.environ.get("YAHOO_1_CLIENT_SECRET") or input("クライアントシークレット: ").strip()

    if not client_id or not client_secret:
        print("エラー: クライアントIDとシークレットが必要です")
        print("\n.env に以下を設定してください:")
        print("  YAHOO_1_CLIENT_ID=your_client_id")
        print("  YAHOO_1_CLIENT_SECRET=your_client_secret")
        sys.exit(1)

    # 認証URLを表示
    auth_url = get_auth_url(client_id)
    print(f"\n以下のURLをブラウザで開いてYahoo! JAPANにログインしてください:")
    print(f"\n  {auth_url}\n")

    try:
        webbrowser.open(auth_url)
        print("(ブラウザが自動で開きます)")
    except Exception:
        print("(手動でブラウザに貼り付けてください)")

    print("\n認証後、リダイレクトされたURLを貼り付けてください")
    print("(例: http://localhost/?code=XXXXXXXX&...)")
    redirect_url = input("\nリダイレクトURL: ").strip()

    # URLから認証コードを抽出
    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)

    if "code" not in params:
        print("\nエラー: URLに code パラメータが見つかりません")
        print("URLを確認してください:", redirect_url)
        sys.exit(1)

    code = params["code"][0]
    print(f"\n認証コード取得: {code[:10]}...")

    # トークン取得
    print("アクセストークンを取得中...")
    try:
        tokens = exchange_code_for_tokens(client_id, client_secret, code)
    except Exception as e:
        print(f"\nエラー: トークン取得に失敗しました: {e}")
        sys.exit(1)

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)

    print("\n" + "=" * 60)
    print("✅ 認証成功！")
    print("=" * 60)
    print(f"\nアクセストークン (有効期限: {expires_in}秒):")
    print(f"  {access_token[:30]}...")

    if refresh_token:
        print(f"\nリフレッシュトークン:")
        print(f"  {refresh_token[:30]}...")
        print("\n以下を .env に追加してください:")
        print(f"\n  YAHOO_1_REFRESH_TOKEN={refresh_token}")
    else:
        print("\n※ リフレッシュトークンが発行されませんでした")
        print("  スコープに offline_access を追加するか、")
        print("  Yahoo!デベロッパーネットワークの設定を確認してください")

    print()


if __name__ == "__main__":
    main()
