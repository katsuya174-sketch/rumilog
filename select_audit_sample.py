"""Phase 3品質監査: 成功38商品からカテゴリ・成分系統が偏らないよう10商品を抽出し、
監査に必要な生データ(Stage1 raw_text・citations、Stage2 payload)を出力する。"""

import json
import os
import sys

import psycopg2

import app  # noqa: F401

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT staging_id, brand, product_name, category, stage1_raw_text, stage1_citations, stage2_payload
        FROM product_collection_staging
        WHERE batch_id = %s AND stage1_status = 'ok' AND stage2_status = 'ok'
        ORDER BY staging_id
    """, (BATCH_ID,))
    rows = cur.fetchall()
    conn.close()

    buckets = {}
    for r in rows:
        staging_id, brand, name, category, raw_text, citations, payload = r
        actives = [i.get("ingredient") for i in (payload.get("active_ingredients") or [])]
        tag = actives[0] if actives else "(none)"
        buckets.setdefault((category, tag), []).append(r)

    selected = []
    for key in sorted(buckets.keys()):
        selected.append(buckets[key][0])
        if len(selected) >= 10:
            break
    # 10件に満たない場合は残りから追加
    if len(selected) < 10:
        remaining = [r for r in rows if r not in selected]
        selected.extend(remaining[: 10 - len(selected)])

    for r in selected:
        staging_id, brand, name, category, raw_text, citations, payload = r
        print(f"\n{'='*80}\nstaging_id={staging_id} | {brand} {name} | category={category}")
        print(f"--- STAGE1 RAW TEXT ---\n{raw_text}")
        print(f"--- STAGE1 CITATIONS ---")
        for c in (citations or []):
            print(f"  title={c.get('title')!r} uri={c.get('uri')[:90]}...")
        print(f"--- STAGE2 PAYLOAD ---\n{json.dumps(payload, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
