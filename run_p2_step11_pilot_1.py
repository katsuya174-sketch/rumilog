"""Phase 3 P2 Step11: amino_acid領域最終1商品(松山油脂 アミノ酸浸透水)の
Stage1/Stage2収集(一回限りの運用スクリプト)。product_masterへの本反映は
行わない(stagingまで)。費用上限$0.08。診断用Gemini利用枠(gemini_usage)
には一切触れないcall_gemini_for_collection()経路のみを使用する。
"""

import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

BATCH_ID = f"p2-step11-{int(time.time())}"
STEP7_BATCH_ID = "p2-step7-1791276523"

PRODUCT = ("松山油脂", "松山油脂 アミノ酸浸透水", "化粧水", "amino_acid")

AREAS = [
    ("化粧水", "tranexamic_acid"), ("化粧水", "amino_acid"), ("洗顔", "glycolic_acid"),
    ("クリーム", "peptide"), ("クレンジング", "centella_extract"),
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


def get_step7_reflectable_tags():
    """Step7 staging(13件)のうち、citation-backedでtarget tagを獲得済みの
    商品を(category, tag) -> 件数 で返す(まだ未反映、集計のみ)。"""
    target_map = {
        ("化粧水", "tranexamic_acid"): ["トランシーノ 薬用ブライトニングクリアローション", "肌ラボ 白潤薬用美白化粧水 しっとりタイプ", "ちふれ 美白化粧水 TA"],
        ("化粧水", "amino_acid"): [],
        ("洗顔", "glycolic_acid"): ["サンソリット ウルピールフォーム", "ドクターサニー AHAクリアソープ", "サンソリット スキンピールバー AHA"],
        ("クリーム", "peptide"): ["トゥヴェール フェイスクリーム パワーアクティブ", "APLB コラーゲン EGF ペプチド フェイシャルクリーム"],
        ("クレンジング", "centella_extract"): ["ツボクサレディ マイルドクレンジングクリーム", "セザンヌ うるオフ クレンジングバーム", "Sitrana シカプロテクト クレンジングバーム"],
    }
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_name, stage2_payload, conflict_status, stage2_status
        FROM product_collection_staging WHERE batch_id = %s
    """, (STEP7_BATCH_ID,))
    rows = cur.fetchall()
    conn.close()

    counts = {}
    for category, tag in AREAS:
        names = target_map[(category, tag)]
        count = 0
        for name, payload, conflict_status, stage2_status in rows:
            if name not in names:
                continue
            if stage2_status != "ok" or conflict_status == "needs_review":
                continue
            payload = payload or {}
            actives = [i.get("ingredient") for i in (payload.get("active_ingredients") or [])
                       if str(i.get("ingredient", "") or "").strip().lower() != "unknown"]
            tags = app.compute_ingredient_tags(actives)
            if tag in tags:
                count += 1
        counts[(category, tag)] = count
    return counts


def main():
    pipeline.init_product_collection_tables()
    pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = 0.08

    before_gemini_usage = get_gemini_usage_count()
    print(f"[BEFORE] 診断用gemini_usage count = {before_gemini_usage}")
    before_relevant = {area: current_relevant_count(*area) for area in AREAS}

    brand, name, category, target_tag = PRODUCT
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
    print(f"[AFTER] 診断用gemini_usage count = {after_gemini_usage}")
    print(f"診断用gemini_usage不変: {before_gemini_usage == after_gemini_usage}")
    print(f"\n処理数: {len(results)}/1")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, product_name, category, stage1_status, stage2_status,
               stage2_payload, conflict_status, stage1_search_queries
        FROM product_collection_staging WHERE batch_id = %s
    """, (BATCH_ID,))
    row = cur.fetchone()
    conn.close()

    if not row:
        print("[ERROR] stagingレコードが見つかりません")
        return

    b, n, c, stage1_status, stage2_status, payload, conflict_status, search_queries = row
    payload = payload or {}
    active_ingredients = [
        i.get("ingredient") for i in (payload.get("active_ingredients") or [])
        if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
    ]
    tags = app.compute_ingredient_tags(active_ingredients)
    target_hit = target_tag in tags
    reflectable = stage2_status == "ok" and conflict_status != "needs_review" and target_hit

    print(f"\n[{c}] {b} / {n}")
    print(f"  狙ったtag: {target_tag}")
    print(f"  stage1={stage1_status} stage2={stage2_status} conflict={conflict_status}")
    print(f"  抽出active_ingredients: {active_ingredients}")
    print(f"  active_ingredient_tags: {tags}")
    print(f"  target_hit(citation-backed, amino_acid付与): {target_hit}")
    print(f"  反映可否: {'反映可能' if reflectable else '反映不可'}")

    # P2 5領域: Step7の13件 + 今回1件を加味した見込みcoverage
    step7_counts = get_step7_reflectable_tags()
    print("\n===== P2 5領域 反映後coverage見込み(Step7 + 今回) =====")
    all_sufficient = True
    for category, tag in AREAS:
        base = before_relevant[(category, tag)]
        added = step7_counts[(category, tag)]
        if category == category and tag == target_tag and reflectable:
            added += 1
        total = base + added
        sufficient = total >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        all_sufficient = all_sufficient and sufficient
        print(f"  {category} x {tag}: 現master={base} + 反映可能見込み={added} = {total}  sufficient={sufficient}")
    print(f"\n  P2 5/5 sufficient到達見込み: {all_sufficient}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(estimated_cost_usd) FROM product_collection_usage WHERE batch_id = %s", (BATCH_ID,))
    total_calls, total_cost = cur.fetchone()
    conn.close()
    print(f"\n総API call数: {total_calls}")
    print(f"総search query数: {len(search_queries or [])}")
    print(f"推定費用: ${float(total_cost or 0):.4f} (上限$0.08)")


if __name__ == "__main__":
    main()
