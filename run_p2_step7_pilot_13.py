"""Phase 3 P2 Step7: 5領域の不足を埋める13商品のStage1/Stage2収集
(一回限りの運用スクリプト)。product_masterへの本反映は行わない(staging
まで)。費用上限$0.60。診断用Gemini利用枠(gemini_usage)には一切触れない
call_gemini_for_collection()経路のみを使用する(既存の仕組みをそのまま再利用)。
"""

import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = f"p2-step7-{int(time.time())}"

# (brand, name, category, 狙うtag)
PRODUCTS = [
    ("第一三共ヘルスケア", "トランシーノ 薬用ブライトニングクリアローション", "化粧水", "tranexamic_acid"),
    ("肌ラボ", "肌ラボ 白潤薬用美白化粧水 しっとりタイプ", "化粧水", "tranexamic_acid"),
    ("ちふれ", "ちふれ 美白化粧水 TA", "化粧水", "tranexamic_acid"),
    ("ミノン", "ミノン アミノモイスト モイストチャージ ローションI しっとりタイプ", "化粧水", "amino_acid"),
    ("菊正宗", "菊正宗 日本酒の化粧水 高保湿", "化粧水", "amino_acid"),
    ("サンソリット", "サンソリット ウルピールフォーム", "洗顔", "glycolic_acid"),
    ("ドクターサニー", "ドクターサニー AHAクリアソープ", "洗顔", "glycolic_acid"),
    ("サンソリット", "サンソリット スキンピールバー AHA", "洗顔", "glycolic_acid"),
    ("トゥヴェール", "トゥヴェール フェイスクリーム パワーアクティブ", "クリーム", "peptide"),
    ("APLB", "APLB コラーゲン EGF ペプチド フェイシャルクリーム", "クリーム", "peptide"),
    ("ツボクサレディ", "ツボクサレディ マイルドクレンジングクリーム", "クレンジング", "centella_extract"),
    ("セザンヌ", "セザンヌ うるオフ クレンジングバーム", "クレンジング", "centella_extract"),
    ("Sitrana", "Sitrana シカプロテクト クレンジングバーム", "クレンジング", "centella_extract"),
]

AREAS = [
    ("化粧水", "tranexamic_acid"),
    ("化粧水", "amino_acid"),
    ("洗顔", "glycolic_acid"),
    ("クリーム", "peptide"),
    ("クレンジング", "centella_extract"),
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


def current_relevant_count(category, tag):
    step = {"category": category, "purpose": "", "ingredient_focus": tag}
    candidates = [c for c in app.query_product_master_candidates(category, limit=50) if c.get("_source") == "product_master"]
    count = 0
    for c in candidates:
        reasons = []
        score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if app._is_relevant_scored_candidate(score, reasons, tag):
            count += 1
    return count


def main():
    pipeline.init_product_collection_tables()
    pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = 0.60

    before_gemini_usage = get_gemini_usage_count()
    print(f"[BEFORE] 診断用gemini_usage count = {before_gemini_usage}")
    before_relevant = {area: current_relevant_count(*area) for area in AREAS}

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
    print(f"[AFTER] 診断用gemini_usage count = {after_gemini_usage}")
    print(f"診断用gemini_usage不変: {before_gemini_usage == after_gemini_usage}")
    print(f"\n処理数: {len(results)}/{len(PRODUCTS)}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, product_name, category, stage1_status, stage2_status,
               stage2_payload, conflict_status, stage1_search_queries
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    staged_rows = cur.fetchall()

    product_target_map = {(b, n, c): tag for b, n, c, tag in PRODUCTS}
    per_product = []
    total_search_queries = 0
    print("\n===== 商品別結果 =====")
    for brand, name, category, stage1_status, stage2_status, payload, conflict_status, search_queries in staged_rows:
        total_search_queries += len(search_queries or [])
        target = product_target_map.get((brand, name, category))
        payload = payload or {}
        active_ingredients = [
            i.get("ingredient") for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        tags = app.compute_ingredient_tags(active_ingredients)
        target_hit = target in tags if target else None
        reflectable = stage2_status == "ok" and conflict_status != "needs_review"
        per_product.append({
            "brand": brand, "name": name, "category": category, "target": target,
            "stage1_status": stage1_status, "stage2_status": stage2_status,
            "conflict_status": conflict_status, "active_ingredients": active_ingredients,
            "tags": tags, "target_hit": target_hit, "reflectable": reflectable,
        })
        print(f"\n[{category}] {brand} / {name}")
        print(f"  狙ったtag: {target}")
        print(f"  stage1={stage1_status} stage2={stage2_status} conflict={conflict_status}")
        print(f"  抽出active_ingredients: {active_ingredients}")
        print(f"  active_ingredient_tags: {tags}")
        print(f"  target_hit(citation-backed): {target_hit}")

    conn.close()

    print("\n\n===== 5領域別まとめ =====")
    for category, tag in AREAS:
        area_products = [p for p in per_product if p["category"] == category and p["target"] == tag]
        stage1_success = sum(1 for p in area_products if p["stage1_status"] == "ok")
        stage2_success = sum(1 for p in area_products if p["stage1_status"] == "ok" and p["stage2_status"] == "ok")
        target_confirmed = [p for p in area_products if p["target_hit"]]
        reflectable = [p for p in area_products if p["reflectable"] and p["target_hit"]]
        base = before_relevant[(category, tag)]
        total_after = base + len(reflectable)
        sufficient = total_after >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        print(f"\n[{category} x {tag}]")
        print(f"  Stage1成功数: {stage1_success}/{len(area_products)}")
        print(f"  Stage2成功数: {stage2_success}/{len(area_products)}")
        print(f"  target tagがcitation-backedで確認できた商品: {len(target_confirmed)}件 "
              f"{[p['name'] for p in target_confirmed]}")
        print(f"  反映可能件数(stage2=ok かつ conflict!=needs_review かつ target_hit): {len(reflectable)}")
        print(f"  現在master({base}件) + 今回反映可能({len(reflectable)}件) = {total_after}件  sufficient(>=3)={sufficient}")
        if len(reflectable) < 2:
            print(f"  *** 不足領域: 2件成功に届いていません({len(reflectable)}件) ***")

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
    print(f"  推定総費用: ${float(total_cost or 0):.4f} (上限$0.60)")


if __name__ == "__main__":
    main()
