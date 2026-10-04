"""Phase 3 P1カバレッジ拡張パイロット: 確定済み12商品のStage1/Stage2収集
(一回限りの運用スクリプト)。product_masterへの本反映は行わない(staging
まで)。費用上限$1.00。診断用Gemini利用枠(gemini_usage)には一切触れない
call_gemini_for_collection()経路のみを使用する(既存の仕組みをそのまま再利用)。
"""

import json
import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = f"p1-coverage-{int(time.time())}"

# (brand, name, category, 狙うtag, 同時に埋める他tag)
PRODUCTS = [
    ("CeraVe", "CeraVe PMフェイシャル モイスチャライジング ローション", "化粧水", "ceramide", ["hyaluronic_acid", "niacinamide"]),
    ("CEZANNE", "CEZANNE スキンコンディショナー 高保湿", "化粧水", "ceramide", []),
    ("ONE THING", "ONE THING ナイアシンアミド化粧水", "化粧水", "niacinamide", []),
    ("medicube", "medicube アゼライン酸ナイアシンアミド クリアトナー", "化粧水", "niacinamide", []),
    ("メラノCC", "メラノCC 薬用しみ集中対策 プレミアム美容液", "美容液", "vitamin_c", []),
    ("Anua", "Anua レチノール0.3 ナイアシンリニューイングセラム", "美容液", "retinol", ["niacinamide"]),
    ("明色化粧品", "明色化粧品 メディショットNA15 リンクル濃美容液", "美容液", "niacinamide", ["retinol"]),
    ("成分エディター", "成分エディター シルクペプチドEGFハートフィット ボリュームアンプル", "美容液", "peptide", []),
    ("ドクターペプチ", "ドクターペプチ ペプチドボリュームエッセンス", "美容液", "peptide", []),
    ("クレラシル", "クレラシル 薬用洗顔クリーム マイルドタイプ", "洗顔", "salicylic_acid", []),
    ("ノブ", "ノブ ACアクティブ ウォッシングフォーム", "洗顔", "salicylic_acid", []),
    ("富士フイルム", "富士フイルム ルナメアAC ファイバーフォーム", "洗顔", "salicylic_acid", []),
]

PRIORITY_COMBOS = [
    ("化粧水", "ceramide"), ("化粧水", "hyaluronic_acid"),
    ("美容液", "vitamin_c"), ("美容液", "peptide"),
    ("洗顔", "salicylic_acid"), ("ピーリング", "aha"),
    ("クリーム", "ceramide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
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
    """現状のproduct_master(今回の12商品は含まない)でのrelevant match数。"""
    step = {"category": category, "purpose": "", "ingredient_focus": tag}
    candidates = [c for c in app.query_product_master_candidates(category, limit=50) if c.get("_source") == "product_master"]
    count = 0
    for c in candidates:
        reasons = []
        score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if app._is_relevant_scored_candidate(score, reasons, tag):
            count += 1
    return count


def staged_relevant(category, tag, active_ingredients, active_ingredient_tags):
    """stagingペイロード(未反映)を仮のproduct_master候補として扱い、
    score_product()でそのtagについて関連性ありと判定されるか確認する
    (reflect_staging_to_product_masterと同じ合流ロジックを再現)。"""
    merged_actives = list(dict.fromkeys((active_ingredients or []) + (active_ingredient_tags or [])))
    virtual = {
        "brand": "x", "name": "y", "category": category, "price_ref": 0, "price": 0,
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
    pipeline.init_product_collection_tables()
    pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = 1.0

    before_gemini_usage = get_gemini_usage_count()
    print(f"[BEFORE] 診断用gemini_usage count = {before_gemini_usage}")

    before_relevant = {combo: current_relevant_count(*combo) for combo in PRIORITY_COMBOS}

    selected = [{"brand": b, "name": n, "category": c, "active_ingredients": []} for b, n, c, _, _ in PRODUCTS]
    pipeline.log_pilot_selection(BATCH_ID, selected)

    def product_master_lookup(brand, name, category):
        results = app.query_product_master_candidates(category)
        key = app._normalize_product_master_identity_key(brand, name, category)
        for r in results:
            if app._normalize_product_master_identity_key(r.get("brand", ""), r.get("name", ""), r.get("category", "")) == key:
                return r
        return None

    results = pipeline.collect_batch(
        [{"brand": b, "name": n, "category": c} for b, n, c, _, _ in PRODUCTS],
        BATCH_ID, product_master_lookup=product_master_lookup,
    )

    after_gemini_usage = get_gemini_usage_count()
    print(f"[AFTER] 診断用gemini_usage count = {after_gemini_usage}")
    print(f"診断用gemini_usage不変: {before_gemini_usage == after_gemini_usage}")

    print(f"\n処理数: {len(results)}/{len(PRODUCTS)}")

    # ===== staging内容の取得 =====
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, product_name, category, stage1_status, stage2_status,
               stage2_payload, conflict_status, stage1_search_queries
        FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id
    """, (BATCH_ID,))
    staged_rows = cur.fetchall()

    print("\n===== 商品別結果 =====")
    product_target_map = {(b, n, c): (target, co_tags) for b, n, c, target, co_tags in PRODUCTS}
    master_reflectable = 0
    per_product_rows = []
    total_search_queries_from_staging = 0
    for brand, name, category, stage1_status, stage2_status, payload, conflict_status, search_queries in staged_rows:
        total_search_queries_from_staging += len(search_queries or [])
        target, co_tags = product_target_map.get((brand, name, category), (None, []))
        payload = payload or {}
        active_ingredients = [
            i.get("ingredient") for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        tags = app.compute_ingredient_tags(active_ingredients)
        target_hit = target in tags if target else None
        co_hits = [t for t in co_tags if t in tags]
        reflectable = stage2_status == "ok" and conflict_status != "needs_review"
        if reflectable:
            master_reflectable += 1
        per_product_rows.append({
            "brand": brand, "name": name, "category": category, "target": target,
            "stage1_status": stage1_status, "stage2_status": stage2_status,
            "conflict_status": conflict_status, "active_ingredients": active_ingredients,
            "tags": tags, "target_hit": target_hit, "co_hits": co_hits, "reflectable": reflectable,
        })
        print(f"\n[{category}] {brand} / {name}")
        print(f"  狙ったtag: {target}")
        print(f"  stage1={stage1_status} stage2={stage2_status} conflict={conflict_status}")
        print(f"  抽出active_ingredients: {active_ingredients}")
        print(f"  active_ingredient_tags: {tags}")
        print(f"  狙ったtagがcitation-backedで取得できたか: {target_hit}")
        if co_tags:
            print(f"  同時タグ({co_tags})の取得結果: {co_hits}")

    conn.close()

    print(f"\nmaster反映可能(stage2=ok かつ conflict!=needs_review)件数: {master_reflectable}/{len(PRODUCTS)}")

    needs_review = [r for r in per_product_rows if r["conflict_status"] == "needs_review"]
    stage1_failed = [r for r in per_product_rows if r["stage1_status"] != "ok"]
    stage2_failed = [r for r in per_product_rows if r["stage1_status"] == "ok" and r["stage2_status"] != "ok"]
    print(f"\nStage1失敗: {len(stage1_failed)}件 {[r['name'] for r in stage1_failed]}")
    print(f"Stage2失敗(Stage1成功のみ対象): {len(stage2_failed)}件 {[r['name'] for r in stage2_failed]}")
    print(f"needs_review: {len(needs_review)}件 {[r['name'] for r in needs_review]}")

    # ===== P1 8領域の実測match数(現在 -> 今回staging成功分を加味) =====
    print("\n===== P1 8領域: 現在 -> 今回取得成功後(実測、未反映のためproduct_master自体は変更なし) =====")
    for combo in PRIORITY_COMBOS:
        category, tag = combo
        base = before_relevant[combo]
        added = 0
        for r in per_product_rows:
            if r["category"] != category or not r["reflectable"]:
                continue
            if staged_relevant(category, tag, r["active_ingredients"], r["tags"]):
                added += 1
        total = base + added
        sufficient = total >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        print(f"  {category} x {tag}: {base} -> {total} (+{added})  sufficient={sufficient}")

    # ===== API使用量・費用 =====
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

    print(f"\n===== API使用量・費用 (batch_id={BATCH_ID}) =====")
    for stage, n, pt, ct, tt, cost, grounding in usage_rows:
        print(f"  stage={stage}: calls={n} prompt_tokens={pt} candidates_tokens={ct} "
              f"total_tokens={tt} grounding使用={grounding} cost=${float(cost or 0):.4f}")
    print(f"\n  総API call数: {total_calls}")
    print(f"  総search query数(stage1_search_queries合計): {total_search_queries_from_staging}")
    print(f"  推定総費用: ${float(total_cost or 0):.4f}")
    print(f"  費用上限: $1.00")


if __name__ == "__main__":
    main()
