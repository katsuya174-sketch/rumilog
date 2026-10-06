"""Phase 3 P2優先順位分析: 234組み合わせの不足マトリクスを高速に再計算する
(読み取り専用、1回のDB接続でproduct_master全件を取得し、以降は純粋な
Pythonループでscore_product()を適用する。新規ヒューリスティックは追加しない)。"""
import os
import psycopg2
import app

NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000

CATEGORIES = ["クレンジング", "洗顔", "化粧水", "美容液", "乳液", "クリーム", "パック", "ピーリング", "日焼け止め"]

COMMON_INGREDIENT_TAGS = [
    "retinol", "retinal", "retinoid", "vitamin_c", "niacinamide", "azelaic_acid",
    "tranexamic_acid", "peptide", "ceramide", "hyaluronic_acid", "centella_extract",
    "panthenol", "aha", "bha", "salicylic_acid", "glycolic_acid", "lactic_acid", "pha",
    "arbutin", "kojic_acid", "glutathione", "squalane", "glycerin", "amino_acid",
    "collagen", "enzyme",
]


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(app._PRODUCT_MASTER_ROW_COLUMNS)} FROM product_master")
    rows = cur.fetchall()
    conn.close()

    products = [app._product_master_row_to_product(r) for r in rows]
    by_category = {}
    for p in products:
        by_category.setdefault(p.get("category"), []).append(p)

    print(f"product_master総件数: {len(products)}\n")

    results = []
    for category in CATEGORIES:
        candidates = by_category.get(category, [])
        for tag in COMMON_INGREDIENT_TAGS:
            step = {"category": category, "purpose": "", "ingredient_focus": tag}
            relevant_names = []
            hard_excluded = 0
            for c in candidates:
                reasons = []
                score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
                if score <= -9000:
                    hard_excluded += 1
                    continue
                if app._is_relevant_scored_candidate(score, reasons, tag):
                    relevant_names.append(f"{c.get('brand')}/{c.get('name')}")
            total = len(candidates)
            relevant = len(relevant_names)
            shortfall = max(0, app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT - relevant)
            results.append({
                "category": category, "tag": tag, "total": total, "hard_excluded": hard_excluded,
                "relevant": relevant, "shortfall": shortfall, "names": relevant_names,
            })

    insufficient = [r for r in results if r["shortfall"] > 0]
    sufficient = [r for r in results if r["shortfall"] == 0]
    print(f"全組み合わせ数: {len(results)}  不足あり: {len(insufficient)}  充足: {len(sufficient)}\n")

    print("=== 充足している組み合わせ(sufficient) ===")
    for r in sorted(sufficient, key=lambda x: (x["category"], x["tag"])):
        print(f"  {r['category']} x {r['tag']}: relevant={r['relevant']} {r['names']}")

    print("\n=== 不足(1-2件あり、閾値未満) ===")
    partial = [r for r in insufficient if r["relevant"] > 0]
    for r in sorted(partial, key=lambda x: (-x["relevant"], x["category"], x["tag"])):
        print(f"  {r['category']} x {r['tag']}: relevant={r['relevant']} 不足={r['shortfall']} {r['names']}")

    print(f"\n=== 該当商品0件(最も深刻, {len([r for r in insufficient if r['relevant']==0])}件) ===")
    zero = [r for r in insufficient if r["relevant"] == 0]
    for r in sorted(zero, key=lambda x: (x["category"], x["tag"])):
        print(f"  {r['category']} x {r['tag']}: category一致={r['total']}")


if __name__ == "__main__":
    main()
