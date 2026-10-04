"""P1 Round2反映後の検証(読み取り専用): 件数、existing45件の不変性、
P1 8領域の実データ再計算、salicylic_acidの誤付与がないことの確認。"""
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
    total = cur.fetchone()[0]
    print(f"product_master総件数: {total} (期待: 50)")

    cur.execute("SELECT identity_key, COUNT(*) FROM product_master GROUP BY identity_key HAVING COUNT(*) > 1")
    print(f"identity_key重複: {cur.fetchall() or 'なし'}")

    cur.execute("SELECT brand, name, active_ingredients FROM product_master WHERE product_id = 50")
    row = cur.fetchone()
    print(f"\nproduct_id=50(ルナメアAC)の実データ: brand={row[0]} name={row[1]} active_ingredients={row[2]}")
    print(f"salicylic_acidが誤って含まれていないか: {'salicylic_acid' not in (row[2] or [])}")

    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id<=38 AND item_code IS NOT NULL AND item_code != ''")
    print(f"\n既存38件のitem_code保有(不変チェック、期待37): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id<=38 AND jan_code IS NOT NULL")
    print(f"既存38件のjan_code保有(不変チェック、期待32): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id BETWEEN 39 AND 45")
    print(f"Round1反映の7件存在確認(期待7): {cur.fetchone()[0]}")
    conn.close()

    print("\nP1 8領域 実データ再計算:")
    results = []
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
        results.append((category, tag, relevant, sufficient))
        print(f"  {category} x {tag}: relevant={relevant} sufficient={sufficient}")

    sufficient_count = sum(1 for _, _, _, s in results if s)
    print(f"\nsufficient到達数: {sufficient_count}/8")


if __name__ == "__main__":
    main()
