# -*- coding: utf-8 -*-
"""
FINDME STORE 監視Bot（GitHub Actions版）

【自分のPC版との違い】
PC版は「無限ループで待ち続ける」プログラムでした。
こちらは「1回チェックして、終了する」プログラムです。
30分ごとに起動する役目は GitHub Actions が引き受けます。
"""

import json
import os
import sys
import time

import requests

# ===== 設定 =====================================================
SHOP = "https://findmestore.thinkr.jp"

# Webhook URLはコードに書きません。GitHubのSecretsから受け取ります。
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
# ================================================================


def fetch_products():
    """全商品を取得して {商品ID: 商品情報} の辞書にして返す。

    1回のリクエストで最大250件までしか返らないので、
    空のページが返るまで page を増やしながら読み進めます。
    """
    products = {}
    page = 1
    while True:
        res = requests.get(
            f"{SHOP}/products.json",
            params={"limit": 250, "page": page},
            headers=HEADERS,
            timeout=20,
        )
        res.raise_for_status()
        items = res.json().get("products", [])
        if not items:  # 空のページ = 最後まで読み終えた
            break

        for p in items:
            # variants = サイズや色などの選択肢。1つでも在庫があれば「買える」とみなす
            available = any(v.get("available") for v in p.get("variants", []))
            products[str(p["id"])] = {
                "title": p.get("title", ""),
                "url": f"{SHOP}/products/{p.get('handle', '')}",
                "available": available,
            }

        page += 1
        time.sleep(1)  # サーバーに負荷をかけないための礼儀

    return products


def notify(text):
    """Discordへ1件通知する。"""
    if not WEBHOOK_URL:
        print("  ※Webhook URLが未設定です。通知は送らず画面表示のみ。")
        print("  " + text.replace("\n", "\n  "))
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": text}, timeout=15)
    except Exception as e:
        print(f"  通知の送信に失敗しました: {e}")
    time.sleep(1)  # 連投してスパム扱いされないように


def load_state():
    """前回の記憶を読み込む。無ければ None（＝初回）。"""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(products):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def main():
    current = fetch_products()

    if not current:
        # 商品が0件＝サイト側の仕様変更やブロックの可能性。
        # ここで記憶を上書きすると次回に全商品が「新商品」として誤爆するので、
        # あえてエラー終了して記憶を守ります。
        print("【警告】商品が1件も取得できませんでした。記憶は更新しません。")
        sys.exit(1)

    known = load_state()

    if known is None:
        # 初回は基準を作るだけ。ここで通知すると全商品が飛んできてしまいます
        print(f"初回実行です。現在の状態を記憶しました（{len(current)}件）")
    else:
        new_ids = current.keys() - known.keys()
        restocked_ids = [
            pid
            for pid in current.keys() & known.keys()
            if current[pid]["available"] and not known[pid]["available"]
        ]

        for pid in new_ids:
            p = current[pid]
            print(f"【新商品】{p['title']}")
            notify(f"🚨 **新商品が追加されました！**\n{p['title']}\n{p['url']}")

        for pid in restocked_ids:
            p = current[pid]
            print(f"【再販】{p['title']}")
            notify(f"♻️ **再販されました！**\n{p['title']}\n{p['url']}")

        if not new_ids and not restocked_ids:
            print(f"変更なし（{len(current)}件）")

    save_state(current)


if __name__ == "__main__":
    main()
