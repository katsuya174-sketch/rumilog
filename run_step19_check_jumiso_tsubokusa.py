"""Step19-4: JUMISO(48)/ツボクサレディ(63)を、修正後の通常経路で各1回だけ確認。
対象SKUが出なければitem_code=NULLのまま終了。個別例外・無理な紐付けは行わない。
"""
import os

import psycopg2

import product_collection_pipeline as pipeline

TARGET_IDS = [48, 63]


def fetch_targets():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, category, jan_code
        FROM product_master WHERE product_id = ANY(%s) ORDER BY product_id
    """, (TARGET_IDS,))
    rows = cur.fetchall()
    conn.close()
    return rows


def main():
    for product_id, brand, name, category, jan_code in fetch_targets():
        print("=" * 70)
        print(f"product_id={product_id} {brand}/{name}")
        print("=" * 70)
        resolution = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan_code)
        print(f"  status={resolution['status']}")
        print(f"  initial_candidate_count={resolution.get('initial_candidate_count')}")
        print(f"  title_matched_count={resolution.get('title_matched_count')}")
        print(f"  single_item_count={resolution.get('single_item_count')}")
        if resolution["status"] == "confirmed":
            print(f"  (confirmed) itemName={resolution['item'].get('itemName')}")
        print("  -> DB更新なし(1回のみの確認)\n")


if __name__ == "__main__":
    main()
