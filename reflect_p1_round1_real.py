"""P1 Round1: batch p1-coverage-1791094432 の成功7件をproduct_masterへ
本反映する(dry-run確認済み、一回限りの運用スクリプト)。"""
import os
import psycopg2
import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_ID = "p1-coverage-1791094432"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM product_master")
    before_total = cur.fetchone()[0]
    conn.close()

    results = pipeline.reflect_batch_to_product_master(BATCH_ID, dry_run=False)
    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM product_master")
    after_total = cur.fetchone()[0]
    conn.close()

    print(f"ステータス別件数: {by_status}")
    print(f"product_master行数: {before_total} -> {after_total} (差分={after_total - before_total})")
    for r in results:
        if r["status"] == "reflected":
            print(f"  reflected: product_id={r['product_id']} action={r['action']}")


if __name__ == "__main__":
    main()
