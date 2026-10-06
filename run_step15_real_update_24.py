"""Step15: Step14で検証済みの24件(resolved21件 + メラノCC/ミノン[変種誤検知の
再分類] + CeraVe[52mL規格を一次情報で確認済み])について、既存のresolve_
item_code_for_product()/fetch_rakuten_item_by_item_code()/update_product_
master_item_code_fields()経路のみを使って実際にproduct_masterへ反映する
(一回限りの運用スクリプト、新しい照合ロジックは作らない)。not_found 5件は
対象外。
"""
import os

import psycopg2

import app
import product_collection_pipeline as pipeline

TARGET_PRODUCT_IDS = [
    # Step14で resolved だった21件
    40, 42, 43, 44, 45, 46, 47, 50, 51, 52, 53, 54, 55, 57, 58, 60, 61, 62, 64, 65, 66,
    # 変種誤検知と判断し再分類(JAN完全一致で確認済み)
    41, 56,
    # 52mLが一次情報で確認できたため更新対象に追加
    39,
]


def fetch_targets():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, category, jan_code,
               active_ingredients, formulation, item_code
        FROM product_master WHERE product_id = ANY(%s) ORDER BY product_id
    """, (TARGET_PRODUCT_IDS,))
    rows = cur.fetchall()
    conn.close()
    return rows


def main():
    if not app.RAKUTEN_APP_ID or not app.RAKUTEN_ACCESS_KEY:
        print("[ABORT] RAKUTEN_APP_ID/RAKUTEN_ACCESS_KEY が設定されていません。")
        return

    targets = fetch_targets()
    print(f"対象: {len(targets)}件\n")

    results = []
    for product_id, brand, name, category, jan_code, before_actives, before_formulation, before_item_code in targets:
        resolution = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan_code)
        if resolution["status"] != "confirmed":
            print(f"[{product_id}] {brand}/{name} -> resolve失敗(status={resolution['status']})、スキップ")
            results.append({"product_id": product_id, "brand": brand, "name": name, "status": "resolve_failed"})
            continue

        update_result = pipeline.verify_and_resolve_item_code(product_id, brand, name, category, jan_code=jan_code)
        print(f"[{product_id}] {brand}/{name} -> {update_result.get('status')}")
        results.append({
            "product_id": product_id, "brand": brand, "name": name,
            "status": update_result.get("status"),
            "item_code": update_result.get("item_code"),
            "before_actives": before_actives, "before_formulation": before_formulation,
        })

    print("\n===== 結果集計 =====")
    status_counts = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    print(f"status内訳: {status_counts}")

    for r in results:
        if r["status"] not in ("resolved",):
            print(f"  未更新: product_id={r['product_id']} {r['brand']}/{r['name']} status={r['status']}")


if __name__ == "__main__":
    main()
