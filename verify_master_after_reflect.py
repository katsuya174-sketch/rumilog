"""Phase 3: product_masterへの反映後の検証(一回限りの運用スクリプト)。
38件が正しく存在し、重複・欠落が無いことを確認する。"""

import os
import sys

import psycopg2

import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("""
        SELECT brand, product_name, category, identity_key FROM product_collection_staging
        WHERE batch_id = %s AND stage2_status = 'ok' AND conflict_status != 'needs_review'
    """, (BATCH_ID,))
    expected = cur.fetchall()

    cur.execute("SELECT brand, name, category FROM product_master WHERE data_source = 'ai_precollected'")
    actual = cur.fetchall()

    cur.execute("""
        SELECT identity_key, COUNT(*) FROM product_master
        WHERE data_source = 'ai_precollected' GROUP BY identity_key HAVING COUNT(*) > 1
    """)
    dup_identity = cur.fetchall()

    conn.close()

    print(f"期待件数(staging側、反映対象): {len(expected)}")
    print(f"product_master実件数(ai_precollected): {len(actual)}")
    print(f"重複identity_key: {len(dup_identity)}件 {dup_identity}")

    missing = []
    for brand, name, category, identity_key in expected:
        found = any(a[0] == brand and a[1] == name and a[2] == category for a in actual)
        if not found:
            missing.append((brand, name, category))
    print(f"\nstagingにあるがproduct_masterに見つからないもの: {len(missing)}件")
    for m in missing:
        print(f"  {m}")

    print("\n=== product_master全38件一覧 ===")
    for brand, name, category in sorted(actual, key=lambda x: x[2]):
        print(f"  [{category}] {brand} / {name}")


if __name__ == "__main__":
    main()
