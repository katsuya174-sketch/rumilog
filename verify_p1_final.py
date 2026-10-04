"""P1最終反映後の検証(読み取り専用): 件数、tag保持、identity/item_code重複、
P1 8領域8/8確認、既存50件の不変性。"""
import os
import psycopg2
import app

PRIORITY_COMBOS_8 = [
    ("化粧水", "hyaluronic_acid"), ("美容液", "vitamin_c"), ("化粧水", "ceramide"),
    ("美容液", "peptide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
    ("化粧水", "niacinamide"), ("洗顔", "salicylic_acid"),
]
NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM product_master")
    print(f"1. product_master総件数: {cur.fetchone()[0]} (期待: 52)")

    cur.execute("SELECT brand, name, active_ingredient_tags FROM product_master WHERE product_id IN (51, 52)")
    for brand, name, tags in cur.fetchall():
        print(f"2. {brand} / {name}: active_ingredient_tags={tags} salicylic_acid保持={'salicylic_acid' in (tags or [])}")

    cur.execute("SELECT identity_key, COUNT(*) FROM product_master GROUP BY identity_key HAVING COUNT(*) > 1")
    print(f"3a. identity_key重複: {cur.fetchall() or 'なし'}")
    cur.execute("SELECT item_code, COUNT(*) FROM product_master WHERE item_code IS NOT NULL AND item_code != '' GROUP BY item_code HAVING COUNT(*) > 1")
    print(f"3b. item_code重複: {cur.fetchall() or 'なし'}")

    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id<=38 AND item_code IS NOT NULL AND item_code != ''")
    print(f"\n5. 既存38件item_code保有(期待37): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id<=38 AND jan_code IS NOT NULL")
    print(f"   既存38件jan_code保有(期待32): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id BETWEEN 39 AND 50")
    print(f"   Round1+2反映の12件存在確認(期待12): {cur.fetchone()[0]}")
    conn.close()

    print("\n4. P1 8領域 実データ再計算:")
    sufficient_count = 0
    for category, tag in PRIORITY_COMBOS_8:
        step = {"category": category, "purpose": "", "ingredient_focus": tag}
        candidates = [c for c in app.query_product_master_candidates(category, limit=50) if c.get("_source") == "product_master"]
        relevant = 0
        for c in candidates:
            reasons = []
            score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
            if app._is_relevant_scored_candidate(score, reasons, tag):
                relevant += 1
        sufficient = relevant >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        if sufficient:
            sufficient_count += 1
        print(f"  {category} x {tag}: relevant={relevant} sufficient={sufficient}")
    print(f"\n  sufficient到達数: {sufficient_count}/8")


if __name__ == "__main__":
    main()
