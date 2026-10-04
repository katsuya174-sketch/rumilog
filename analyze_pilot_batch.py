"""Phase 3: pilot-1791040996の50商品全体の集計レポート(一回限りの運用スクリプト)。"""

import json
import os
import sys

import psycopg2

import app  # noqa: F401 (load_dotenv()の副作用でDATABASE_URLをos.environへ反映させるため)

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("""
        SELECT staging_id, brand, product_name, stage1_status, stage1_citations,
               stage1_search_queries, stage2_status, stage2_payload, conflict_status, conflict_detail
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    rows = cur.fetchall()

    total = len(rows)
    stage1_ok = sum(1 for r in rows if r[3] == "ok")
    stage1_fail = total - stage1_ok
    stage1_fail_reasons = {}
    for r in rows:
        if r[3] != "ok":
            stage1_fail_reasons[r[3]] = stage1_fail_reasons.get(r[3], 0) + 1

    stage2_ok = sum(1 for r in rows if r[6] == "ok")
    stage2_skipped = sum(1 for r in rows if r[6] == "skipped")
    stage2_error = sum(1 for r in rows if r[6] not in ("ok", "skipped") and r[6] is not None)

    fully_complete = sum(1 for r in rows if r[3] == "ok" and r[6] == "ok")

    total_search_queries = sum(len(r[5] or []) for r in rows)

    needs_review = [(r[0], r[1], r[2], r[9]) for r in rows if r[8] == "needs_review"]

    # unknown率・citation裏付け率(stage2_payloadのactive_ingredients/formulation_features)
    total_fields = 0
    unknown_fields = 0
    total_items = 0
    cited_items = 0
    for r in rows:
        payload = r[7]
        if not payload:
            continue
        for item in (payload.get("active_ingredients") or []):
            total_items += 1
            if item.get("source_url") and item.get("source_url") != "unknown":
                cited_items += 1
            total_fields += 1
            if item.get("concentration") == "unknown":
                unknown_fields += 1
        for item in (payload.get("formulation_features") or []):
            total_items += 1
            if item.get("source_url") and item.get("source_url") != "unknown":
                cited_items += 1
        jan = payload.get("jan_code")
        if jan is not None:
            total_fields += 1
            if jan == "unknown":
                unknown_fields += 1

    cur.execute("""
        SELECT COUNT(*), SUM(prompt_token_count), SUM(candidates_token_count),
               SUM(total_token_count), SUM(estimated_cost_usd),
               SUM(CASE WHEN grounding_used THEN 1 ELSE 0 END)
        FROM product_collection_usage WHERE batch_id = %s
    """, (BATCH_ID,))
    usage_row = cur.fetchone()
    conn.close()

    print(f"===== Phase 3 パイロット集計レポート (batch_id={BATCH_ID}) =====\n")
    print(f"対象商品数: {total}")
    print(f"\n[Stage1] 成功: {stage1_ok} / 失敗: {stage1_fail}")
    print(f"  失敗理由分類: {stage1_fail_reasons}")
    print(f"\n[Stage2] 成功: {stage2_ok} / スキップ(Stage1失敗のため未実行): {stage2_skipped} / エラー: {stage2_error}")
    print(f"\n完全成功商品数(Stage1・Stage2ともOK): {fully_complete}")
    print(f"\n総API呼び出し数: {usage_row[0]}")
    print(f"総検索クエリ数(web_search_queries合計): {total_search_queries}")
    print(f"総token数: prompt={usage_row[1]} / output={usage_row[2]} / total={usage_row[3]}")
    print(f"実測トークン数に基づく安全側推定費用: ${usage_row[4]:.6f}")
    print(f"Grounding使用呼び出し数: {usage_row[5]} / {usage_row[0]}")
    print(f"\nunknown率(jan_code・成分濃度の対象フィールド中): {unknown_fields}/{total_fields} = {unknown_fields/total_fields*100:.1f}%" if total_fields else "\nunknown率: 対象フィールドなし")
    print(f"citationで裏付けられた項目率(成分・formulation_feature中、source_url!=unknown): {cited_items}/{total_items} = {cited_items/total_items*100:.1f}%" if total_items else "citationで裏付けられた項目率: 対象項目なし")
    print(f"\nneeds_review件数: {len(needs_review)}")
    for staging_id, brand, name, detail in needs_review:
        print(f"  staging_id={staging_id} {brand} {name}: {detail}")


if __name__ == "__main__":
    main()
