"""Step21-5: product_id=59(ドクターサニー)を修正後のis_same_verified_rakuten_product()
を通る既存経路(resolve_item_code_for_product/verify_and_resolve_item_code)で
再解決する。confirmed(かつ再検証成功)の場合のみDB更新。
"""
import os

import psycopg2

import app
import product_collection_pipeline as pipeline

PRODUCT_ID = 59


def fetch_target():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, name, category, jan_code, item_code
        FROM product_master WHERE product_id = %s
    """, (PRODUCT_ID,))
    row = cur.fetchone()
    conn.close()
    return row


def main():
    brand, name, category, jan_code, before_item_code = fetch_target()
    print(f"product_id={PRODUCT_ID} {brand}/{name} (before item_code={before_item_code!r})")

    resolution = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan_code)
    print(f"resolve status={resolution['status']}")
    print(f"initial_candidate_count={resolution.get('initial_candidate_count')}")
    print(f"title_matched_count={resolution.get('title_matched_count')}")
    print(f"single_item_count={resolution.get('single_item_count')}")

    if resolution["status"] != "confirmed":
        print(f"詳細: {resolution}")
        print("-> confirmedでないためDB更新せず終了")
        return

    item = resolution["item"]
    print(f"選定item: itemCode={item.get('itemCode')} shop={item.get('shopName')} price={item.get('itemPrice')}")
    print(f"itemName={item.get('itemName')}")
    print(f"disambiguated_by={resolution.get('disambiguated_by') or 'natural'}")
    print(f"set判定={app._is_rakuten_set_item(str(item.get('itemName', '')))}")

    update_result = pipeline.verify_and_resolve_item_code(PRODUCT_ID, brand, name, category, jan_code=jan_code)
    print(f"verify_and_resolve_item_code status={update_result.get('status')}")
    for k in ("item_code", "http_status", "disambiguated_by"):
        if k in update_result:
            print(f"  {k}={update_result.get(k)}")


if __name__ == "__main__":
    main()
