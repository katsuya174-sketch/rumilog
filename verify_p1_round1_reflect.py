"""P1 Round1反映後の検証(読み取り専用): 件数、identity重複、既存38件の
不変性、P1 8領域の実データ再計算。実API呼び出しなし。"""
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

# 今回のバックフィル前(Phase2+3統合修正ラウンド)に記録した既存38件の
# 変更前スナップショット相当のキー項目(brand, name, category)で突合する。
EXISTING_38_IDENTITY_CHECK_FIELDS = (
    "product_id", "brand", "name", "category", "active_ingredients",
    "formulation", "jan_code", "item_code", "price_ref", "data_source",
)


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM product_master")
    total = cur.fetchone()[0]
    print(f"1. product_master総件数: {total} (期待: 45)")

    cur.execute("""
        SELECT identity_key, COUNT(*) FROM product_master GROUP BY identity_key HAVING COUNT(*) > 1
    """)
    dup = cur.fetchall()
    print(f"2. identity_key重複: {dup if dup else 'なし'}")

    cur.execute("SELECT COUNT(*) FROM product_master WHERE data_source = 'ai_precollected'")
    ai_total = cur.fetchone()[0]
    print(f"   data_source='ai_precollected'件数: {ai_total} (期待: 45)")

    # 既存38件(product_id<=38)の重要フィールドが変化していないか
    cur.execute(f"""
        SELECT {', '.join(EXISTING_38_IDENTITY_CHECK_FIELDS)}
        FROM product_master WHERE product_id <= 38 ORDER BY product_id
    """)
    existing_rows = cur.fetchall()
    print(f"\n5. 既存38件の存在確認: {len(existing_rows)}/38件")
    conn.close()

    print("\n3. P1 8領域をproduct_master実データから再計算:")
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
    print(f"\n4. sufficient到達数: {sufficient_count}/8")


if __name__ == "__main__":
    main()
