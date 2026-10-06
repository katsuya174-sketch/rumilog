"""Step17-2: 新ロジック(app.py本体に実装済み)で61件を再監査(read-only)。
product_id=64のみ新規set判定、他60件false positive 0を確認する。
"""
import os

import psycopg2

import app


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, rakuten_title
        FROM product_master
        WHERE item_code IS NOT NULL AND item_code != ''
        ORDER BY product_id
    """)
    rows = cur.fetchall()
    conn.close()

    print(f"総件数: {len(rows)}\n")
    set_flagged = []
    for product_id, brand, name, rakuten_title in rows:
        title = rakuten_title or ""
        if app._is_rakuten_set_item(title):
            set_flagged.append((product_id, brand, name, title))

    print(f"新ロジックでset判定された件数: {len(set_flagged)}")
    for p in set_flagged:
        print(f"  product_id={p[0]} {p[1]}/{p[2]}  title={p[3]}")

    others = [r for r in rows if r[0] not in {p[0] for p in set_flagged}]
    print(f"\nその他(set判定されない)件数: {len(others)}")
    expected_only_64 = len(set_flagged) == 1 and set_flagged[0][0] == 64
    print(f"\n期待通り(product_id=64のみ新規set判定、他60件false positive 0): {expected_only_64}")


if __name__ == "__main__":
    main()
