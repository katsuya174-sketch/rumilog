"""
採点根拠トレース実装(improve → base → routine → why_best統合)のテスト。

絶対条件: ランキング式・加点/減点値・重み・条件分岐・候補順位は変更しない。
reasons=None(またはreasons未使用)時に、既存のbase_score/improve_score/
routine_score/final scoreが完全に一致することを全段階で確認する。

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


# =========================================================
# STEP 1: improve軸
# =========================================================

class ImprovementReasonDetailsTests(unittest.TestCase):
    def test_build_improvement_reason_matches_joined_details_labels(self):
        """既存のbuild_improvement_reason()の戻り値(文字列)が、
        build_improvement_reason_details()のlabelを連結したものと
        完全に一致すること(二重実装になっていないことの確認)。"""
        products = [
            {"name": "ニキビケア美容液", "category": "美容液",
             "active_ingredients": ["salicylic_acid"], "sensitive_ok": "yes"},
            {"name": "UVミルク", "category": "日焼け止め", "sensitive_ok": "no"},
            {"name": "酵素洗顔パウダー", "category": "ピーリング"},
            {"name": "プレーン乳液", "category": "乳液"},
            {"name": "無地商品", "category": "不明カテゴリ"},
        ]
        for product in products:
            with self.subTest(product=product["name"]):
                details = app.build_improvement_reason_details(product, {})
                joined = app._join_reason_labels(details)
                self.assertEqual(app.build_improvement_reason(product, {}), joined)

    def test_details_contain_traceable_points_for_category_and_sensitivity_rules(self):
        sunscreen = {"name": "UVミルク", "category": "日焼け止め"}
        details = app.build_improvement_reason_details(sunscreen, {})
        sunscreen_entry = next(d for d in details if d["rule"] == "improvement_category_sunscreen")
        self.assertEqual(sunscreen_entry["points"], 16)
        self.assertEqual(sunscreen_entry["axis"], "improve")

        peeling = {"name": "ピール", "category": "ピーリング"}
        details = app.build_improvement_reason_details(peeling, {})
        peeling_entry = next(d for d in details if d["rule"] == "improvement_category_peeling")
        self.assertEqual(peeling_entry["points"], 15)

        sensitive = {"name": "低刺激乳液", "category": "乳液", "sensitive_ok": "yes"}
        details = app.build_improvement_reason_details(sensitive, {})
        sens_entry = next(d for d in details if d["rule"] == "improvement_sensitive_friendly")
        self.assertEqual(sens_entry["points"], 8)

    def test_details_do_not_fabricate_points_for_unattributable_category_text(self):
        """score_improvement()のCATEGORY_IMPROVEMENT_BONUSは全カテゴリ共通の
        別加点であり、build_improvement_reason_detailsの洗顔/乳液/パック文言と
        1対1対応しないため、pointsをNoneのままにすること(存在しない対応を
        捏造しない)。"""
        cleanser = {"name": "洗顔フォーム", "category": "洗顔"}
        details = app.build_improvement_reason_details(cleanser, {})
        entry = next(d for d in details if d["rule"] == "improvement_category_cleansing")
        self.assertIsNone(entry["points"])

    def test_matched_product_feature_is_an_actual_term_from_the_product(self):
        product = {
            "name": "サリチル酸ジェル",
            "category": "美容液",
            "active_ingredients": ["salicylic_acid"],
        }
        # improvement_plan未指定でもtargetsが空になり得るため、明示的にtargetを誘発する
        improvement_plan = {"priority_concerns": ["acne"]}
        details = app.build_improvement_reason_details(product, improvement_plan)
        target_entries = [d for d in details if d["rule"].startswith("improvement_target_")]
        for entry in target_entries:
            self.assertTrue(entry["matched_product_feature"])
            self.assertIn(entry["matched_product_feature"], app.collect_product_terms(product))


class NormalizeCandidateRetainsScoreReasonsTests(unittest.TestCase):
    """normalize_candidate()(finalize_step_data内)がcandidate_score_reasons
    (improve軸)を落とさずtop_candidatesまで保持することの確認。"""

    def test_finalize_step_data_preserves_improvement_reason_details(self):
        step = {
            "category": "美容液",
            "purpose": "",
            "product": "テスト美容液",
            "brand": "テストブランド",
            "top_candidates": [
                {
                    "brand": "テストブランド",
                    "name": "テスト美容液",
                    "score": 90,
                    "base_score": 90,
                    "improve_score": 20,
                    "routine_score": 0,
                    "price_ref": 2500,
                    "source": "db",
                    "_improvement_reason_details": [
                        {
                            "axis": "improve",
                            "rule": "improvement_category_sunscreen",
                            "label": "紫外線対策で赤み・色素沈着の悪化を防ぐ",
                            "matched_product_feature": "日焼け止め",
                            "matched_user_condition": "",
                            "points": 16,
                        }
                    ],
                },
            ],
        }
        result_step = app.finalize_step_data(dict(step), {"oil": "oily", "concerns": []})
        top = result_step.get("top_candidates", [])
        self.assertTrue(top)
        self.assertEqual(top[0].get("candidate_score_reasons"), step["top_candidates"][0]["_improvement_reason_details"])


# =========================================================
# STEP 2: base軸 (score_product / score_goal_fit /
# score_signature_ingredients / apply_common_score_rules /
# apply_cleansing_score_rules / apply_sunscreen_score_rules)
# =========================================================

_BASE_REPRESENTATIVE_PRODUCTS = [
    (
        {
            "name": "ナイアシンアミド美容液", "brand": "テスト", "category": "美容液",
            "active_ingredients": ["niacinamide"], "support_ingredients": ["ceramide"],
            "concerns": ["pores"], "skin_types": ["oily"], "sensitive_ok": "yes",
            "main_functions": ["毛穴ケア"], "ingredient_focus": ["niacinamide"],
            "formulation": ["barrier_formula"], "technology": [], "texture": "gel",
            "contraindications": [], "signature_ingredients": [], "retinol_level": 0,
            "price_ref": 2500, "availability_japan": ["amazon"],
        },
        {"category": "美容液", "purpose": "毛穴ケア", "ingredient_focus": "niacinamide"},
        {"oil": "oily", "sens": "middle", "exp": "middle"}, 3000, 137,
    ),
    (
        {
            "name": "クリームA", "brand": "テスト", "category": "クリーム",
            "active_ingredients": [], "support_ingredients": [], "concerns": ["dryness"],
            "skin_types": ["dry"], "sensitive_ok": "unknown", "main_functions": [],
            "ingredient_focus": [], "formulation": [], "technology": [], "texture": "rich",
            "contraindications": [], "signature_ingredients": [], "retinol_level": 0,
            "price_ref": 1800, "availability_japan": [],
        },
        {"category": "クリーム", "purpose": "保湿", "ingredient_focus": ""},
        {"oil": "dry", "sens": "high", "exp": "middle"}, 2000, 92,
    ),
    (
        {
            "name": "ピーリングジェル", "brand": "テスト", "category": "ピーリング",
            "active_ingredients": ["aha"], "support_ingredients": [], "concerns": ["pores"],
            "skin_types": [], "sensitive_ok": "no", "main_functions": [], "ingredient_focus": [],
            "formulation": [], "technology": [], "texture": "gel", "contraindications": [],
            "signature_ingredients": [], "retinol_level": 0, "price_ref": 2200,
            "availability_japan": ["rakuten"],
        },
        {"category": "ピーリング", "purpose": "角質ケア", "ingredient_focus": "aha"},
        {"oil": "oily", "sens": "low", "exp": "high"}, 3000, 104,
    ),
    (
        {
            "name": "洗顔フォームB", "brand": "テスト", "category": "洗顔",
            "active_ingredients": [], "support_ingredients": ["cica"], "concerns": ["acne"],
            "skin_types": [], "sensitive_ok": "yes",
            "main_functions": ["makeup_removal", "pore_preventive"], "ingredient_focus": [],
            "formulation": ["low_irritation"], "technology": [], "texture": "foam",
            "contraindications": [], "signature_ingredients": [], "retinol_level": 0,
            "price_ref": 1200, "availability_japan": ["amazon", "rakuten"],
        },
        {"category": "洗顔", "purpose": "ニキビケア", "ingredient_focus": ""},
        {"oil": "oily", "sens": "high", "exp": "beginner", "makeup_level": "medium", "morning_cleanse": "yes"}, 1500, 151,
    ),
    (
        {
            "name": "UVミルクC", "brand": "テスト", "category": "日焼け止め",
            "active_ingredients": ["uv_filter"], "support_ingredients": [], "concerns": [],
            "skin_types": [], "sensitive_ok": "unknown", "main_functions": ["紫外線防御"],
            "ingredient_focus": [], "formulation": ["waterproof"], "technology": [],
            "texture": "light", "contraindications": [], "signature_ingredients": [],
            "retinol_level": 0, "price_ref": 1600, "availability_japan": ["amazon"],
            "uv_level": {"spf": 50, "pa": "++++"},
        },
        {"category": "日焼け止め", "purpose": "紫外線対策", "ingredient_focus": ""},
        {"oil": "oily", "sens": "low", "exp": "middle"}, 2000, 91,
    ),
]


class ScoreProductReasonsDoNotChangeScoreTests(unittest.TestCase):
    """
    base軸の絶対条件: reasons有無でbase_score(score_product)が完全一致すること。
    5カテゴリ(美容液/クリーム/ピーリング/洗顔/日焼け止め)の代表的候補セットで
    固定した期待値と照合し、リグレッションを検知できるようにする。
    """

    def test_score_product_matches_pinned_expected_scores_with_and_without_reasons(self):
        for product, step, user_data, budget, expected_score in _BASE_REPRESENTATIVE_PRODUCTS:
            with self.subTest(name=product["name"]):
                s1 = app.score_product(product, step, user_data, budget)
                reasons = []
                s2 = app.score_product(product, step, user_data, budget, reasons=reasons)
                self.assertEqual(s1, expected_score, f"score changed for {product['name']!r}")
                self.assertEqual(s1, s2, "reasons=[] must not change score_product's score")
                self.assertTrue(reasons, f"no reasons recorded for {product['name']!r}")
                for r in reasons:
                    self.assertEqual(r["axis"], "base")
                    self.assertIn("rule", r)
                    self.assertIn("label", r)
                    self.assertIn("points", r)

    def test_sub_functions_scores_unchanged_with_reasons(self):
        product = _BASE_REPRESENTATIVE_PRODUCTS[0][0]
        step = _BASE_REPRESENTATIVE_PRODUCTS[0][1]
        user_data = _BASE_REPRESENTATIVE_PRODUCTS[0][2]
        budget = _BASE_REPRESENTATIVE_PRODUCTS[0][3]
        concern_tags = app.purpose_to_concern_tags(step["purpose"])
        ingredient_tag = app.normalize_ingredient_tag(step["ingredient_focus"])

        self.assertEqual(
            app.score_goal_fit(product, step),
            app.score_goal_fit(product, step, reasons=[]),
        )
        self.assertEqual(
            app.score_signature_ingredients(product, step),
            app.score_signature_ingredients(product, step, reasons=[]),
        )
        self.assertEqual(
            app.apply_common_score_rules(product, step, user_data, budget, concern_tags, ingredient_tag),
            app.apply_common_score_rules(product, step, user_data, budget, concern_tags, ingredient_tag, reasons=[]),
        )
        cleansing_product = _BASE_REPRESENTATIVE_PRODUCTS[3][0]
        cleansing_user_data = _BASE_REPRESENTATIVE_PRODUCTS[3][2]
        self.assertEqual(
            app.apply_cleansing_score_rules(cleansing_product, cleansing_user_data, ["acne"]),
            app.apply_cleansing_score_rules(cleansing_product, cleansing_user_data, ["acne"], reasons=[]),
        )
        sunscreen_product = _BASE_REPRESENTATIVE_PRODUCTS[4][0]
        sunscreen_step = _BASE_REPRESENTATIVE_PRODUCTS[4][1]
        sunscreen_user_data = _BASE_REPRESENTATIVE_PRODUCTS[4][2]
        self.assertEqual(
            app.apply_sunscreen_score_rules(sunscreen_product, sunscreen_step, sunscreen_user_data, []),
            app.apply_sunscreen_score_rules(sunscreen_product, sunscreen_step, sunscreen_user_data, [], reasons=[]),
        )

    def test_penalty_is_recorded_with_negative_points(self):
        product = {
            "name": "刺激注意乳液", "category": "乳液", "active_ingredients": [],
            "support_ingredients": [], "concerns": [], "skin_types": [], "sensitive_ok": "no",
            "main_functions": [], "ingredient_focus": [], "formulation": [], "technology": [],
            "texture": "", "contraindications": ["sensitive_skin"], "signature_ingredients": [],
            "retinol_level": 0, "price_ref": 0, "availability_japan": [],
        }
        step = {"category": "乳液", "purpose": "", "ingredient_focus": "niacinamide"}
        user_data = {"oil": "normal", "sens": "high", "exp": "middle"}
        reasons = []
        app.score_product(product, step, user_data, 0, reasons=reasons)
        penalty_rules = [r for r in reasons if r["points"] is not None and r["points"] < 0]
        self.assertTrue(penalty_rules, "expected at least one penalty to be recorded")
        self.assertTrue(any(r["rule"] == "ingredient_focus_missing_penalty" for r in penalty_rules))


class SelectBestMarketCandidateRankingStabilityTests(unittest.TestCase):
    """
    reasons収集を有効化しても、select_best_market_candidate()の1位・
    候補順位・スコアが変化しないことの統合テスト(楽天APIはモックして
    ネットワーク非依存にする)。
    """

    def setUp(self):
        self._orig_search = app.search_rakuten_for_step
        app.search_rakuten_for_step = lambda step, improvement_plan: []

    def tearDown(self):
        app.search_rakuten_for_step = self._orig_search

    def test_winner_and_score_are_deterministic_and_reasons_are_attached(self):
        step = {
            "category": "美容液",
            "purpose": "毛穴ケア",
            "ingredient_focus": "niacinamide",
            "product_candidates": [
                {"brand": "A社", "name": "ナイアシンアミド美容液プロ"},
                {"brand": "B社", "name": "ヒアルロン酸美容液"},
                {"brand": "C社", "name": "毛穴集中ケア美容液"},
            ],
        }
        user_data = {"oil": "oily", "sens": "middle", "exp": "middle"}

        result1 = app.select_best_market_candidate(
            step, db_products=[], user_data=user_data, budget_value=3000, verified_products=[],
        )
        result2 = app.select_best_market_candidate(
            step, db_products=[], user_data=user_data, budget_value=3000, verified_products=[],
        )
        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)
        self.assertEqual(result1.get("name"), result2.get("name"))
        self.assertEqual(result1.get("_score"), result2.get("_score"))
        self.assertEqual(result1.get("_base_score"), result2.get("_base_score"))
        self.assertEqual(
            [c["name"] for c in result1.get("_top_candidates", [])],
            [c["name"] for c in result2.get("_top_candidates", [])],
        )

        # 1位のcandidate_score_reasonsが実際に記録されていること
        self.assertIn("_base_reasons", result1)
        self.assertTrue(result1["_base_reasons"])


# =========================================================
# STEP 3: routine軸 (score_routine_balance)
# =========================================================

class RoutineBalanceReasonsTests(unittest.TestCase):
    def test_score_unchanged_with_reasons_purpose_and_synergy_case(self):
        product = {"active_ingredients": ["retinol", "niacinamide"], "name": "test"}
        step = {"purpose": "毛穴 ハリ", "ingredient_focus": "niacinamide"}
        routine_context = {
            "families": ["vitamin_c"],
            "global_families": [],
            "avoid_rules": [{"families": ["retinoid", "aha"], "severity": "soft", "scope": "same_session"}],
            "assigned_focus_tags": ["niacinamide"],
            "synergy_rules": [{"families": ["retinoid", "vitamin_c"], "bonus": "high"}],
        }
        s1 = app.score_routine_balance(step, product, routine_context)
        reasons = []
        s2 = app.score_routine_balance(step, product, routine_context, reasons=reasons)
        self.assertEqual(s1, s2)
        self.assertTrue(reasons)
        for r in reasons:
            self.assertEqual(r["axis"], "routine")

    def test_hard_block_score_unchanged_and_returns_minus_9999(self):
        product = {"active_ingredients": ["retinol"], "name": "t2"}
        step = {"purpose": "", "ingredient_focus": ""}
        routine_context = {
            "families": ["aha"],
            "global_families": [],
            "avoid_rules": [{"families": ["retinoid", "aha"], "severity": "hard", "scope": "same_session"}],
        }
        s1 = app.score_routine_balance(step, product, routine_context)
        reasons = []
        s2 = app.score_routine_balance(step, product, routine_context, reasons=reasons)
        self.assertEqual(s1, -9999)
        self.assertEqual(s1, s2)

    def test_no_routine_context_score_unchanged(self):
        product = {"active_ingredients": ["azelaic_acid"], "name": "t3"}
        step = {"purpose": "ニキビ跡", "ingredient_focus": ""}
        s1 = app.score_routine_balance(step, product)
        reasons = []
        s2 = app.score_routine_balance(step, product, reasons=reasons)
        self.assertEqual(s1, s2)

    def test_synergy_and_overlap_penalty_are_recorded_with_correct_points(self):
        product = {"active_ingredients": ["niacinamide", "retinol"], "name": "test"}
        step = {"purpose": "", "ingredient_focus": "niacinamide"}
        routine_context = {
            "families": ["vitamin_c"],
            "global_families": [],
            "avoid_rules": [],
            "assigned_focus_tags": ["retinol"],  # niacinamide以外に retinol が他stepで既に使われている想定
            "synergy_rules": [{"families": ["retinoid", "vitamin_c"], "bonus": "medium"}],
        }
        reasons = []
        app.score_routine_balance(step, product, routine_context, reasons=reasons)
        synergy = next((r for r in reasons if r["rule"] == "routine_synergy_bonus"), None)
        overlap = next((r for r in reasons if r["rule"] == "routine_non_focus_overlap_penalty"), None)
        self.assertIsNotNone(synergy)
        self.assertEqual(synergy["points"], 12)
        self.assertIsNotNone(overlap)
        self.assertEqual(overlap["points"], -15)


class NormalizeCandidateRetainsRoutineReasonsTests(unittest.TestCase):
    def test_finalize_step_data_merges_all_three_axes_into_candidate_score_reasons(self):
        step = {
            "category": "美容液",
            "purpose": "",
            "product": "テスト美容液",
            "brand": "テストブランド",
            "top_candidates": [
                {
                    "brand": "テストブランド", "name": "テスト美容液", "score": 90,
                    "base_score": 90, "improve_score": 20, "routine_score": 10,
                    "price_ref": 2500, "source": "db",
                    "_improvement_reason_details": [
                        {"axis": "improve", "rule": "x", "label": "improve", "matched_product_feature": "",
                         "matched_user_condition": "", "points": 10},
                    ],
                    "_base_reasons": [
                        {"axis": "base", "rule": "y", "label": "base", "matched_product_feature": "",
                         "matched_user_condition": "", "points": 20},
                    ],
                    "_routine_reasons": [
                        {"axis": "routine", "rule": "z", "label": "routine", "matched_product_feature": "",
                         "matched_user_condition": "", "points": 5},
                    ],
                },
            ],
        }
        result_step = app.finalize_step_data(dict(step), {"oil": "oily", "concerns": []})
        top = result_step.get("top_candidates", [])
        self.assertTrue(top)
        reasons = top[0].get("candidate_score_reasons")
        axes = [r["axis"] for r in reasons]
        self.assertEqual(axes, ["improve", "base", "routine"])


# =========================================================
# STEP 4: why_best統合 (candidate_score_reasons比較)
# =========================================================

def _reason(rule, label, feature="", condition="", points=10, axis="base"):
    return {
        "axis": axis, "rule": rule, "label": label,
        "matched_product_feature": feature, "matched_user_condition": condition,
        "points": points,
    }


class WhyBestUsesRealScoreReasonsTests(unittest.TestCase):
    def _candidate(self, name, base_score, reasons):
        return {
            "brand": "", "name": name, "score": base_score, "base_score": base_score,
            "improve_score": 0, "routine_score": 0, "source": "db", "price_ref": 2000,
            "active_ingredients": [], "support_ingredients": [], "main_functions": [],
            "skin_types": [], "concerns": [], "texture": "", "formulation": [],
            "candidate_score_reasons": reasons,
        }

    def test_decisive_reason_reflects_actual_gap_between_candidates(self):
        best = self._candidate("1位商品", 90, [
            _reason("common_concern_match", "今回の悩みタグに一致する", feature="pores", points=8),
            _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている", feature="sensitive_ok=yes", points=6),
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=25),
        ])
        second = self._candidate("2位商品", 70, [
            _reason("common_concern_match", "今回の悩みタグに一致する", feature="pores", points=8),
            _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている", feature="sensitive_ok=yes", points=2),
        ])
        third = self._candidate("3位商品", 60, [
            _reason("common_concern_match", "今回の悩みタグに一致する", feature="pores", points=4),
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=25),
        ])
        result = app.build_candidate_comparison_notes([best, second, third], {}, {})
        why_best = result["why_best"]
        # 毛穴ケア(common_concern_match)は他候補にもあり同点分がある(3位はpoints低いが
        # 存在自体はしている)ため、単独の決め手にはならない。
        # ナイアシンアミドは3位と同点(25=25)のため決め手にならない。
        # 敏感肌向け(6 vs 2)は全候補に対して実際に上回っているため決め手になる。
        self.assertIn("敏感肌向け", why_best)
        self.assertNotIn("ナイアシンアミド", why_best)

    def test_reasons_do_not_leak_between_candidates(self):
        """複数候補をselect_best_market_candidate()で評価しても、
        各候補のcandidate_score_reasonsが他候補のreasonsと混在しないこと。"""
        orig = app.search_rakuten_for_step
        app.search_rakuten_for_step = lambda step, improvement_plan: []
        try:
            step = {
                "category": "美容液", "purpose": "毛穴ケア", "ingredient_focus": "niacinamide",
                "product_candidates": [
                    {"brand": "A社", "name": "ナイアシンアミド美容液"},
                    {"brand": "B社", "name": "ヒアルロン酸美容液"},
                ],
            }
            user_data = {"oil": "oily", "sens": "middle", "exp": "middle"}
            result = app.select_best_market_candidate(
                step, db_products=[], user_data=user_data, budget_value=3000, verified_products=[],
            )
            self.assertIsNotNone(result)
            top_candidates = result.get("_top_candidates", [])
            self.assertGreaterEqual(len(top_candidates), 1)
            # 各候補のreasonsが、他候補のreasonsオブジェクトと同一(混在)でないこと
            reasons_lists = [c.get("_base_reasons") for c in top_candidates if "_base_reasons" in c]
            for i in range(len(reasons_lists)):
                for j in range(i + 1, len(reasons_lists)):
                    self.assertIsNot(reasons_lists[i], reasons_lists[j])
        finally:
            app.search_rakuten_for_step = orig

    def test_shared_reason_is_mentioned_as_context_not_sole_decisive_point(self):
        shared = _reason("common_main_function_purpose_match", "今回の目的に合う機能を持つ", feature="毛穴ケア", points=6)
        best = self._candidate("1位商品", 90, [
            dict(shared),
            _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている", feature="sensitive_ok=yes", points=12),
        ])
        other = self._candidate("2位商品", 80, [dict(shared)])
        result = app.build_candidate_comparison_notes([best, other], {}, {})
        why_best = result["why_best"]
        self.assertIn("は比較した候補にも見られますが", why_best)
        self.assertIn("敏感肌向け", why_best)


if __name__ == "__main__":
    unittest.main()
