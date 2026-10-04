"""Phase 3カバレッジ拡張 Step1: 不足マトリクス作成(読み取り専用)。
実API呼び出し・DB変更・新規ヒューリスティックの追加は一切行わない。
既存のactive_ingredient_tags/score_product()/PRODUCT_MASTER_SUFFICIENT_
CANDIDATE_COUNT(=3)をそのまま使う。"""

import app  # noqa: F401

NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000

CATEGORIES = ["クレンジング", "洗顔", "化粧水", "美容液", "乳液", "クリーム", "パック", "ピーリング", "日焼け止め"]

# 診断ロジック側で「既知の成分」としてconcerns/main_functions等が定義されている
# 統制タグ集合(app.py内のACTIVE_INGREDIENT_PROFILES相当、routine/plan生成で
# 実際に使われる主要タグ)。新規のタグ定義はしない、既存のnormalize_ingredient_tag()
# が返す値のみを使う。
COMMON_INGREDIENT_TAGS = [
    "retinol", "retinal", "retinoid", "vitamin_c", "niacinamide", "azelaic_acid",
    "tranexamic_acid", "peptide", "ceramide", "hyaluronic_acid", "centella_extract",
    "panthenol", "aha", "bha", "salicylic_acid", "glycolic_acid", "lactic_acid", "pha",
    "arbutin", "kojic_acid", "glutathione", "squalane", "glycerin", "amino_acid",
    "collagen", "enzyme",
]

PRIORITY_COMBOS = [
    ("化粧水", "ceramide"),
    ("化粧水", "hyaluronic_acid"),
    ("美容液", "vitamin_c"),
    ("美容液", "peptide"),
    ("洗顔", "salicylic_acid"),
    ("ピーリング", "aha"),
    ("クリーム", "ceramide"),
]


def evaluate_combo(category, ingredient_tag):
    step = {"category": category, "purpose": "", "ingredient_focus": ingredient_tag}
    candidates = app.query_product_master_candidates(category, limit=50)
    candidates = [c for c in candidates if c.get("_source") == "product_master"]

    total = len(candidates)
    hard_excluded = 0
    relevant_names = []
    for c in candidates:
        reasons = []
        score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if score <= -9000:
            hard_excluded += 1
            continue
        if app._is_relevant_scored_candidate(score, reasons, ingredient_tag):
            relevant_names.append(f"{c.get('brand')} / {c.get('name')}")

    effective = total - hard_excluded
    relevant_count = len(relevant_names)
    shortfall = max(0, app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT - relevant_count)
    return {
        "category": category, "ingredient_tag": ingredient_tag,
        "total": total, "hard_excluded": hard_excluded, "effective": effective,
        "relevant_count": relevant_count, "shortfall": shortfall,
        "relevant_names": relevant_names,
    }


def main():
    print("=" * 78)
    print("優先調査対象(7組み合わせ)")
    print("=" * 78)
    priority_results = []
    for category, tag in PRIORITY_COMBOS:
        r = evaluate_combo(category, tag)
        priority_results.append(r)
        print(f"\n### {category} x {tag}")
        print(f"  1. category一致商品数: {r['total']}")
        print(f"  2. hard_exclude後の商品数(有効): {r['effective']}")
        print(f"  3. ingredient active/support match数: {r['relevant_count']}")
        print(f"  4. sufficient(閾値3)までの不足数: {r['shortfall']}")
        print(f"  5. 現在該当している商品:")
        if r["relevant_names"]:
            for n in r["relevant_names"]:
                print(f"     - {n}")
        else:
            print("     (該当なし)")

    print("\n" + "=" * 78)
    print("既存診断ロジックで使われる主要成分タグ x 全カテゴリ の網羅スキャン")
    print("(不足(shortfall>0)のみ表示)")
    print("=" * 78)
    all_results = []
    for category in CATEGORIES:
        for tag in COMMON_INGREDIENT_TAGS:
            r = evaluate_combo(category, tag)
            all_results.append(r)

    insufficient = [r for r in all_results if r["shortfall"] > 0]
    sufficient = [r for r in all_results if r["shortfall"] == 0]
    print(f"\n全組み合わせ数: {len(all_results)}  /  不足あり: {len(insufficient)}  /  充足: {len(sufficient)}")

    # 不足の中でも「現在0件(該当商品なし)」と「1-2件あるが閾値未満」を分けて表示
    zero_candidate = [r for r in insufficient if r["relevant_count"] == 0]
    partial_candidate = [r for r in insufficient if r["relevant_count"] > 0]

    print(f"\n--- 該当商品が0件(最も深刻): {len(zero_candidate)}件 ---")
    for r in sorted(zero_candidate, key=lambda x: (x["category"], x["ingredient_tag"])):
        print(f"  {r['category']} x {r['ingredient_tag']}: category一致={r['total']} relevant=0 不足={r['shortfall']}")

    print(f"\n--- 1-2件あるが閾値未満: {len(partial_candidate)}件 ---")
    for r in sorted(partial_candidate, key=lambda x: (x["category"], x["ingredient_tag"])):
        print(f"  {r['category']} x {r['ingredient_tag']}: relevant={r['relevant_count']} 不足={r['shortfall']} "
              f"({', '.join(r['relevant_names'])})")

    return priority_results, all_results


if __name__ == "__main__":
    main()
