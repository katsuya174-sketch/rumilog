"""P2 Step12: Step7(13商品)・Step11(1商品)のstagingをdry-run確認後、
product_masterへ本反映する(一回限りの運用スクリプト)。既存のreflect_batch_
to_product_masterをそのまま再利用する。"""
import sys
import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_IDS = ["p2-step7-1791276523", "p2-step11-1791277900"]


def main(dry_run=True):
    for batch_id in BATCH_IDS:
        print(f"\n########## batch_id={batch_id} ##########")
        results = pipeline.reflect_batch_to_product_master(batch_id, dry_run=dry_run)
        by_status = {}
        for r in results:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        print(f"ステータス別件数: {by_status}")
        for r in results:
            if r["status"] in ("would_reflect", "reflected"):
                p = r.get("product") or {}
                print(f"  {r['status']}: action={r.get('action')} brand={p.get('brand','')} name={p.get('name','')} "
                      f"active_ingredients={p.get('active_ingredients','')} product_id={r.get('product_id')}")
            else:
                print(f"  {r['status']}: staging_id={r['staging_id']} reason={r.get('reason')}")


if __name__ == "__main__":
    main(dry_run="--apply" not in sys.argv)
