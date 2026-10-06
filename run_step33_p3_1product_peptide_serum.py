"""Step33: serum×peptide最終不足1件(KISOCARE/キソ マトリックスセラム PE)の
Stage1/Stage2収集(一回限りの運用スクリプト)。既存Phase3パイプラインのみ使用。
product_masterへの本反映は行わない(staging/usageまで)。
"""
import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = f"p3-step33-{int(time.time())}"

PRODUCT = ("KISOCARE", "キソ マトリックスセラム PE", "美容液", "peptide")

AREAS_13 = [
    ("化粧水", "hyaluronic_acid"), ("美容液", "vitamin_c"), ("化粧水", "ceramide"),
    ("美容液", "peptide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
    ("化粧水", "niacinamide"), ("洗顔", "salicylic_acid"),
    ("化粧水", "tranexamic_acid"), ("化粧水", "amino_acid"), ("洗顔", "glycolic_acid"),
    ("クリーム", "peptide"), ("クレンジング", "centella_extract"),
]

NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000

# Step31+32で実際にreflect可能と確認済みの7商品(そのまま再利用、新しいロジックなし)
PRIOR_REFLECTABLE = [
    {"brand": "プラスキレイ", "name": "プラスレチAセラム", "category": "美容液",
     "active_ingredients": ["hyaluronic_acid", "retinol"]},
    {"brand": "プラスキレイ", "name": "プラスピュアVC28", "category": "美容液",
     "active_ingredients": ["vitamin_c"]},
    {"brand": "ハルメク", "name": "C35プレミアム", "category": "美容液",
     "active_ingredients": ["enzyme", "vitamin_c"]},
    {"brand": "ETVOS", "name": "モイスチャライジングローション", "category": "化粧水",
     "active_ingredients": ["amino_acid", "ceramide", "hyaluronic_acid"]},
    {"brand": "肌ラボ", "name": "極潤 薬用ハリ化粧水", "category": "化粧水",
     "active_ingredients": ["niacinamide"]},
    {"brand": "ちふれ", "name": "濃厚化粧水", "category": "化粧水",
     "active_ingredients": ["dipotassium_glycyrrhizate", "glycerin", "hyaluronic_acid"]},
    {"brand": "SKINFOOD", "name": "どんぐり ポア ペプチド クリーム", "category": "クリーム",
     "active_ingredients": ["peptide"]},
]


def get_gemini_usage_count():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        key = app.get_gemini_usage_key()
        cur.execute("SELECT request_count FROM gemini_usage WHERE usage_key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def effective_candidates(category, tag, db_products, verified_products, extra_candidates):
    def keys_of(items):
        s = set()
        for p in items:
            if isinstance(p, dict):
                k = app.make_verified_product_key(p)
                if k:
                    s.add(k)
        return s

    seen = keys_of(db_products) | keys_of(verified_products)
    master = app.query_product_master_candidates(category, limit=50)
    survivors = [mp for mp in master if app.make_verified_product_key(mp) not in seen]

    for extra in extra_candidates:
        if app.normalize_candidate_category(extra.get("category", ""), fallback=extra.get("category", "")) != \
           app.normalize_candidate_category(category, fallback=category):
            continue
        k = app.make_verified_product_key(extra)
        if k and k not in seen:
            survivors.append(extra)
            seen.add(k)

    step = {"category": category, "purpose": "", "ingredient_focus": tag}
    relevant = 0
    for mp in survivors:
        reasons = []
        score = app.score_product(dict(mp), step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if app._is_relevant_scored_candidate(score, reasons, tag):
            relevant += 1
    return relevant


def main():
    pipeline.init_product_collection_tables()
    pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = 0.15

    before_gemini_usage = get_gemini_usage_count()
    print(f"[BEFORE] 診断用gemini_usage count = {before_gemini_usage}")

    db_products = app.load_products()
    verified_products = app.load_verified_products_cache()

    before_sp = effective_candidates("美容液", "peptide", db_products, verified_products, PRIOR_REFLECTABLE)
    print(f"\n[BEFORE] 美容液 x peptide 実効候補数(Step31+32反映分込み): {before_sp}")

    brand, name, category, target = PRODUCT
    selected = [{"brand": brand, "name": name, "category": category, "active_ingredients": []}]
    pipeline.log_pilot_selection(BATCH_ID, selected)

    def product_master_lookup(b, n, c):
        results = app.query_product_master_candidates(c)
        key = app._normalize_product_master_identity_key(b, n, c)
        for r in results:
            if app._normalize_product_master_identity_key(r.get("brand", ""), r.get("name", ""), r.get("category", "")) == key:
                return r
        return None

    results = pipeline.collect_batch(
        [{"brand": brand, "name": name, "category": category}],
        BATCH_ID, product_master_lookup=product_master_lookup,
    )

    after_gemini_usage = get_gemini_usage_count()
    print(f"\n[AFTER] 診断用gemini_usage count = {after_gemini_usage}")
    print(f"診断用gemini_usage不変: {before_gemini_usage == after_gemini_usage}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, product_name, category, stage1_status, stage1_citations, stage2_status,
               stage2_payload, conflict_status, stage1_search_queries
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    row = cur.fetchone()
    conn.close()

    if not row:
        print("[ERROR] staging record not found")
        return

    b, n, c, stage1_status, citations, stage2_status, payload, conflict_status, search_queries = row
    payload = payload or {}
    active_ingredients = [
        i.get("ingredient") for i in (payload.get("active_ingredients") or [])
        if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
    ]
    tags = app.compute_ingredient_tags(active_ingredients)
    target_hit = target in tags

    existing_keys = set()
    master = app.query_product_master_candidates(category, limit=50)
    for p in db_products + master + verified_products:
        k = app.make_verified_product_key(p)
        if k:
            existing_keys.add(k)
    own_key = app.make_verified_product_key({"brand": b, "name": n, "category": c})
    is_duplicate = own_key in existing_keys

    reflectable = stage2_status == "ok" and conflict_status != "needs_review" and not is_duplicate and target_hit

    print(f"\n===== {b} / {n} =====")
    print(f"  狙ったtag: {target}")
    print(f"  stage1={stage1_status} (citation数={len(citations or [])}) stage2={stage2_status} conflict={conflict_status}")
    print(f"  抽出active_ingredients: {active_ingredients}")
    print(f"  active_ingredient_tags(computed): {tags}")
    print(f"  target_hit(peptide生成): {target_hit}")
    print(f"  identity重複: {is_duplicate}")
    print(f"  reflect可能: {reflectable}")

    if not (stage1_status == "ok" and target_hit):
        print("\n*** 失敗: no_search_evidenceまたはpeptide未抽出。追加Gemini実行はせずSTOP。 ***")
        print("次に調査すべきはStage1の検索取得能力そのもの(候補を連続投入しない)。")
    else:
        print("\n成功。美容液×peptideへ仮追加し、13領域を再計算します。")
        all_extra = PRIOR_REFLECTABLE + [{"brand": b, "name": n, "category": c, "active_ingredients": tags}]
        after_sp = effective_candidates("美容液", "peptide", db_products, verified_products, all_extra)
        print(f"\n美容液 x peptide: {before_sp} -> {after_sp} sufficient(>=3)={after_sp >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT}")

        print("\n===== 13領域(P1 8+P2 5) 全体再計算(Step27と同じdedup後基準) =====")
        all_sufficient = True
        for cat, tag in AREAS_13:
            cnt = effective_candidates(cat, tag, db_products, verified_products, all_extra)
            suff = cnt >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
            print(f"  {cat} x {tag}: {cnt} sufficient={suff}")
            if not suff:
                all_sufficient = False
        print(f"\n13/13すべてsufficient: {all_sufficient}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT stage, COUNT(*), SUM(prompt_token_count), SUM(candidates_token_count),
               SUM(total_token_count), SUM(estimated_cost_usd), SUM(CASE WHEN grounding_used THEN 1 ELSE 0 END)
        FROM product_collection_usage WHERE batch_id = %s GROUP BY stage
    """, (BATCH_ID,))
    usage_rows = cur.fetchall()
    cur.execute("SELECT COUNT(*), SUM(estimated_cost_usd) FROM product_collection_usage WHERE batch_id = %s", (BATCH_ID,))
    total_calls, total_cost = cur.fetchone()
    conn.close()

    print(f"\n\n===== API使用量・費用 (batch_id={BATCH_ID}) =====")
    for stage, cnt, pt, ct, tt, cost, grounding in usage_rows:
        print(f"  stage={stage}: calls={cnt} prompt_tokens={pt} candidates_tokens={ct} "
              f"total_tokens={tt} grounding使用={grounding} cost=${float(cost or 0):.4f}")
    print(f"\n  総API call数: {total_calls}")
    print(f"  総search query数: {len(search_queries or [])}")
    print(f"  推定総費用: ${float(total_cost or 0):.4f} (上限$0.15)")


if __name__ == "__main__":
    main()
