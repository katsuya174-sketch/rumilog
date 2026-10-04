"""
Phase 3: pilot-1791040996の再開実行(未実行18商品のみ)。
一回限りの運用スクリプト(診断用gemini_usageの実行前後確認を含む)。
"""

import os
import sys

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"


def usage_count():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        key = app.get_gemini_usage_key()
        cur.execute("SELECT request_count FROM gemini_usage WHERE usage_key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def product_master_lookup(brand, name, category):
    results = app.query_product_master_candidates(category)
    key = app._normalize_product_master_identity_key(brand, name, category)
    for r in results:
        if app._normalize_product_master_identity_key(r.get("brand", ""), r.get("name", ""), r.get("category", "")) == key:
            return r
    return None


def main():
    before = usage_count()
    print(f"[BEFORE] diagnosis gemini_usage count = {before}")

    results = pipeline.resume_batch(BATCH_ID, product_master_lookup=product_master_lookup)

    after = usage_count()
    print(f"[AFTER] diagnosis gemini_usage count = {after}")
    print(f"[DIAGNOSIS USAGE UNCHANGED] {before == after}")

    print(f"\n[RESUME RESULTS] {len(results)} products processed this run")
    for r in results:
        print(r)


if __name__ == "__main__":
    main()
