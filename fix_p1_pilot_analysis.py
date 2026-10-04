"""run_p1_coverage_pilot_12.pyの集計で、仮想候補のname/brandをplaceholderに
していたため洗顔カテゴリのis_wrong_cleanser_candidate等の名称依存チェックが
誤爆していた。実際のbrand/nameを使って正しく再集計する(読み取り専用、
追加API呼び出しなし)。"""
import os
import psycopg2
import app

BATCH_ID = "p1-coverage-1791094432"
NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000

PRIORITY_COMBOS = [
    ("化粧水", "ceramide"), ("化粧水", "hyaluronic_acid"),
    ("美容液", "vitamin_c"), ("美容液", "peptide"),
    ("洗顔", "salicylic_acid"), ("ピーリング", "aha"),
    ("クリーム", "ceramide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
]


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


def staged_relevant(category, tag, brand, name, active_ingredients, active_ingredient_tags):
    merged_actives = list(dict.fromkeys((active_ingredients or []) + (active_ingredient_tags or [])))
    virtual = {
        "brand": brand, "name": name, "category": category, "price_ref": 0, "price": 0,
        "active_ingredients": merged_actives, "support_ingredients": [], "signature_ingredients": [],
        "concerns": [], "skin_types": [], "sensitive_ok": "unknown", "retinol_level": 0,
        "main_functions": [], "ingredient_focus": [], "ingredient_strength": {}, "formulation": [],
        "technology": [], "texture": "", "contraindications": [], "uv_level": {},
        "availability_japan": [], "image": "", "rakuten_link": "",
    }
    step = {"category": category, "purpose": "", "ingredient_focus": tag}
    reasons = []
    score = app.score_product(virtual, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
    return app._is_relevant_scored_candidate(score, reasons, tag)


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, product_name, category, stage1_status, stage2_status, stage2_payload, conflict_status
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    rows = cur.fetchall()
    conn.close()

    staged = []
    for brand, name, category, stage1_status, stage2_status, payload, conflict_status in rows:
        payload = payload or {}
        active_ingredients = [
            i.get("ingredient") for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        tags = app.compute_ingredient_tags(active_ingredients)
        reflectable = stage2_status == "ok" and conflict_status != "needs_review"
        staged.append({
            "brand": brand, "name": name, "category": category,
            "active_ingredients": active_ingredients, "tags": tags, "reflectable": reflectable,
        })

    before_relevant = {combo: current_relevant_count(*combo) for combo in PRIORITY_COMBOS}

    print("===== P1 8領域: 現在 -> 今回取得成功後(再集計、実際のbrand/name使用) =====")
    for category, tag in PRIORITY_COMBOS:
        base = before_relevant[(category, tag)]
        added = 0
        added_names = []
        for r in staged:
            if r["category"] != category or not r["reflectable"]:
                continue
            if staged_relevant(category, tag, r["brand"], r["name"], r["active_ingredients"], r["tags"]):
                added += 1
                added_names.append(f"{r['brand']}/{r['name']}")
        total = base + added
        sufficient = total >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        print(f"  {category} x {tag}: {base} -> {total} (+{added}) {added_names} sufficient={sufficient}")


if __name__ == "__main__":
    main()
