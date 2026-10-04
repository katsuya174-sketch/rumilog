"""P1 Round1: batch p1-coverage-1791094432 の成功7件をdry-run確認する
(一回限りの運用スクリプト)。既存のreflect_batch_to_product_masterを
そのまま再利用する(別の反映ロジックは作らない)。"""
import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_ID = "p1-coverage-1791094432"


def main():
    results = pipeline.reflect_batch_to_product_master(BATCH_ID, dry_run=True)
    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(f"ステータス別件数: {by_status}")
    for r in results:
        if r["status"] == "would_reflect":
            print(f"  would_reflect: action={r['action']} existing_product_id={r.get('existing_product_id')} "
                  f"brand={r['product']['brand']} name={r['product']['name']}")
        else:
            print(f"  {r['status']}: staging_id={r['staging_id']} reason={r.get('reason')}")


if __name__ == "__main__":
    main()
