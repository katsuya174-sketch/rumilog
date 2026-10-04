"""Phase 3 P1カバレッジ拡張 Round3(最終2商品): センカ・ビフェスタの
Stage1/Stage2収集(一回限りの運用スクリプト)。product_masterへの本反映は
行わない(stagingまで)。費用上限$0.3878(Round1 $0.3750 + Round2 $0.2372
= $0.6122 と合わせてP1累計$1.00以内)。診断用Gemini利用枠には一切触れない
call_gemini_for_collection()経路のみを使用する。
"""

import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = f"p1-coverage-round3-{int(time.time())}"
ROUND1_BATCH_ID = "p1-coverage-1791094432"
ROUND2_BATCH_ID = "p1-coverage-round2-1791095683"

PRODUCTS = [
    ("センカ", "センカ プレミアムパーフェクトホイップ クリア", "洗顔", "salicylic_acid"),
    ("ビフェスタ", "ビフェスタ 泡ピーリング洗顔 ポアクリア", "洗顔", "salicylic_acid"),
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


def current_salicylic_relevant():
    step = {"category": "洗顔", "purpose": "", "ingredient_focus": "salicylic_acid"}
    candidates = [c for c in app.query_product_master_candidates("洗顔", limit=50) if c.get("_source") == "product_master"]
    count = 0
    for c in candidates:
        reasons = []
        score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if app._is_relevant_scored_candidate(score, reasons, "salicylic_acid"):
            count += 1
    return count


def staged_relevant(brand, name, active_ingredients, active_ingredient_tags):
    merged_actives = list(dict.fromkeys((active_ingredients or []) + (active_ingredient_tags or [])))
    virtual = {
        "brand": brand, "name": name, "category": "洗顔", "price_ref": 0, "price": 0,
        "active_ingredients": merged_actives, "support_ingredients": [], "signature_ingredients": [],
        "concerns": [], "skin_types": [], "sensitive_ok": "unknown", "retinol_level": 0,
        "main_functions": [], "ingredient_focus": [], "ingredient_strength": {}, "formulation": [],
        "technology": [], "texture": "", "contraindications": [], "uv_level": {},
        "availability_japan": [], "image": "", "rakuten_link": "",
    }
    step = {"category": "洗顔", "purpose": "", "ingredient_focus": "salicylic_acid"}
    reasons = []
    score = app.score_product(virtual, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
    return app._is_relevant_scored_candidate(score, reasons, "salicylic_acid")


def main():
    pipeline.init_product_collection_tables()
    pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = 0.3878

    before_gemini_usage = get_gemini_usage_count()
    print(f"[BEFORE] 診断用gemini_usage count = {before_gemini_usage}")

    before_relevant = current_salicylic_relevant()

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
               stage2_payload, conflict_status
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    staged_rows = cur.fetchall()

    print("\n===== 商品別結果 =====")
    master_reflectable = 0
    per_product_rows = []
    for brand, name, category, stage1_status, stage2_status, payload, conflict_status in staged_rows:
        payload = payload or {}
        active_ingredients = [
            i.get("ingredient") for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        tags = app.compute_ingredient_tags(active_ingredients)
        target_hit = "salicylic_acid" in tags
        reflectable = stage2_status == "ok" and conflict_status != "needs_review"
        if reflectable:
            master_reflectable += 1
        per_product_rows.append({
            "brand": brand, "name": name, "active_ingredients": active_ingredients,
            "tags": tags, "target_hit": target_hit, "reflectable": reflectable,
        })
        print(f"\n{brand} / {name}")
        print(f"  stage1={stage1_status} stage2={stage2_status} conflict={conflict_status}")
        print(f"  抽出active_ingredients: {active_ingredients}")
        print(f"  active_ingredient_tags: {tags}")
        print(f"  salicylic_acidがcitation-backedで確認できたか: {target_hit}")

    conn.close()

    print(f"\n反映可能(stage2=ok かつ conflict!=needs_review)件数: {master_reflectable}/{len(PRODUCTS)}")

    added = 0
    for r in per_product_rows:
        if r["reflectable"] and staged_relevant(r["brand"], r["name"], r["active_ingredients"], r["tags"]):
            added += 1
    total = before_relevant + added
    sufficient = total >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
    print(f"\n洗顔 x salicylic_acid: {before_relevant} -> {total} (+{added})  P1 8/8到達可否: {sufficient}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(estimated_cost_usd) FROM product_collection_usage WHERE batch_id = %s", (BATCH_ID,))
    total_calls, total_cost = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM product_collection_usage WHERE batch_id IN (%s, %s)",
        (ROUND1_BATCH_ID, ROUND2_BATCH_ID),
    )
    prior_cost = float(cur.fetchone()[0])
    conn.close()

    round3_cost = float(total_cost or 0)
    print(f"\n今回(Round3)API call数: {total_calls}  費用: ${round3_cost:.4f} (上限$0.3878)")
    print(f"Round1+2費用: ${prior_cost:.4f}")
    print(f"P1累計費用: ${prior_cost + round3_cost:.4f} (上限$1.00)")


if __name__ == "__main__":
    main()
