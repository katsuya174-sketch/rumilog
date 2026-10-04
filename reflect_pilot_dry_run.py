"""Phase 3: pilot-1791040996の成功38件をproduct_masterへ反映する前の
dry-run確認スクリプト。実際には何も書き込まない。"""

import json
import os
import sys

import psycopg2

import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"


def main():
    results = pipeline.reflect_batch_to_product_master(BATCH_ID, dry_run=True)

    by_status = {}
    actions = {"insert": 0, "update": 0}
    identity_keys_seen = {}
    collisions = []

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "SELECT staging_id, identity_key, brand, product_name FROM product_collection_staging WHERE batch_id = %s",
        (BATCH_ID,),
    )
    id_to_identity = {r[0]: r for r in cur.fetchall()}
    conn.close()

    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == "would_reflect":
            actions[r["action"]] += 1
            info = id_to_identity.get(r["staging_id"])
            if info:
                _, identity_key, brand, name = info
                if identity_key in identity_keys_seen:
                    collisions.append((identity_key, identity_keys_seen[identity_key], (brand, name)))
                else:
                    identity_keys_seen[identity_key] = (brand, name)

    print(f"===== dry-run結果 (batch_id={BATCH_ID}) =====")
    print(f"ステータス別件数: {by_status}")
    print(f"would_reflectのaction内訳: {actions}")
    print(f"\n同一バッチ内でのidentity_key衝突: {len(collisions)}件")
    for c in collisions:
        print(f"  {c}")

    print("\n=== would_reflectの内容サンプル(先頭5件) ===")
    shown = 0
    for r in results:
        if r["status"] == "would_reflect" and shown < 5:
            print(json.dumps(r["product"], ensure_ascii=False, indent=2)[:500])
            print(f"  action={r['action']} existing_product_id={r['existing_product_id']}")
            shown += 1


if __name__ == "__main__":
    main()
