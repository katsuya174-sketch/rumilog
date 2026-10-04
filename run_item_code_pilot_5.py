"""Step3: 選定済み5商品のみで実楽天APIパイロットを再実行する(一回限りの運用
スクリプト)。merchant_tiebreak導入後の2回目のパイロット。Gemini APIは使用
しない。resolvedのみproduct_masterへ保存される(verify_and_resolve_item_code
自体の設計により、not_found/verification_failedは一切書き込まれない)。
"""

import os

import psycopg2
import requests

import app  # noqa: F401
import product_collection_pipeline as pipeline

# select_item_code_pilot_candidates(count=5)で選定済みの5商品(product_id)。
TARGET_PRODUCT_IDS = [29, 1, 33, 35, 16]


def _fetch_targets():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        targets = []
        for product_id in TARGET_PRODUCT_IDS:
            cur.execute(
                "SELECT product_id, brand, name, category, jan_code, item_code, price_ref, "
                "active_ingredients, formulation "
                "FROM product_master WHERE product_id = %s AND data_source = 'ai_precollected'",
                (product_id,),
            )
            row = cur.fetchone()
            if not row:
                print(f"[WARNING] not found in product_master: product_id={product_id}")
                continue
            targets.append(row)
        return targets
    finally:
        conn.close()


def main():
    if not app.RAKUTEN_APP_ID or not app.RAKUTEN_ACCESS_KEY:
        print("[ABORT] RAKUTEN_APP_ID/RAKUTEN_ACCESS_KEY が設定されていません。")
        return

    targets = _fetch_targets()
    print(f"対象: {len(targets)}件\n")

    request_log = []
    real_get = requests.get

    def counting_get(url, params=None, **kwargs):
        res = real_get(url, params=params, **kwargs)
        kind = "item_code_verify" if (params and "itemCode" in params) else "search"
        request_log.append({
            "kind": kind, "status_code": res.status_code,
            "keyword": (params or {}).get("keyword"), "item_code": (params or {}).get("itemCode"),
        })
        return res

    results = []
    requests.get = counting_get
    try:
        for (product_id, brand, name, category, jan_code, before_item_code, before_price,
             before_actives, before_formulation) in targets:
            before_count = len(request_log)
            resolution = pipeline.verify_and_resolve_item_code(
                product_id, brand, name, category, jan_code=jan_code,
            )
            calls_this_product = request_log[before_count:]
            results.append({
                "product_id": product_id, "brand": brand, "name": name, "category": category,
                "jan_code": jan_code, "before_item_code": before_item_code, "before_price": before_price,
                "before_actives": before_actives, "before_formulation": before_formulation,
                "search_api_calls": len([c for c in calls_this_product if c["kind"] == "search"]),
                "verify_api_calls": len([c for c in calls_this_product if c["kind"] == "item_code_verify"]),
                "resolution": resolution,
            })
    finally:
        requests.get = real_get

    print("=" * 70)
    for r in results:
        res = r["resolution"]
        disambiguated_by = res.get("disambiguated_by") or ("natural" if res.get("status") == "resolved" else None)
        print(f"\n### [{r['category']}] {r['brand']} / {r['name']}")
        print(f"  検索API回数: {r['search_api_calls']} / itemCode再検証回数: {r['verify_api_calls']}")
        print(f"  初期候補数: {res.get('initial_candidate_count')}")
        print(f"  商品一致通過数(is_same_verified_rakuten_product): {res.get('title_matched_count')}")
        print(f"  セット除外後候補数: {res.get('single_item_count')}")
        print(f"  disambiguated_by: {disambiguated_by}")
        print(f"  最終status: {res.get('status')}")

        if res.get("status") == "resolved":
            print(f"  楽天title: {res.get('rakuten_title')}")
            print(f"  shop: {res.get('shop_name')}")
            print(f"  item_code: {res.get('item_code')}")
            print(f"  price: {res.get('price')}")
            print(f"  itemCode再検証http_status: {res.get('http_status')}")

            if disambiguated_by == "merchant_tiebreak":
                pool = res.get("tiebreak_pool") or []
                print(f"  --- merchant_tiebreak選定理由(候補{len(pool)}件、"
                      f"既存タイブレーク基準=スコア→レビュー数→画像有無→評価→価格) ---")
                for c in sorted(pool, key=lambda x: x["score"], reverse=True):
                    marker = " <= 選択" if c["item_code"] == res.get("item_code") else ""
                    print(f"    item_code={c['item_code']} shop={c['shop_name']} score={c['score']} "
                          f"reviews={c['review_count']} avg={c['review_average']} "
                          f"has_image={c['has_image']} price={c['price']}{marker}")
        else:
            print(f"  詳細: reason={res.get('reason')} http_status={res.get('http_status')}")

    # 実行後検証
    print("\n" + "=" * 70)
    print("=== 実行後検証 ===")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    resolved_count = sum(1 for r in results if r["resolution"].get("status") == "resolved")
    print(f"resolved率: {resolved_count}/{len(results)}")

    print("\n商品名レベルの一致確認:")
    item_codes_seen = []
    for r in results:
        res = r["resolution"]
        if res.get("status") != "resolved":
            continue
        cur.execute(
            "SELECT brand, name, rakuten_title, item_code, active_ingredients, formulation, jan_code "
            "FROM product_master WHERE product_id = %s", (r["product_id"],),
        )
        row = cur.fetchone()
        item_codes_seen.append(row[3])
        print(f"  {row[0]} / {row[1]}  <->  楽天title: {row[2]}")
        print(f"    成分不変: {row[4] == r['before_actives']}  formulation不変: {row[5] == r['before_formulation']}  "
              f"JAN不変: {row[6] == r['jan_code']}")

    conn.close()

    dup_codes = {c for c in item_codes_seen if item_codes_seen.count(c) > 1}
    print(f"\nitem_code重複の有無: {'あり: ' + str(dup_codes) if dup_codes else 'なし'}")

    all_search = [c for c in request_log if c["kind"] == "search"]
    all_verify = [c for c in request_log if c["kind"] == "item_code_verify"]
    errors = [c for c in request_log if c["status_code"] not in (200,)]
    print(f"\nAPIリクエスト総数: {len(request_log)} (search={len(all_search)}, item_code_verify={len(all_verify)})")
    print(f"429/その他エラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")

    print(f"\n前回(merchant_tiebreak導入前)との比較: 前回resolved=1/5 -> 今回resolved={resolved_count}/5")


if __name__ == "__main__":
    main()
