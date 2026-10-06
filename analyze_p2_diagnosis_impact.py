"""P2 Step2: 診断影響度検証(読み取り専用、実API呼び出しなし)。
既存のselect_best_market_candidate()をそのまま使い、app.search_rakuten_for_step
のみをモック(2シナリオ: 楽天完全失敗/楽天はヒットするが対象成分に無関係)して、
P2上位10領域の各組み合わせで実際に何が選ばれるかを確認する。
新規ヒューリスティックの追加・DB変更・Gemini/楽天実API呼び出しは行わない。
"""
from unittest.mock import patch

import app

BUDGET_VALUE = 3000

# (category, 狙うtag, purpose, step用ingredient_focus生テキスト, user_data)
COMBOS = [
    ("美容液", "hyaluronic_acid", "乾燥・潤い不足が気になる", "ヒアルロン酸",
     {"skin_type": "dry", "oil": "dry", "sens": "normal"}),
    ("美容液", "ceramide", "乾燥・バリア機能の低下が気になる", "セラミド",
     {"skin_type": "dry", "oil": "dry", "sens": "high"}),
    ("化粧水", "tranexamic_acid", "くすみ・色素沈着が気になる", "トラネキサム酸",
     {"skin_type": "normal", "oil": "normal", "sens": "normal"}),
    ("化粧水", "amino_acid", "乾燥・バリア機能の低下が気になる(低刺激)", "アミノ酸",
     {"skin_type": "sensitive", "oil": "normal", "sens": "high"}),
    ("パック", "niacinamide", "毛穴・皮脂・くすみが気になる", "ナイアシンアミド",
     {"skin_type": "oily", "oil": "oily", "sens": "normal"}),
    ("クリーム", "peptide", "ハリ不足・エイジングが気になる", "ペプチド",
     {"skin_type": "normal", "oil": "normal", "sens": "normal"}),
    ("クリーム", "collagen", "ハリ不足・エイジングが気になる", "コラーゲン",
     {"skin_type": "normal", "oil": "normal", "sens": "normal"}),
    ("洗顔", "ceramide", "乾燥・バリア機能の低下が気になる", "セラミド",
     {"skin_type": "dry", "oil": "dry", "sens": "high"}),
    ("洗顔", "glycolic_acid", "角質・肌のざらつきが気になる", "グリコール酸",
     {"skin_type": "normal", "oil": "normal", "sens": "normal"}),
    ("クレンジング", "centella_extract", "敏感肌で刺激・赤みが気になる", "シカ",
     {"skin_type": "sensitive", "oil": "normal", "sens": "high"}),
]

GENERIC_LIVE_ITEM = {
    "brand": "テスト楽天ブランド", "name": "テスト楽天汎用商品", "category": None,
    "price": 1800, "price_ref": 1800, "active_ingredients": [], "support_ingredients": [],
    "signature_ingredients": [], "concerns": [], "skin_types": [], "sensitive_ok": "unknown",
    "retinol_level": 0, "main_functions": [], "ingredient_focus": [], "ingredient_strength": {},
    "formulation": [], "technology": [], "texture": "", "contraindications": [],
    "availability_japan": ["rakuten"], "image": "https://example.com/x.jpg", "rakuten_link": "https://example.com",
    "item_code": "shop:generic1", "_source": "rakuten_live",
}


def run_scenario(category, tag, purpose, ingredient_focus_text, user_data, rakuten_return):
    step = {"category": category, "purpose": purpose, "ingredient_focus": ingredient_focus_text}
    live_item = dict(GENERIC_LIVE_ITEM)
    live_item["category"] = category
    mocked_return = [] if rakuten_return == "empty" else [live_item]

    with patch.object(app, "search_rakuten_for_step", return_value=mocked_return):
        result = app.select_best_market_candidate(
            step, db_products=[], user_data=user_data, budget_value=BUDGET_VALUE,
            verified_products=[],
        )

    if result is None:
        return {"outcome": "no_candidate_fallback", "source": None, "score": None, "tag_matched": False}

    source = result.get("_source", "unknown")
    reasons = []
    ingredient_tag = app.normalize_ingredient_tag(ingredient_focus_text)
    score = app.score_product(result, step, user_data, BUDGET_VALUE, reasons=reasons)
    tag_matched = app._is_relevant_scored_candidate(score, reasons, ingredient_tag)
    return {
        "outcome": "selected", "source": source, "score": score, "tag_matched": tag_matched,
        "name": f"{result.get('brand','')}/{result.get('name','')}",
    }


def main():
    for category, tag, purpose, ingredient_focus_text, user_data in COMBOS:
        step_tag = app.normalize_ingredient_tag(ingredient_focus_text)
        candidates = [c for c in app.query_product_master_candidates(category, limit=50) if c.get("_source") == "product_master"]
        relevant_count = 0
        for c in candidates:
            reasons = []
            s = app.score_product(c, {"category": category, "purpose": purpose, "ingredient_focus": ingredient_focus_text},
                                   user_data, BUDGET_VALUE, reasons=reasons)
            if app._is_relevant_scored_candidate(s, reasons, step_tag):
                relevant_count += 1

        print(f"\n### {category} x {tag}  (ingredient_focus_text={ingredient_focus_text!r} -> normalized={step_tag!r})")
        print(f"  現在の52商品での関連候補数: {relevant_count}")

        r_empty = run_scenario(category, tag, purpose, ingredient_focus_text, user_data, "empty")
        print(f"  [シナリオA: 楽天検索が完全に0件] -> outcome={r_empty['outcome']} "
              f"source={r_empty.get('source')} tag_matched={r_empty.get('tag_matched')} name={r_empty.get('name')}")

        r_generic = run_scenario(category, tag, purpose, ingredient_focus_text, user_data, "generic")
        print(f"  [シナリオB: 楽天検索は1件返すが対象成分に無関係] -> outcome={r_generic['outcome']} "
              f"source={r_generic.get('source')} tag_matched={r_generic.get('tag_matched')} name={r_generic.get('name')}")


if __name__ == "__main__":
    main()
