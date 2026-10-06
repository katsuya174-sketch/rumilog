"""P2 Step3横断検査: 「対象成分を実際に持つ商品が、持たない商品に負ける」が
他のingredient_focusでも発生するかを、実際のselect_best_market_candidate()
を通して確認する(読み取り専用、コード修正なし、楽天/Gemini実APIなし)。
improvement_planはpurposeと整合する現実的な内容を与える(空のplanによる
フォールバック既定値{"barrier","dryness"}の混入を避けるため)。
"""
from unittest.mock import patch

import app

BUDGET_VALUE = 3000

# (category, tag, purpose, ingredient_focus生テキスト, user_data, improvement_plan_summary)
# 「1-2件あるが閾値未満」(単一障害点リスクが高い)組み合わせを中心に横断検査
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


def main():
    mismatch_count = 0
    for category, tag, purpose, ingredient_focus_text, user_data, plan_summary in COMBOS:
        step = {"category": category, "purpose": purpose, "ingredient_focus": ingredient_focus_text}
        improvement_plan = {"summary": plan_summary}
        ingredient_tag = app.normalize_ingredient_tag(ingredient_focus_text)

        with patch.object(app, "search_rakuten_for_step", return_value=[]):
            result = app.select_best_market_candidate(
                step, db_products=[], user_data=user_data, budget_value=BUDGET_VALUE,
                verified_products=[], improvement_plan=improvement_plan,
            )

        if result is None:
            print(f"{category}x{tag}: NO_CANDIDATE")
            continue

        winner_tags = result.get("active_ingredients", []) or []
        tag_holder = ingredient_tag in winner_tags if ingredient_tag else None
        mismatch = tag_holder is False
        if mismatch:
            mismatch_count += 1
        print(f"{category}x{tag}: winner={result.get('brand')}/{result.get('name')} "
              f"score={result.get('_score')} base={result.get('_base_score')} "
              f"improve={result.get('_improve_score')} routine={result.get('_routine_score')} "
              f"tag_holder={tag_holder} {'<<< MISMATCH' if mismatch else ''}")

    print(f"\n同型ミスマッチ件数: {mismatch_count}/{len(COMBOS)}")


if __name__ == "__main__":
    main()
