# -*- coding: utf-8 -*-
"""
FINDME STORE 監視Bot（GitHub Actions版）

【自分のPC版との違い】
PC版は「無限ループで待ち続ける」プログラムでした。
こちらは「1回チェックして、終了する」プログラムです。
30分ごとに起動する役目は GitHub Actions が引き受けます。

【改訂点】★印のついた箇所が変更部分です。
  1. サーバーの一時的なエラー（500番台・429）を自動でやり直す（取得・通知の両方）
  2. 記憶には「Discordに伝え終わった状態」だけを保存する
     （通知に失敗した商品は次回もう一度検知される。成功した分は重複しない）
  3. 取得できた商品数が前回から急減したら、記憶を守るために中止する
  4. GitHub Actions上でWebhook URLが空なら、何もせず止まる

【記憶ファイルについて】
このプログラムは state.json を書いて終わるだけで、それを次回に引き継ぐ
（git commit する）のはワークフロー側の後続ステップの役目です。
「記憶を更新したくない場面」では state.json を書かずに sys.exit(1) しているので、
後続ステップがどう組まれていても壊れた記憶が残ることはありません。
"""

import json
import os
import sys
import time

import requests
from requests.adapters import HTTPAdapter   # ★追加
from urllib3.util.retry import Retry        # ★追加

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

# ★追加：前回より商品数がこの割合を下回ったら「取得が不完全」とみなして中止する
MIN_RATIO = 0.9
# ================================================================


def make_session():   # ★追加
    """やり直し機能つきの通信係を作る。

    500番台はサーバー側の一時的な不調、429は「送りすぎだから待て」という合図。
    どちらも少し待ってやり直せば通ります。待ち時間を倍々に伸ばすのは、
    混んでいる相手に追い打ちをかけないための作法です。
    429のときは相手が指定した待ち時間（Retry-Afterヘッダ）に従います。
    """
    session = requests.Session()
    session.headers.update(HEADERS)   # 毎回 headers= を書かなくて済みます
    retry = Retry(
        total=5,                                     # 最大5回までやり直す
        backoff_factor=2,                            # 2秒→4秒→8秒…と待つ（最長で約1分）
        status_forcelist=[429, 500, 502, 503, 504],  # やり直す対象のステータスコード
        allowed_methods=["GET", "POST"],             # 取得（GET）も通知（POST）も対象にする
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_products(session):
    """全商品を取得して {商品ID: 商品情報} の辞書にして返す。

    1回のリクエストで最大250件までしか返らないので、
    空のページが返るまで page を増やしながら読み進めます。
    """
    products = {}
    page = 1
    while True:
        res = session.get(   # ★変更：やり直し機能つきの session を使う
            f"{SHOP}/products.json",
            params={"limit": 250, "page": page},
            timeout=20,
        )
        # やり直しても直らなければ session.get の時点で例外になります。
        # 403や404のようにやり直し対象外のエラーは、ここで例外にします。
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


def notify(session, text):
    """Discordへ1件通知する。成功したら True、失敗したら False を返す。"""   # ★変更
    if not WEBHOOK_URL:
        print("  ※Webhook URLが未設定です。通知は送らず画面表示のみ。")
        print("  " + text.replace("\n", "\n  "))
        return True   # 手元での動作確認用。意図的な「表示のみ」モードなので成功扱い
    try:
        res = session.post(WEBHOOK_URL, json={"content": text}, timeout=15)
        # ★追加：Discordが404や401を返しても例外にはならないので、自分で確認する
        res.raise_for_status()
    except Exception as e:
        print(f"  通知の送信に失敗しました: {e}")
        return False
    time.sleep(1)  # 連投してスパム扱いされないように
    return True


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
    # ★追加：GitHub Actions上でSecretが空なら、走る前に止める。
    # Secret名のタイプミスに気づかないまま「通知ゼロ」で動き続けるのを防ぎます。
    if not WEBHOOK_URL and os.environ.get("GITHUB_ACTIONS"):
        print("【エラー】DISCORD_WEBHOOK_URL が空です。Secretsの名前を確認してください。")
        sys.exit(1)

    session = make_session()
    current = fetch_products(session)

    if not current:
        # 商品が0件＝サイト側の仕様変更やブロックの可能性。
        # ここで記憶を上書きすると次回に全商品が「新商品」として誤爆するので、
        # あえてエラー終了して記憶を守ります。
        print("【警告】商品が1件も取得できませんでした。記憶は更新しません。")
        sys.exit(1)

    known = load_state()

    # ★追加：途中のページが空で返ってループが早期終了した場合、
    # 縮んだ記憶を保存すると次回に何千件もの誤通知が飛びます。
    if known is not None and len(current) < len(known) * MIN_RATIO:
        print(
            f"【警告】商品数が急減しました（{len(known)}件 → {len(current)}件）。"
            "取得が不完全な可能性があるため、記憶は更新しません。"
        )
        sys.exit(1)

    if known is None:
        # 初回は基準を作るだけ。ここで通知すると全商品が飛んできてしまいます
        print(f"初回実行です。現在の状態を記憶しました（{len(current)}件）")
        save_state(current)
        return

    new_ids = current.keys() - known.keys()
    restocked_ids = {
        pid
        for pid in current.keys() & known.keys()
        if current[pid]["available"] and not known[pid]["available"]
    }

    notified = set()   # ★追加：今回Discordに伝えられた商品のID
    failed = 0

    for pid in new_ids:
        p = current[pid]
        print(f"【新商品】{p['title']}")
        if notify(session, f"🚨 **新商品が追加されました！**\n{p['title']}\n{p['url']}"):
            notified.add(pid)
        else:
            failed += 1

    for pid in restocked_ids:
        p = current[pid]
        print(f"【再販】{p['title']}")
        if notify(session, f"♻️ **再販されました！**\n{p['title']}\n{p['url']}"):
            notified.add(pid)
        else:
            failed += 1

    if not new_ids and not restocked_ids:
        print(f"変更なし（{len(current)}件）")

    # ★変更：記憶の更新ルール ＝「伝え終わった状態」だけを記憶する
    #   ・通知が要らなかった商品 → 今の状態をそのまま記憶（売り切れになった等も反映）
    #   ・通知できた商品         → 今の状態を記憶
    #   ・通知に失敗した再販     → 前回の「売り切れ」のまま記憶し、次回もう一度検知させる
    #   ・通知に失敗した新商品   → 記憶に入れず、次回もう一度「新商品」として検知させる
    next_state = {}
    for pid, info in current.items():
        needs_notice = pid in new_ids or pid in restocked_ids
        if needs_notice and pid not in notified:
            if pid in known:
                next_state[pid] = known[pid]
        else:
            next_state[pid] = info

    if failed:
        if not notified:
            # 全滅はWebhook自体が壊れている可能性が高いので、
            # 記憶を更新せずエラー終了してメールで知らせます
            print(f"【エラー】通知が{failed}件すべて失敗しました。Webhookを確認してください。")
            sys.exit(1)
        # 一部だけ失敗 → 失敗分は次回に再通知されるので、警告を残して正常終了します
        # （"::warning::" で始まる行は GitHub Actions の実行画面に注釈として表示されます）
        print(f"::warning::{failed}件の通知に失敗しました。次回の実行で再通知されます。")

    save_state(next_state)


if __name__ == "__main__":
    main()
