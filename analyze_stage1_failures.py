"""Phase 3品質監査: Stage1失敗(no_search_evidence)12商品と成功38商品を比較し、
共通する原因パターンがないか分析する。"""

import os
import sys

import psycopg2

import app  # noqa: F401

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, product_name, category, stage1_status, stage1_raw_text
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    rows = cur.fetchall()
    conn.close()

    failed = [r for r in rows if r[3] != "ok"]
    success = [r for r in rows if r[3] == "ok"]

    print(f"失敗{len(failed)}件 / 成功{len(success)}件\n")
    print("=== 失敗商品一覧 ===")
    fail_categories = {}
    fail_name_lengths = []
    for brand, name, category, status, raw_text in failed:
        fail_categories[category] = fail_categories.get(category, 0) + 1
        fail_name_lengths.append(len(name))
        print(f"  [{category}] {brand} / {name} (名前長={len(name)})")
        print(f"    raw_text冒頭: {(raw_text or '')[:150]!r}")

    print(f"\n失敗のカテゴリ分布: {fail_categories}")
    print(f"失敗商品の商品名文字数: 平均={sum(fail_name_lengths)/len(fail_name_lengths):.1f} 最小={min(fail_name_lengths)} 最大={max(fail_name_lengths)}")

    success_categories = {}
    success_name_lengths = []
    for brand, name, category, status, raw_text in success:
        success_categories[category] = success_categories.get(category, 0) + 1
        success_name_lengths.append(len(name))
    print(f"\n成功のカテゴリ分布: {success_categories}")
    print(f"成功商品の商品名文字数: 平均={sum(success_name_lengths)/len(success_name_lengths):.1f} 最小={min(success_name_lengths)} 最大={max(success_name_lengths)}")

    # ブランド名が英数字のみか日本語を含むか
    def is_ascii_brand(b):
        return all(ord(c) < 128 for c in b)

    fail_ascii = sum(1 for b, *_ in failed if is_ascii_brand(b))
    success_ascii = sum(1 for b, *_ in success if is_ascii_brand(b))
    print(f"\n失敗商品: ブランド名が英数字のみ={fail_ascii}/{len(failed)}")
    print(f"成功商品: ブランド名が英数字のみ={success_ascii}/{len(success)}")


if __name__ == "__main__":
    main()
