"""Phase 2+3統合修正: product_masterの既存行へactive_ingredient_tagsを
バックフィルするmigration(一回限りの運用スクリプト)。

- 原文のactive_ingredientsは変更しない。
- 別の正規化ロジックは作らず、app.compute_ingredient_tags()
  (= normalize_ingredient_tag()の集合)をそのまま再利用する。
- 正規化できない成分は無理にタグ化しない(タグ集合から単純に落ちる)。
- 既定はdry_run(--applyを渡した場合のみ実際にUPDATEする)。
"""

import json
import os
import sys

import psycopg2

import app  # noqa: F401 (.env読み込みトリガー)


def main(dry_run=True):
    app.init_product_master_table()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, category, active_ingredients, active_ingredient_tags
        FROM product_master
        ORDER BY product_id
    """)
    rows = cur.fetchall()

    to_update = []
    unchanged = []
    for product_id, brand, name, category, active_ingredients, existing_tags in rows:
        active_ingredients = active_ingredients or []
        new_tags = app.compute_ingredient_tags(active_ingredients)
        if (existing_tags or []) == new_tags:
            unchanged.append((product_id, brand, name))
            continue
        to_update.append({
            "product_id": product_id, "brand": brand, "name": name, "category": category,
            "active_ingredients": active_ingredients,
            "existing_tags": existing_tags or [],
            "new_tags": new_tags,
        })

    print(f"対象行数(全件): {len(rows)}")
    print(f"変更不要(既に一致): {len(unchanged)}件")
    print(f"更新対象: {len(to_update)}件\n")

    dropped_total = 0
    for item in to_update:
        dropped = [a for a in item["active_ingredients"] if not app.normalize_ingredient_tag(str(a))]
        dropped_total += len(dropped)
        print(f"  [{item['category']}] {item['brand']} / {item['name']}")
        print(f"    active_ingredients(原文、変更なし): {item['active_ingredients']}")
        print(f"    active_ingredient_tags: {item['existing_tags']} -> {item['new_tags']}")
        if dropped:
            print(f"    タグ化できず除外(無理にタグ化しない): {dropped}")
        print()

    print(f"タグ化できなかった成分の総数(正規化不能、無理にタグ化せず除外): {dropped_total}件")

    if dry_run:
        print("\n[DRY RUN] 実際の更新は行っていません。--apply を付けて再実行してください。")
        conn.close()
        return {"dry_run": True, "total": len(rows), "would_update": len(to_update), "unchanged": len(unchanged)}

    for item in to_update:
        cur.execute(
            "UPDATE product_master SET active_ingredient_tags = %s WHERE product_id = %s",
            (json.dumps(item["new_tags"]), item["product_id"]),
        )
    conn.commit()
    conn.close()
    print(f"\n[APPLIED] {len(to_update)}件を更新しました。")
    return {"dry_run": False, "total": len(rows), "updated": len(to_update), "unchanged": len(unchanged)}


if __name__ == "__main__":
    main(dry_run="--apply" not in sys.argv)
