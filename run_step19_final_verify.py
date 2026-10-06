"""Step19-5最終検証(読み取り専用): item_code件数、NULL内訳、重複、61+件全件
新セット判定、P1 8領域・P2 5領域、identity訂正によるbrand/name/identity_key
以外の不変性確認。"""
import os
import psycopg2
import app

P1_COMBOS_8 = [
    ("化粧水", "hyaluronic_acid"), ("美容液", "vitamin_c"), ("化粧水", "ceramide"),
    ("美容液", "peptide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
    ("化粧水", "niacinamide"), ("洗顔", "salicylic_acid"),
]
P2_AREAS_5 = [
    ("化粧水", "tranexamic_acid"), ("化粧水", "amino_acid"), ("洗顔", "glycolic_acid"),
    ("クリーム", "peptide"), ("クレンジング", "centella_extract"),
]
NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000


def relevant_count(category, tag):
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
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM product_master")
    print(f"product_master総件数: {cur.fetchone()[0]} (期待: 66)")

    cur.execute("SELECT COUNT(*) FROM product_master WHERE item_code IS NOT NULL AND item_code != ''")
    print(f"item_code保有数: {cur.fetchone()[0]}")

    cur.execute("""
        SELECT product_id, brand, name FROM product_master
        WHERE item_code IS NULL OR item_code = '' ORDER BY product_id
    """)
    null_rows = cur.fetchall()
    print(f"item_code=NULLの件数: {len(null_rows)}")
    for r in null_rows:
        print(f"  {r}")

    cur.execute("SELECT identity_key, COUNT(*) FROM product_master GROUP BY identity_key HAVING COUNT(*) > 1")
    print(f"\nidentity_key重複: {cur.fetchall() or 'なし'}")
    cur.execute("SELECT item_code, COUNT(*) FROM product_master WHERE item_code IS NOT NULL AND item_code != '' GROUP BY item_code HAVING COUNT(*) > 1")
    print(f"item_code重複: {cur.fetchall() or 'なし'}")

    cur.execute("""
        SELECT product_id, rakuten_title FROM product_master
        WHERE item_code IS NOT NULL AND item_code != ''
    """)
    rows = cur.fetchall()
    conn_rows_count = len(rows)
    set_flagged = [(pid, t) for pid, t in rows if app._is_rakuten_set_item(t or "")]
    print(f"\nitem_code保有{conn_rows_count}件のうち新セット判定された件数: {len(set_flagged)} (期待: 0)")
    for pid, t in set_flagged:
        print(f"  product_id={pid} title={t}")

    # product_id=49: identity訂正でbrand/name/identity_key以外が変化していないか
    cur.execute("""
        SELECT jan_code, active_ingredients, formulation, active_ingredient_tags, price_ref
        FROM product_master WHERE product_id = 49
    """)
    row49 = cur.fetchone()
    print(f"\nproduct_id=49 (identity訂正後) jan/actives/formulation/tags/price_ref: {row49}")

    conn.close()

    print("\nP1 8領域 実データ再計算:")
    p1_sufficient = 0
    for category, tag in P1_COMBOS_8:
        n = relevant_count(category, tag)
        sufficient = n >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        if sufficient:
            p1_sufficient += 1
        print(f"  {category} x {tag}: relevant={n} sufficient={sufficient}")
    print(f"  P1 sufficient到達数: {p1_sufficient}/8")

    print("\nP2 5領域 実データ再計算:")
    p2_sufficient = 0
    for category, tag in P2_AREAS_5:
        n = relevant_count(category, tag)
        sufficient = n >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        if sufficient:
            p2_sufficient += 1
        print(f"  {category} x {tag}: relevant={n} sufficient={sufficient}")
    print(f"  P2 sufficient到達数: {p2_sufficient}/5")


if __name__ == "__main__":
    main()
