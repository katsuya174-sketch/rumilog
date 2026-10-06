"""P2 Step4: ingredient_focus優先補正の設計シミュレーション(読み取り専用、
コード修正なし、DB変更なし、実API呼び出しなし)。

既存のscore_product/score_improvement/score_routine_balance/
get_dynamic_score_weights/get_routine_score_weight/enrich_product_metadata_
from_ingredientsをそのまま再利用し、select_best_market_candidate()と同じ
最終スコア計算を再現した上で、tag_holder(ingredient_tagを実際に保持する
候補)に対する追加ボーナス/非保持への減点を仮想的に加えた場合の勝者の変化を
比較する。app.py自体は一切変更しない。
"""
import copy

import app

BUDGET_VALUE = 3000

# Step3で使用した18組み合わせ(同一)
COMBOS = [
    ("美容液", "hyaluronic_acid", "乾燥・潤い不足が気になる", "ヒアルロン酸", {"skin_type": "dry", "oil": "dry", "sens": "normal"}, "乾燥・潤い不足が気になる。保湿を重視したい。"),
    ("美容液", "ceramide", "乾燥・バリア機能の低下が気になる", "セラミド", {"skin_type": "dry", "oil": "dry", "sens": "high"}, "乾燥・バリア機能の低下が気になる。バリア強化を重視したい。"),
    ("化粧水", "tranexamic_acid", "くすみ・色素沈着が気になる", "トラネキサム酸", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "くすみ・色素沈着が気になる。美白ケアを重視したい。"),
    ("化粧水", "amino_acid", "乾燥・バリア機能の低下が気になる(低刺激)", "アミノ酸", {"skin_type": "sensitive", "oil": "normal", "sens": "high"}, "乾燥・バリア機能の低下が気になる。低刺激な保湿を重視したい。"),
    ("パック", "niacinamide", "毛穴・皮脂・くすみが気になる", "ナイアシンアミド", {"skin_type": "oily", "oil": "oily", "sens": "normal"}, "毛穴・皮脂・くすみが気になる。毛穴改善を重視したい。"),
    ("クリーム", "peptide", "ハリ不足・エイジングが気になる", "ペプチド", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "ハリ不足・エイジングが気になる。ハリを重視したい。"),
    ("クリーム", "collagen", "ハリ不足・エイジングが気になる", "コラーゲン", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "ハリ不足・エイジングが気になる。ハリを重視したい。"),
    ("洗顔", "ceramide", "乾燥・バリア機能の低下が気になる", "セラミド", {"skin_type": "dry", "oil": "dry", "sens": "high"}, "乾燥・バリア機能の低下が気になる。バリア強化を重視したい。"),
    ("洗顔", "glycolic_acid", "角質・肌のざらつきが気になる", "グリコール酸", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "角質・肌のざらつきが気になる。角質ケアを重視したい。"),
    ("クレンジング", "centella_extract", "敏感肌で刺激・赤みが気になる", "ツボクサ", {"skin_type": "sensitive", "oil": "normal", "sens": "high"}, "敏感肌で刺激・赤みが気になる。鎮静を重視したい。"),
    ("化粧水", "azelaic_acid", "ニキビ跡・赤みが気になる", "アゼライン酸", {"skin_type": "normal", "oil": "oily", "sens": "normal"}, "ニキビ跡・赤みが気になる。赤み鎮静を重視したい。"),
    ("洗顔", "collagen", "ハリ不足が気になる(洗顔)", "コラーゲン", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "ハリ不足が気になる。ハリを重視したい。"),
    ("洗顔", "glutathione", "くすみ・美白が気になる(洗顔)", "グルタチオン", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "くすみ・美白が気になる。美白ケアを重視したい。"),
    ("美容液", "arbutin", "くすみ・美白が気になる", "アルブチン", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "くすみ・美白が気になる。美白ケアを重視したい。"),
    ("美容液", "glutathione", "くすみ・美白が気になる", "グルタチオン", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "くすみ・美白が気になる。美白ケアを重視したい。"),
    ("美容液", "tranexamic_acid", "くすみ・色素沈着が気になる", "トラネキサム酸", {"skin_type": "normal", "oil": "normal", "sens": "normal"}, "くすみ・色素沈着が気になる。美白ケアを重視したい。"),
    ("ピーリング", "hyaluronic_acid", "乾燥しがちだが角質ケアもしたい", "ヒアルロン酸", {"skin_type": "dry", "oil": "normal", "sens": "normal"}, "乾燥しがちだが角質ケアもしたい。保湿も重視したい。"),
    ("乳液", "ceramide", "乾燥・バリア機能の低下が気になる", "セラミド", {"skin_type": "dry", "oil": "dry", "sens": "normal"}, "乾燥・バリア機能の低下が気になる。バリア強化を重視したい。"),
]


def score_all_candidates(category, tag, purpose, ingredient_focus_text, user_data, plan_summary):
    """select_best_market_candidate()と同じ最終スコアを候補全件について再現する。"""
    step = {"category": category, "purpose": purpose, "ingredient_focus": ingredient_focus_text}
    improvement_plan = {"summary": plan_summary}
    ingredient_tag = app.normalize_ingredient_tag(ingredient_focus_text)

    raw_candidates = [c for c in app.query_product_master_candidates(category, limit=50) if c.get("_source") == "product_master"]
    base_weight, improve_weight = app.get_dynamic_score_weights(step, user_data)
    routine_weight = app.get_routine_score_weight(step)

    results = []
    for c in raw_candidates:
        product = copy.deepcopy(c)
        app.enrich_product_metadata_from_ingredients(product)

        base_reasons = []
        base_score = app.score_product(product, step, user_data, BUDGET_VALUE, reasons=base_reasons)
        if base_score <= -9000:
            continue

        improve_score = app.score_improvement(product, improvement_plan, None)
        routine_reasons = []
        routine_score = app.score_routine_balance(step, product, None, reasons=routine_reasons)

        final_score = base_score * base_weight + improve_score * improve_weight + routine_score * routine_weight
        tag_holder = bool(ingredient_tag) and ingredient_tag in (product.get("active_ingredients") or [])

        results.append({
            "brand": product.get("brand"), "name": product.get("name"),
            "final_score": final_score, "tag_holder": tag_holder,
        })

    return results, ingredient_tag


def pick_winner(results, bonus, penalty):
    """tag_holderにbonus加点、非保持(タグ指定時のみ)にpenalty減点した場合の勝者。"""
    best = None
    for r in results:
        adjusted = r["final_score"] + (bonus if r["tag_holder"] else -penalty)
        if best is None or adjusted > best[0]:
            best = (adjusted, r)
    return best[1], best[0]


def main():
    print("=== 各組み合わせのベースライン(補正なし)勝者 ===")
    all_results = {}
    for combo in COMBOS:
        category, tag = combo[0], combo[1]
        results, ingredient_tag = score_all_candidates(*combo)
        all_results[(category, tag)] = (results, ingredient_tag)
        baseline_winner, _ = pick_winner(results, 0, 0)
        print(f"{category}x{tag}: baseline_winner={baseline_winner['brand']}/{baseline_winner['name']} "
              f"tag_holder={baseline_winner['tag_holder']} score={baseline_winner['final_score']:.1f}")

    print("\n\n=== 補正値シミュレーション ===")
    schemes = [
        ("bonus_only", 15, 0), ("bonus_only", 20, 0), ("bonus_only", 25, 0), ("bonus_only", 30, 0), ("bonus_only", 40, 0),
        ("penalty_only", 0, 15), ("penalty_only", 0, 20), ("penalty_only", 0, 25), ("penalty_only", 0, 30),
        ("combined", 10, 10), ("combined", 15, 15), ("combined", 20, 20), ("combined", 10, 20), ("combined", 15, 10),
    ]

    for scheme_name, bonus, penalty in schemes:
        changed = []
        known_fixed = {"化粧水x tranexamic_acid": False, "洗顔x collagen": False}
        for (category, tag), (results, ingredient_tag) in all_results.items():
            baseline_winner, baseline_score = pick_winner(results, 0, 0)
            new_winner, new_score = pick_winner(results, bonus, penalty)
            if new_winner["name"] != baseline_winner["name"]:
                changed.append((category, tag, baseline_winner, new_winner, new_score - baseline_score))
            key = f"{category}x {tag}"
            if key in known_fixed:
                known_fixed[key] = new_winner["tag_holder"]

        print(f"\n--- scheme={scheme_name} bonus=+{bonus} penalty=-{penalty} ---")
        print(f"  既知2件の解消: 化粧水xtranexamic_acid={known_fixed.get('化粧水x tranexamic_acid')}, "
              f"洗顔xcollagen={known_fixed.get('洗顔x collagen')}")
        print(f"  18件中の順位変動数: {len(changed)}")
        for category, tag, old, new, diff in changed:
            print(f"    {category}x{tag}: {old['brand']}/{old['name']} -> {new['brand']}/{new['name']} (score差 {diff:+.1f})")


if __name__ == "__main__":
    main()
