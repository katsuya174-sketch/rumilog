"""Step3-A: product_masterの既存行へ、staging側でcitation確認済みのJANを
バックフィルするmigration(一回限りの運用スクリプト)。

- 対象はproduct_master.jan_codeがNULLの行のみ。
- product_collection_stagingをidentity_keyで突き合わせ、stage2_status='ok'
  かつconflict_status != 'needs_review'かつjan_codeが実在(空/'unknown'以外)
  の行だけを対象にする(citation未確認のJANは反映しない)。
- 既定はdry_run(--applyを渡した場合のみ実際にUPDATEする)。
- 期待件数は固定しない(その時点のstagingデータに応じて変動する)。
"""

import os
import sys

import psycopg2

import app  # noqa: F401


def main(dry_run=True):
    app.init_product_master_table()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("""
        SELECT pm.product_id, pm.brand, pm.name, pm.category, pcs.stage2_payload
        FROM product_master pm
        JOIN product_collection_staging pcs ON pcs.identity_key = pm.identity_key
        WHERE pm.jan_code IS NULL
          AND pcs.stage2_status = 'ok'
          AND pcs.conflict_status != 'needs_review'
        ORDER BY pm.product_id
    """)
    rows = cur.fetchall()

    to_update = []
    no_real_jan = 0
    for product_id, brand, name, category, payload in rows:
        jan_code = str((payload or {}).get("jan_code", "") or "").strip()
        if not jan_code or jan_code.lower() == "unknown":
            no_real_jan += 1
            continue
        to_update.append({
            "product_id": product_id, "brand": brand, "name": name,
            "category": category, "jan_code": jan_code,
        })

    print(f"product_master.jan_code IS NULL かつ stagingに紐付く行: {len(rows)}件")
    print(f"  citation確認済みJANなし(unknown/空): {no_real_jan}件")
    print(f"  更新対象(実在するJANあり): {len(to_update)}件\n")

    for item in to_update:
        print(f"  [{item['category']}] {item['brand']} / {item['name']}: JAN={item['jan_code']}")

    if dry_run:
        print("\n[DRY RUN] 実際の更新は行っていません。--apply を付けて再実行してください。")
        conn.close()
        return {"dry_run": True, "total_checked": len(rows), "would_update": len(to_update), "no_real_jan": no_real_jan}

    for item in to_update:
        cur.execute(
            "UPDATE product_master SET jan_code = %s WHERE product_id = %s",
            (item["jan_code"], item["product_id"]),
        )
    conn.commit()
    conn.close()
    print(f"\n[APPLIED] {len(to_update)}件を更新しました。")
    return {"dry_run": False, "total_checked": len(rows), "updated": len(to_update), "no_real_jan": no_real_jan}


if __name__ == "__main__":
    main(dry_run="--apply" not in sys.argv)
