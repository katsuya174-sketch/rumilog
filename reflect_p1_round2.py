"""P1 Round2: batch p1-coverage-round2-1791095683 の成功5件をdry-run確認後、
product_masterへ本反映する(一回限りの運用スクリプト)。既存のreflect_batch_
to_product_masterをそのまま再利用する(別の反映ロジックは作らない)。
ルナメアACもcitationで確認できた成分(グリチルレチン酸ステアリル)のみで反映され、
salicylic_acidは追加されない(stagingの実データをそのまま使うため)。
"""
import sys
import app  # noqa: F401
import product_collection_pipeline as pipeline

BATCH_ID = "p1-coverage-round2-1791095683"


def main(dry_run=True):
    results = pipeline.reflect_batch_to_product_master(BATCH_ID, dry_run=dry_run)
    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(f"ステータス別件数: {by_status}")
    for r in results:
        if r["status"] in ("would_reflect", "reflected"):
            product = r.get("product") or {}
            print(f"  {r['status']}: action={r.get('action')} "
                  f"brand={product.get('brand', '')} name={product.get('name', '')} "
                  f"active_ingredients={product.get('active_ingredients', '')} "
                  f"product_id={r.get('product_id')}")
        else:
            print(f"  {r['status']}: staging_id={r['staging_id']} reason={r.get('reason')}")


if __name__ == "__main__":
    main(dry_run="--apply" not in sys.argv)
