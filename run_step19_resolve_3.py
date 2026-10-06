"""Step19-3: 28(WHITH WHITE)/49(クレアラシル)/59(ドクターサニー)を、修正後の
build_rakuten_search_keywords()を通る既存経路(resolve_item_code_for_product/
verify_and_resolve_item_code)で再解決する。単品確認できた場合のみDB更新。
"""
import os

import psycopg2

import app
import product_collection_pipeline as pipeline

TARGET_IDS = [28, 49, 59]


def fetch_targets():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, category, jan_code, item_code
        FROM product_master WHERE product_id = ANY(%s) ORDER BY product_id
    """, (TARGET_IDS,))
    rows = cur.fetchall()
    conn.close()
    return rows


def main():
    targets = fetch_targets()
    for product_id, brand, name, category, jan_code, before_item_code in targets:
        print("=" * 70)
        print(f"product_id={product_id} {brand}/{name} (before item_code={before_item_code})")
        print("=" * 70)

        resolution = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan_code)
        print(f"  resolve status={resolution['status']}")
        print(f"  initial_candidate_count={resolution.get('initial_candidate_count')}")
        print(f"  title_matched_count={resolution.get('title_matched_count')}")
        print(f"  single_item_count={resolution.get('single_item_count')}")

        if resolution["status"] != "confirmed":
            print(f"  詳細: {resolution}")
            print("  -> confirmedでないためDB更新せず終了")
            continue

        item = resolution["item"]
        print(f"  選定item: itemCode={item.get('itemCode')} shop={item.get('shopName')} price={item.get('itemPrice')}")
        print(f"  itemName={item.get('itemName')}")
        print(f"  disambiguated_by={resolution.get('disambiguated_by') or 'natural'}")
        print(f"  set判定(新ロジック)={app._is_rakuten_set_item(str(item.get('itemName', '')))}")

        update_result = pipeline.verify_and_resolve_item_code(product_id, brand, name, category, jan_code=jan_code)
        print(f"  verify_and_resolve_item_code status={update_result.get('status')}")
        for k in ("item_code", "http_status", "disambiguated_by"):
            if k in update_result:
                print(f"    {k}={update_result.get(k)}")
        print()


if __name__ == "__main__":
    main()
