"""
楽天APIのitemCode検索(fetch_rakuten_item_by_item_code())を実環境で
検証するためのスタンドアロンスクリプト。

- 既存のRakuten API環境変数(.env経由のload_dotenv())をそのまま利用する。
- 本番診断フロー(api_create_diagnosis等)は一切呼ばない。fetch_rakuten_
  item_by_item_code()はこの検証専用関数であり、既存の診断フローからは
  呼ばれない。
- APIキー等の秘密情報は一切出力しない(fetch_rakuten_item_by_item_code()
  自体がリクエストパラメータを戻り値に含めない設計のため、ここでも
  戻り値をそのまま出力するだけで安全)。
- 出力は SUCCESS/FAILED と、失敗時のみ原因判断に必要な楽天レスポンス
  (http_status/rakuten_error)のみ。

実行方法(リポジトリルートから1コマンド):
    python3 verify_item_code_search.py
    python3 verify_item_code_search.py <別のitem_code>  # 任意で対象を変更可能
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402

DEFAULT_ITEM_CODE = "f252107-yasu:10000099"


def main():
    item_code = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ITEM_CODE
    result = app.fetch_rakuten_item_by_item_code(item_code)

    if result.get("ok"):
        print("SUCCESS")
    else:
        print("FAILED")
        print(f"http_status: {result.get('http_status')}")
        print(f"rakuten_error: {result.get('rakuten_error')}")


if __name__ == "__main__":
    main()
