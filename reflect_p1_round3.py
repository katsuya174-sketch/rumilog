"""P1 Final: batch p1-coverage-round3-1791096267 の成功2件をdry-run確認後、
product_masterへ本反映する(一回限りの運用スクリプト)。既存のreflect_batch_
to_product_masterをそのまま再利用する。"""
import sys
import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_ID = "p1-coverage-round3-1791096267"


def main(dry_run=True):
    results = pipeline.reflect_batch_to_product_master(BATCH_ID, dry_run=dry_run)
    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(f"ステータス別件数: {by_status}")
    for r in results:
        if r["status"] == "would_reflect":
            p = r.get("product") or {}
            print(f"  would_reflect: action={r.get('action')} brand={p.get('brand')} name={p.get('name')} "
                  f"active_ingredients={p.get('active_ingredients')}")
        elif r["status"] == "reflected":
            print(f"  reflected: product_id={r.get('product_id')} action={r.get('action')}")
        else:
            print(f"  {r['status']}: staging_id={r['staging_id']} reason={r.get('reason')}")


if __name__ == "__main__":
    main(dry_run="--apply" not in sys.argv)
