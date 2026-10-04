"""Step3: パイロットで確定した処理(商品同一性判定・セット除外・JAN優先・
merchant_tiebreak・itemCode再検証)を、既にitem_code取得済みの5商品を除く
残り33商品へ適用する(一回限りの運用スクリプト)。Gemini APIは使用しない。
resolvedかつ再検証成功のみDB更新。not_found/verification_failedは更新しない。
"""

import json
import os

import psycopg2
import requests

import app  # noqa: F401
import product_collection_pipeline as pipeline

ALREADY_DONE_PRODUCT_IDS = {29, 1, 33, 35, 16}


def _fetch_targets():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT product_id, brand, name, category, jan_code, item_code, price_ref,
                   active_ingredients, formulation
            FROM product_master
            WHERE data_source = 'ai_precollected' AND (item_code IS NULL OR item_code = '')
            ORDER BY product_id
        """)
        rows = cur.fetchall()
    finally:
        conn.close()
    return [r for r in rows if r[0] not in ALREADY_DONE_PRODUCT_IDS]


def main():
    if not app.RAKUTEN_APP_ID or not app.RAKUTEN_ACCESS_KEY:
        print("[ABORT] RAKUTEN_APP_ID/RAKUTEN_ACCESS_KEY が設定されていません。")
        return

    targets = _fetch_targets()
    print(f"対象: {len(targets)}件(既にitem_code取得済みの5件は除外)\n", flush=True)

    request_log = []
    real_get = requests.get

    def counting_get(url, params=None, **kwargs):
        res = real_get(url, params=params, **kwargs)
        kind = "item_code_verify" if (params and "itemCode" in params) else "search"
        request_log.append({"kind": kind, "status_code": res.status_code})
        return res

    results = []
    requests.get = counting_get
    try:
        for (product_id, brand, name, category, jan_code, before_item_code, before_price,
             before_actives, before_formulation) in targets:
            before_count = len(request_log)
            try:
                resolution = pipeline.verify_and_resolve_item_code(
                    product_id, brand, name, category, jan_code=jan_code,
                )
            except Exception as e:
                resolution = {"status": "error", "reason": repr(e)}
            calls_this_product = request_log[before_count:]
            results.append({
                "product_id": product_id, "brand": brand, "name": name, "category": category,
                "jan_code": jan_code, "before_actives": before_actives, "before_formulation": before_formulation,
                "search_api_calls": len([c for c in calls_this_product if c["kind"] == "search"]),
                "verify_api_calls": len([c for c in calls_this_product if c["kind"] == "item_code_verify"]),
                "resolution": resolution,
            })
            print(f"[{len(results)}/{len(targets)}] {brand} / {name} -> {resolution.get('status')} "
                  f"({resolution.get('disambiguated_by')})", flush=True)
    finally:
        requests.get = real_get

    # ===== 集計 =====
    print("\n" + "=" * 70)
    print("=== 集計 ===")
    status_counts = {}
    disambig_counts = {}
    for r in results:
        st = r["resolution"].get("status")
        status_counts[st] = status_counts.get(st, 0) + 1
        if st == "resolved":
            db = r["resolution"].get("disambiguated_by") or "natural"
            disambig_counts[db] = disambig_counts.get(db, 0) + 1
    print(f"status内訳: {status_counts}")
    print(f"disambiguated_by内訳(resolvedのみ): {disambig_counts}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM product_master WHERE data_source='ai_precollected' AND item_code IS NOT NULL AND item_code != ''")
    total_with_item_code = cur.fetchone()[0]
    print(f"\nitem_code取得済み総数: {total_with_item_code}/38")

    cur.execute("""
        SELECT item_code, COUNT(*) FROM product_master
        WHERE data_source='ai_precollected' AND item_code IS NOT NULL AND item_code != ''
        GROUP BY item_code HAVING COUNT(*) > 1
    """)
    dup = cur.fetchall()
    print(f"item_code重複: {dup if dup else 'なし'}")

    print("\n--- 商品名レベルの誤紐付け監査(resolvedのみ全件) ---")
    misattribution_suspects = []
    for r in results:
        if r["resolution"].get("status") != "resolved":
            continue
        cur.execute(
            "SELECT brand, name, rakuten_title, item_code, price_ref, active_ingredients, formulation, jan_code "
            "FROM product_master WHERE product_id = %s", (r["product_id"],),
        )
        row = cur.fetchone()
        brand, name, rakuten_title, item_code, price_ref, actives, formulation, jan = row
        ingredients_ok = actives == r["before_actives"]
        formulation_ok = formulation == r["before_formulation"]
        jan_ok = jan == r["jan_code"]
        print(f"  [{r['category']}] {brand} / {name}")
        print(f"    楽天title: {rakuten_title}")
        print(f"    item_code={item_code} price={price_ref} "
              f"成分不変={ingredients_ok} formulation不変={formulation_ok} JAN不変={jan_ok}")
        if not (ingredients_ok and formulation_ok and jan_ok):
            misattribution_suspects.append((brand, name))

    print(f"\n成分/formulation/JAN変化が検出された件数: {len(misattribution_suspects)}")
    if misattribution_suspects:
        print(f"  {misattribution_suspects}")

    # price/URL/image取得率
    cur.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE price_ref IS NOT NULL AND price_ref > 0),
               COUNT(*) FILTER (WHERE last_known_rakuten_link IS NOT NULL AND last_known_rakuten_link != ''),
               COUNT(*) FILTER (WHERE last_known_image IS NOT NULL AND last_known_image != '')
        FROM product_master
        WHERE data_source='ai_precollected' AND item_code IS NOT NULL AND item_code != ''
    """)
    total_resolved_rows, with_price, with_url, with_image = cur.fetchone()
    print(f"\nprice取得率: {with_price}/{total_resolved_rows}")
    print(f"URL取得率: {with_url}/{total_resolved_rows}")
    print(f"image取得率: {with_image}/{total_resolved_rows}")

    conn.close()

    not_found_or_failed = [r for r in results if r["resolution"].get("status") != "resolved"]
    print(f"\n--- not_found/verification_failedの内訳 ---")
    for r in not_found_or_failed:
        print(f"  [{r['category']}] {r['brand']} / {r['name']}: "
              f"status={r['resolution'].get('status')} reason={r['resolution'].get('reason')}")

    all_search = [c for c in request_log if c["kind"] == "search"]
    all_verify = [c for c in request_log if c["kind"] == "item_code_verify"]
    errors = [c for c in request_log if c["status_code"] not in (200,)]
    print(f"\nAPIリクエスト総数: {len(request_log)} (search={len(all_search)}, item_code_verify={len(all_verify)})")
    print(f"429/その他エラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")


if __name__ == "__main__":
    main()
