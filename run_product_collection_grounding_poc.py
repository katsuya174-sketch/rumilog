"""
Phase 3「AIによる商品事前収集」の本実装前の最小技術検証(PoC)実行スクリプト。

- 実際にGemini API(Google Search Grounding + 厳格JSON出力)を呼び出す。
- 対象は最大3商品まで(大量収集は禁止、合意事項)。
- 診断パイプライン・product_masterには一切接続・書き込みしない
  (このスクリプトはDB接続・DATABASE_URLを一切使わない)。
- 既存の共通Geminiラッパー(call_gemini_with_retry、既存の使用量カウント・
  80%到達時管理者通知を引き継ぐ)をそのまま再利用する。

実行方法(リポジトリルートから1コマンド):
    python3 run_product_collection_grounding_poc.py
"""

import json
import time

import app
import product_collection_grounding_poc as poc

# 検証対象は最大3商品まで(合意事項)。実在し、公式情報が比較的豊富と
# 見込まれる有名製品を選定(収集品質の検証が目的のため、わざと難しい
# 商品を選ばない)。
TEST_PRODUCTS = [
    ("COSRX", "アドバンスドスネイルムチンエッセンス96"),
    ("ロート製薬", "肌ラボ 極潤ヒアルロン液"),
    ("SK-II", "フェイシャル トリートメント エッセンス"),
]


def run_one(brand, product_name):
    prompt = poc.build_product_collection_prompt(brand, product_name)
    config = poc.build_product_collection_config()

    print(f"\n===== {brand} {product_name} =====")
    t_start = time.time()
    try:
        response = app.call_gemini_with_retry(
            app.client,
            app.DETAIL_MODEL,
            prompt,
            config=config,
            max_retries=1,
            timeout=60,
        )
    except Exception as e:
        print(f"[RESULT] FAILED (exception): {repr(e)}")
        return {"brand": brand, "product_name": product_name, "ok": False, "error": repr(e)}

    elapsed = time.time() - t_start

    try:
        parsed = json.loads(response.text)
        parse_ok = True
    except Exception as e:
        parsed = None
        parse_ok = False
        print(f"[PARSE ERROR] {repr(e)}")
        print(f"[RAW TEXT] {getattr(response, 'text', '')[:500]}")

    usage = poc.extract_usage_summary(response)
    grounding = poc.extract_grounding_summary(response)

    print(f"[RESULT] {'SUCCESS' if parse_ok else 'FAILED'} elapsed={elapsed:.1f}s")
    print(f"[USAGE] {usage}")
    print(f"[GROUNDING] web_search_queries={grounding['web_search_queries']}")
    print(f"[GROUNDING] sources={grounding['sources']}")
    if parsed:
        print(f"[PARSED] {json.dumps(parsed, ensure_ascii=False, indent=2)[:1500]}")

    return {
        "brand": brand,
        "product_name": product_name,
        "ok": parse_ok,
        "usage": usage,
        "grounding": grounding,
        "parsed": parsed,
    }


def main():
    results = [run_one(brand, name) for brand, name in TEST_PRODUCTS]

    print("\n===== SUMMARY =====")
    for r in results:
        print(f"{r['brand']} {r['product_name']}: ok={r['ok']}")

    total_tokens = sum(
        (r.get("usage") or {}).get("total_token_count") or 0
        for r in results if r.get("ok")
    )
    print(f"\n合計total_token_count(成功分のみ): {total_tokens}")
    print("※1商品あたりの実費用は、上記usage(token数)と、Google AI Studioの")
    print("  現時点の公式料金表(モデル: " + app.DETAIL_MODEL + "、Grounding課金を含む)を")
    print("  照らし合わせて算出してください(料金は変動するため、このスクリプト")
    print("  自体では断定的な金額を計算しません)。")


if __name__ == "__main__":
    main()
