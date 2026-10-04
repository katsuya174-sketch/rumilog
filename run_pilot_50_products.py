"""
Phase 3: 50商品パイロットの実行スクリプト。

まだ実行しない(承認後にユーザー自身が実行する想定)。1コマンドで
選定ログ保存→Stage1/Stage2収集→staging保存までを行う。product_masterへの
反映(reflect_batch_to_product_master)はここでは行わない(収集結果を見て
から別途判断するため、別コマンドとして分離する)。

実行方法:
    python3 run_pilot_50_products.py [batch_id]

batch_id省略時は実行時刻から自動生成する。
"""

import sys
import time

import app
import product_collection_pipeline as pipeline


def main():
    batch_id = sys.argv[1] if len(sys.argv) > 1 else f"pilot-{int(time.time())}"

    pipeline.init_product_collection_tables()

    selected = pipeline.select_and_log_pilot_products(batch_id, target_count=50)
    print(f"[SELECTED] {len(selected)}商品(ログ: product_collection_batch_logs/{batch_id}_selection.json)")

    def product_master_lookup(brand, name, category):
        results = app.query_product_master_candidates(category)
        key = app._normalize_product_master_identity_key(brand, name, category)
        for r in results:
            if app._normalize_product_master_identity_key(r.get("brand", ""), r.get("name", ""), r.get("category", "")) == key:
                return r
        return None

    results = pipeline.collect_batch(selected, batch_id, product_master_lookup=product_master_lookup)

    print(f"\n===== BATCH SUMMARY (batch_id={batch_id}) =====")
    for r in results:
        print(r)
    print(f"\n処理数: {len(results)}/{len(selected)}")
    print("\nproduct_masterへの反映はまだ行っていません。収集結果(staging)を確認後、")
    print("以下で反映してください(dry_run=Trueで内容確認 → dry_run=Falseで本反映):")
    print(f'  python3 -c "import product_collection_pipeline as p; print(p.reflect_batch_to_product_master({batch_id!r}, dry_run=True))"')


if __name__ == "__main__":
    main()
