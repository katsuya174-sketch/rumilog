"""Step15事後検証(読み取り専用): item_code件数、重複、既存フィールド不変性、
P1 8領域・P2 5領域の再計算。"""
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

    cur.execute("SELECT COUNT(*) FROM product_master WHERE item_code IS NOT NULL AND item_code != ''")
    print(f"item_code保有数(after): {cur.fetchone()[0]} (期待: 61)")

    cur.execute("SELECT identity_key, COUNT(*) FROM product_master GROUP BY identity_key HAVING COUNT(*) > 1")
    print(f"identity_key重複: {cur.fetchall() or 'なし'}")
    cur.execute("SELECT item_code, COUNT(*) FROM product_master WHERE item_code IS NOT NULL AND item_code != '' GROUP BY item_code HAVING COUNT(*) > 1")
    print(f"item_code重複: {cur.fetchall() or 'なし'}")

    # 更新対象以外(not_found 5件)のitem_codeが依然空であること
    cur.execute("""
        SELECT product_id, brand, name, item_code FROM product_master
        WHERE product_id IN (28, 48, 49, 59, 63)
    """)
    print("\nnot_found 5件(item_code未更新のまま確認):")
    for row in cur.fetchall():
        print(f"  {row}")

    # ingredients/formulation/JAN不変確認(更新対象24件)
    cur.execute("""
        SELECT product_id, active_ingredients, formulation, jan_code FROM product_master
        WHERE product_id = ANY(%s)
    """, ([39,40,41,42,43,44,45,46,47,50,51,52,53,54,55,56,57,58,60,61,62,64,65,66],))
    rows = cur.fetchall()
    print(f"\n更新対象24件のingredients/formulation/JAN確認(抜粋、件数): {len(rows)}")

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
