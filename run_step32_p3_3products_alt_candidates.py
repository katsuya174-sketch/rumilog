"""Step32: 不足3領域の代替候補3件のStage1/Stage2収集(一回限りの運用スクリプト)。
既存Phase3パイプライン(collect_batch等)のみを使用、新規ロジックは作らない。
product_masterへの本反映は行わない(staging/usageまで)。
"""
import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = f"p3-step32-{int(time.time())}"

PRODUCTS = [
    ("ちふれ", "濃厚化粧水", "化粧水", "hyaluronic_acid"),
    ("AMPLEN", "ペプチドショット(2X)美容液", "美容液", "peptide"),
    ("SKINFOOD", "どんぐり ポア ペプチド クリーム", "クリーム", "peptide"),
]

AREAS_13 = [
    ("化粧水", "hyaluronic_acid"), ("美容液", "vitamin_c"), ("化粧水", "ceramide"),
    ("美容液", "peptide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
    ("化粧水", "niacinamide"), ("洗顔", "salicylic_acid"),
    ("化粧水", "tranexamic_acid"), ("化粧水", "amino_acid"), ("洗顔", "glycolic_acid"),
    ("クリーム", "peptide"), ("クレンジング", "centella_extract"),
]

NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000

# Step31で反映可能と確認済みの5商品(data-quality基準でreflectable、targetが
# 部分的にでも確認できたタグはそのまま使う。新しいロジックではなく、
# Step31の実際の結果をそのまま再利用するだけ)
STEP31_REFLECTABLE = [
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
    pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = 0.30

    before_gemini_usage = get_gemini_usage_count()
    print(f"[BEFORE] 診断用gemini_usage count = {before_gemini_usage}")

    db_products = app.load_products()
    verified_products = app.load_verified_products_cache()

    TARGET_AREAS = [("化粧水", "hyaluronic_acid"), ("美容液", "peptide"), ("クリーム", "peptide")]
    before_target = {a: effective_candidates(*a, db_products, verified_products, STEP31_REFLECTABLE) for a in TARGET_AREAS}
    print("\n[BEFORE](Step31反映分を仮追加した状態) 対象3領域の実効候補数:")
    for a, n in before_target.items():
        print(f"  {a}: {n}")

    selected = [{"brand": b, "name": n, "category": c, "active_ingredients": []} for b, n, c, _ in PRODUCTS]
    pipeline.log_pilot_selection(BATCH_ID, selected)

    def product_master_lookup(brand, name, category):
        results = app.query_product_master_candidates(category)
        key = app._normalize_product_master_identity_key(brand, name, category)
        for r in results:
            if app._normalize_product_master_identity_key(r.get("brand", ""), r.get("name", ""), r.get("category", "")) == key:
                return r
        return None

    results = pipeline.collect_batch(
        [{"brand": b, "name": n, "category": c} for b, n, c, _ in PRODUCTS],
        BATCH_ID, product_master_lookup=product_master_lookup,
    )

    after_gemini_usage = get_gemini_usage_count()
    print(f"\n[AFTER] 診断用gemini_usage count = {after_gemini_usage}")
    print(f"診断用gemini_usage不変: {before_gemini_usage == after_gemini_usage}")
    print(f"\n処理数: {len(results)}/{len(PRODUCTS)}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, product_name, category, stage1_status, stage1_citations, stage2_status,
               stage2_payload, conflict_status, stage1_search_queries
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    staged_rows = cur.fetchall()

    product_target_map = {(b, n, c): t for b, n, c, t in PRODUCTS}

    existing_keys = set()
    master_all = []
    for cat in {c for _, _, c, _ in PRODUCTS}:
        master_all.extend(app.query_product_master_candidates(cat, limit=50))
    for p in db_products + master_all + verified_products:
        k = app.make_verified_product_key(p)
        if k:
            existing_keys.add(k)

    per_product = []
    total_search_queries = 0
    print("\n===== 商品別結果 =====")
    for brand, name, category, stage1_status, citations, stage2_status, payload, conflict_status, search_queries in staged_rows:
        total_search_queries += len(search_queries or [])
        target = product_target_map.get((brand, name, category))
        payload = payload or {}
        active_ingredients = [
            i.get("ingredient") for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        tags = app.compute_ingredient_tags(active_ingredients)
        target_hit = target in tags if target else None

        own_key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        is_duplicate = own_key in existing_keys

        reflectable = stage2_status == "ok" and conflict_status != "needs_review" and not is_duplicate

        per_product.append({
            "brand": brand, "name": name, "category": category, "target": target,
            "stage1_status": stage1_status, "stage2_status": stage2_status,
            "conflict_status": conflict_status, "active_ingredients": active_ingredients,
            "tags": tags, "target_hit": target_hit, "is_duplicate": is_duplicate,
            "reflectable": reflectable, "citation_count": len(citations or []),
        })
        print(f"\n[{category}] {brand} / {name}")
        print(f"  狙ったtag: {target}")
        print(f"  stage1={stage1_status} (citation数={len(citations or [])}) stage2={stage2_status} conflict={conflict_status}")
        print(f"  抽出active_ingredients: {active_ingredients}")
        print(f"  active_ingredient_tags(computed): {tags}")
        print(f"  target_hit: {target_hit}")
        print(f"  identity重複: {is_duplicate}")
        print(f"  reflect可能(データ品質基準): {reflectable}")

    conn.close()

    print("\n\n===== 対象3領域の達成確認 =====")
    reflectable_new = [
        {"brand": p["brand"], "name": p["name"], "category": p["category"], "active_ingredients": p["tags"]}
        for p in per_product if p["reflectable"]
    ]
    all_extra = STEP31_REFLECTABLE + reflectable_new

    all_3_achieved = True
    for category, tag in TARGET_AREAS:
        after = effective_candidates(category, tag, db_products, verified_products, all_extra)
        before = before_target[(category, tag)]
        sufficient = after >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        print(f"  {category} x {tag}: {before} -> {after} sufficient(>=3)={sufficient}")
        if not sufficient:
            all_3_achieved = False

    if all_3_achieved:
        print("\n対象3領域すべて達成。13領域全体を再計算します。")
        print("\n===== 13領域(P1 8+P2 5) 全体再計算(Step27と同じdedup後基準) =====")
        all_sufficient = True
        for category, tag in AREAS_13:
            n = effective_candidates(category, tag, db_products, verified_products, all_extra)
            sufficient = n >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
            print(f"  {category} x {tag}: {n} sufficient={sufficient}")
            if not sufficient:
                all_sufficient = False
        print(f"\n13/13すべてsufficient: {all_sufficient}")
    else:
        print("\n対象3領域のうち未達成あり。13領域全体の再計算はスキップします。追加候補は自動収集しません。")

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
    for stage, n, pt, ct, tt, cost, grounding in usage_rows:
        print(f"  stage={stage}: calls={n} prompt_tokens={pt} candidates_tokens={ct} "
              f"total_tokens={tt} grounding使用={grounding} cost=${float(cost or 0):.4f}")
    print(f"\n  総API call数: {total_calls}")
    print(f"  総search query数: {total_search_queries}")
    print(f"  推定総費用: ${float(total_cost or 0):.4f} (上限$0.30)")


if __name__ == "__main__":
    main()
