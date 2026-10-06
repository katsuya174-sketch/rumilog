"""Step31: P3 6商品のStage1/Stage2収集(一回限りの運用スクリプト)。
既存Phase3パイプライン(collect_batch等)のみを使用し、新しい収集ロジックは
作らない。product_masterへの本反映は行わない(staging/usageまで)。
診断用Gemini利用枠(gemini_usage)には触れないcall_gemini_for_collection()
経路のみを使用する既存の仕組みをそのまま再利用する。
"""
import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = f"p3-step31-{int(time.time())}"

# (brand, name, category, [狙うtagリスト])
PRODUCTS = [
    ("プラスキレイ", "プラスレチAセラム", "美容液", ["retinol", "peptide"]),
    ("プラスキレイ", "プラスピュアVC28", "美容液", ["vitamin_c"]),
    ("ハルメク", "C35プレミアム", "美容液", ["vitamin_c"]),
    ("ETVOS", "モイスチャライジングローション", "化粧水", ["hyaluronic_acid", "amino_acid"]),
    ("肌ラボ", "極潤 薬用ハリ化粧水", "化粧水", ["hyaluronic_acid"]),
    ("VT", "レチナール ペプチド カプセルクリーム", "クリーム", ["peptide"]),
]

AREAS = [
    ("化粧水", "hyaluronic_acid"),
    ("美容液", "vitamin_c"),
    ("美容液", "peptide"),
    ("美容液", "retinol"),
    ("化粧水", "amino_acid"),
    ("クリーム", "peptide"),
]

NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000


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
    """Step27と同じdedup後の実効候補基準。extra_candidatesは今回の新商品
    (reflect可能かつtarget_hitのものだけ)を仮追加して再計算する。"""

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
    pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = 0.50

    before_gemini_usage = get_gemini_usage_count()
    print(f"[BEFORE] 診断用gemini_usage count = {before_gemini_usage}")

    db_products = app.load_products()
    verified_products = app.load_verified_products_cache()
    before_effective = {area: effective_candidates(*area, db_products, verified_products, []) for area in AREAS}
    print("\n[BEFORE] 現在の実効候補数(dedup後):")
    for area, n in before_effective.items():
        print(f"  {area}: {n}")

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

    product_target_map = {(b, n, c): tags for b, n, c, tags in PRODUCTS}
    per_product = []
    total_search_queries = 0

    # identity重複確認(db_products + product_master + verified_cache)
    existing_keys = set()
    master_all = []
    for cat in {c for _, _, c, _ in PRODUCTS}:
        master_all.extend(app.query_product_master_candidates(cat, limit=50))
    for p in db_products + master_all + verified_products:
        k = app.make_verified_product_key(p)
        if k:
            existing_keys.add(k)

    print("\n===== 商品別結果 =====")
    for brand, name, category, stage1_status, citations, stage2_status, payload, conflict_status, search_queries in staged_rows:
        total_search_queries += len(search_queries or [])
        targets = product_target_map.get((brand, name, category)) or []
        payload = payload or {}
        active_ingredients = [
            i.get("ingredient") for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        tags = app.compute_ingredient_tags(active_ingredients)
        targets_hit = [t for t in targets if t in tags]
        targets_missing = [t for t in targets if t not in tags]
        all_targets_hit = len(targets_missing) == 0

        own_key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        is_duplicate = own_key in existing_keys

        reflectable = (
            stage2_status == "ok"
            and conflict_status != "needs_review"
            and all_targets_hit
            and not is_duplicate
        )

        per_product.append({
            "brand": brand, "name": name, "category": category, "targets": targets,
            "stage1_status": stage1_status, "stage2_status": stage2_status,
            "conflict_status": conflict_status, "active_ingredients": active_ingredients,
            "tags": tags, "targets_hit": targets_hit, "targets_missing": targets_missing,
            "all_targets_hit": all_targets_hit, "is_duplicate": is_duplicate,
            "reflectable": reflectable, "citation_count": len(citations or []),
        })
        print(f"\n[{category}] {brand} / {name}")
        print(f"  狙ったtag: {targets}")
        print(f"  stage1={stage1_status} (citation数={len(citations or [])}) stage2={stage2_status} conflict={conflict_status}")
        print(f"  抽出active_ingredients: {active_ingredients}")
        print(f"  active_ingredient_tags(computed): {tags}")
        print(f"  target充足: hit={targets_hit} missing={targets_missing} -> all_hit={all_targets_hit}")
        print(f"  identity重複(db/master/verified): {is_duplicate}")
        print(f"  reflect可能: {reflectable}")

    conn.close()

    print("\n\n===== 予測実効候補の再計算(reflect可能かつtarget充足の商品のみ仮追加) =====")
    reflectable_products = []
    for p in per_product:
        if p["reflectable"]:
            reflectable_products.append({
                "brand": p["brand"], "name": p["name"], "category": p["category"],
                "active_ingredients": p["tags"],
            })

    insufficient_areas = []
    for category, tag in AREAS:
        after_count = effective_candidates(category, tag, db_products, verified_products, reflectable_products)
        before_count = before_effective[(category, tag)]
        sufficient = after_count >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        print(f"  {category} x {tag}: 修正前={before_count} -> 予測後={after_count} sufficient(>=3)={sufficient}")
        if not sufficient:
            insufficient_areas.append((category, tag, after_count))

    if insufficient_areas:
        print("\n*** 不足している領域(追加候補が必要、自動追加はしない) ***")
        for category, tag, n in insufficient_areas:
            print(f"  {category} x {tag}: 予測実効候補={n} (3未満)")
    else:
        print("\n全6領域が予測実効候補3以上に到達。")

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
    print(f"  推定総費用: ${float(total_cost or 0):.4f} (上限$0.50)")


if __name__ == "__main__":
    main()
