"""P2 Step12最終検証(読み取り専用): 件数、identity/item_code重複、
既存52件の不変性、P1 8領域・P2 5領域の実データ再計算。"""
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
    names = []
    for c in candidates:
        reasons = []
        score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if app._is_relevant_scored_candidate(score, reasons, tag):
            names.append(f"{c.get('brand')}/{c.get('name')}")
    return names


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM product_master")
    print(f"1. product_master総件数: {cur.fetchone()[0]} (期待: 66)")

    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id BETWEEN 53 AND 65")
    print(f"   Step7反映13件存在確認: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id = 66")
    print(f"   Step11反映1件存在確認: {cur.fetchone()[0]}")

    cur.execute("SELECT identity_key, COUNT(*) FROM product_master GROUP BY identity_key HAVING COUNT(*) > 1")
    print(f"4a. identity_key重複: {cur.fetchall() or 'なし'}")
    cur.execute("SELECT item_code, COUNT(*) FROM product_master WHERE item_code IS NOT NULL AND item_code != '' GROUP BY item_code HAVING COUNT(*) > 1")
    print(f"4b. item_code重複: {cur.fetchall() or 'なし'}")

    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id<=38 AND item_code IS NOT NULL AND item_code != ''")
    print(f"\n5. 既存38件item_code保有(期待37、不変チェック): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id<=38 AND jan_code IS NOT NULL")
    print(f"   既存38件jan_code保有(期待32、不変チェック): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM product_master WHERE product_id BETWEEN 39 AND 52")
    print(f"   既存P1反映14件(39-52)存在確認: {cur.fetchone()[0]}")
    conn.close()

    print("\n2. P1 8領域 実データ再計算:")
    p1_sufficient = 0
    for category, tag in P1_COMBOS_8:
        names = relevant_count(category, tag)
        sufficient = len(names) >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        if sufficient:
            p1_sufficient += 1
        print(f"   {category} x {tag}: relevant={len(names)} sufficient={sufficient}")
    print(f"   P1 sufficient到達数: {p1_sufficient}/8")

    print("\n3. P2 5領域 実データ再計算:")
    p2_sufficient = 0
    for category, tag in P2_AREAS_5:
        names = relevant_count(category, tag)
        sufficient = len(names) >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        if sufficient:
            p2_sufficient += 1
        print(f"   {category} x {tag}: relevant={len(names)} {names} sufficient={sufficient}")
    print(f"   P2 sufficient到達数: {p2_sufficient}/5")


if __name__ == "__main__":
    main()
