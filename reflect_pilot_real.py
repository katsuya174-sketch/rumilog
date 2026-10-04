"""Phase 3: pilot-1791040996の成功38件をproduct_masterへ本反映する。
dry-runで異常が無いことを確認済みの上で実行する(一回限りの運用スクリプト)。"""

import os
import sys

import psycopg2

import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"


def main():
    before_count_conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = before_count_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM product_master")
    before_total = cur.fetchone()[0]
    before_count_conn.close()

    results = pipeline.reflect_batch_to_product_master(BATCH_ID, dry_run=False)

    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    after_count_conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = after_count_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM product_master")
    after_total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM product_master WHERE data_source = 'ai_precollected'")
    ai_precollected_total = cur.fetchone()[0]
    after_count_conn.close()

    print(f"===== 本反映結果 (batch_id={BATCH_ID}) =====")
    print(f"ステータス別件数: {by_status}")
    print(f"product_master行数: {before_total} -> {after_total} (差分={after_total - before_total})")
    print(f"data_source='ai_precollected'の行数: {ai_precollected_total}")


if __name__ == "__main__":
    main()
