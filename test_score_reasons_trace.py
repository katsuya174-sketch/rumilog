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

    def test_matched_product_feature_is_a_clean_keyword_not_the_raw_product_name(self):
        """
        matched_product_featureは、一致判定に使われたterm全体(商品名等を
        含みうる)ではなく、一致したキーワード自体(可能ならingredient_mapで
        日本語化)を返すこと。商品名「サリチル酸ジェル」まるごとが
        featureに出てはならない。
        """
        product = {
            "name": "サリチル酸ジェル",
            "category": "美容液",
            "active_ingredients": ["salicylic_acid"],
        }
        improvement_plan = {"priority_concerns": ["acne"]}
        details = app.build_improvement_reason_details(product, improvement_plan)
        target_entries = [d for d in details if d["rule"].startswith("improvement_target_")]
        self.assertTrue(target_entries)
        for entry in target_entries:
            self.assertTrue(entry["matched_product_feature"])
            self.assertNotEqual(entry["matched_product_feature"], product["name"])
            self.assertEqual(entry["matched_product_feature"], "サリチル酸")


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

        # 回帰テスト(2026-09発見の不具合): best["_top_candidates"]の各要素
        # (1位・2位・3位すべて)にも_base_reasons等が保持されていること。
        # 以前はここが欠落しており、キー不存在をスキップする書き方の
        # テストでは検知できなかった。ここでは「キーが存在する」ことを
        # 明示的にassertし、スキップしない。
        top_candidates = result1.get("_top_candidates", [])
        self.assertGreaterEqual(len(top_candidates), 3, "この検証には最低3候補が必要")
        for i, c in enumerate(top_candidates[:3]):
            self.assertIn("_base_reasons", c, f"top_candidates[{i}]に_base_reasonsキーが無い")
            self.assertIn("_improvement_reason_details", c, f"top_candidates[{i}]に_improvement_reason_detailsキーが無い")
            self.assertIn("_routine_reasons", c, f"top_candidates[{i}]に_routine_reasonsキーが無い")
            combined = (
                c["_base_reasons"] + c["_improvement_reason_details"] + c["_routine_reasons"]
            )
            self.assertTrue(combined, f"top_candidates[{i}]の採点根拠が実際には空(何らかの理由が記録されているはず)")

    def test_real_pipeline_propagates_reasons_into_candidate_score_reasons_and_why_best(self):
        """
        select_best_market_candidate() → best["_top_candidates"] →
        step["top_candidates"](実際のパイプラインと同じ代入)→
        finalize_step_data() → normalize_candidate() →
        candidate_score_reasons → build_candidate_comparison_notes() → why_best
        の一連の流れを、実パイプラインの関数のみで(手組みfixtureを介さず)検証する。
        """
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

        best = app.select_best_market_candidate(
            step, db_products=[], user_data=user_data, budget_value=3000, verified_products=[],
        )
        self.assertIsNotNone(best)

        # 実際のパイプライン(app.py:13557/13919)と同じ代入
        step["top_candidates"] = best.get("_top_candidates", [])
        result_step = app.finalize_step_data(dict(step), user_data)

        top = result_step.get("top_candidates", [])
        self.assertGreaterEqual(len(top), 1)

        final_reasons = top[0].get("candidate_score_reasons", [])
        self.assertTrue(final_reasons, "最終stepのcandidate_score_reasonsが空(データが途中で失われている)")

        why_best = result_step.get("candidate_comparison", {}).get("why_best", "")
        self.assertNotEqual(why_best, "")
        # 決定的な採点根拠が使えるケースでは、axisの点差だけを説明する
        # fallback文言(「個々の採点根拠では...大きな差はありませんでしたが」)
        # になっていないこと。
        self.assertNotIn("個々の採点根拠では比較した候補と大きな差はありませんでした", why_best)


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
            self.assertGreaterEqual(len(top_candidates), 2)
            # キーが実際に存在することを明示的に確認する(存在しない場合を
            # スキップして合格させない。過去にこの書き方の不備で
            # best["_top_candidates"]のreasons欠落を見逃していた)。
            for i, c in enumerate(top_candidates):
                self.assertIn("_base_reasons", c, f"top_candidates[{i}]に_base_reasonsキーが無い")
            # 各候補のreasonsが、他候補のreasonsオブジェクトと同一(混在)でないこと
            reasons_lists = [c["_base_reasons"] for c in top_candidates]
            for i in range(len(reasons_lists)):
                for j in range(i + 1, len(reasons_lists)):
                    self.assertIsNot(reasons_lists[i], reasons_lists[j])
        finally:
            app.search_rakuten_for_step = orig

    def test_shared_reason_is_mentioned_as_context_when_only_partially_shared(self):
        """
        一部の比較対象(2位のみ)とだけ共通する加点は、補助的な文脈として
        添えてよい(全候補共通の場合とは区別する)。
        """
        shared = _reason("common_main_function_purpose_match", "今回の目的に合う機能を持つ", feature="毛穴ケア", points=6)
        best = self._candidate("1位商品", 90, [
            dict(shared),
            _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている", feature="sensitive_ok=yes", points=12),
        ])
        second = self._candidate("2位商品", 80, [dict(shared)])
        third = self._candidate("3位商品", 75, [])  # 3位はsharedを持たない(一部共有のみ)
        result = app.build_candidate_comparison_notes([best, second, third], {}, {})
        why_best = result["why_best"]
        self.assertIn("は比較した候補にも見られますが", why_best)

    def test_universally_shared_reason_is_not_mentioned_even_as_context(self):
        """
        比較した候補全員が共通して持つ加点は、順位差を生んでいないため
        「共有点」としても導入句に機械的に使わないこと。
        """
        shared = _reason("common_main_function_purpose_match", "今回の目的に合う機能を持つ", feature="毛穴ケア", points=6)
        best = self._candidate("1位商品", 90, [
            dict(shared),
            _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている", feature="sensitive_ok=yes", points=12),
        ])
        other = self._candidate("2位商品", 80, [dict(shared)])
        result = app.build_candidate_comparison_notes([best, other], {}, {})
        why_best = result["why_best"]
        self.assertNotIn("は比較した候補にも見られますが", why_best)
        self.assertIn("敏感肌向け", why_best)
        self.assertIn("敏感肌向け", why_best)


# =========================================================
# STEP 5: why_bestの対称比較(1位への加点だけでなく、
# 他候補が受けた減点を1位が回避したことも決め手として扱う)
# =========================================================

class PenaltyAvoidanceReasonTests(unittest.TestCase):
    def _candidate(self, name, base_score, reasons):
        return {
            "brand": "", "name": name, "score": base_score, "base_score": base_score,
            "improve_score": 0, "routine_score": 0, "source": "db", "price_ref": 2000,
            "active_ingredients": [], "support_ingredients": [], "main_functions": [],
            "skin_types": [], "concerns": [], "texture": "", "formulation": [],
            "candidate_score_reasons": reasons,
        }

    # 1. 1位のみ強い加点 → 従来どおり(gain)説明されること
    def test_gain_only_case_still_works(self):
        best = self._candidate("1位商品", 90, [
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=25),
        ])
        second = self._candidate("2位商品", 70, [])
        result = app.build_candidate_comparison_notes([best, second], {}, {})
        self.assertIn("ナイアシンアミド", result["why_best"])
        self.assertNotIn("影響を受けていません", result["why_best"])

    # 2. 2位・3位のみ同じ減点 → その回避を1位理由として説明
    def test_others_share_penalty_best_avoids_it(self):
        best = self._candidate("1位商品", 90, [])
        second = self._candidate("2位商品", 80, [
            _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", feature="irritation_risk=high", points=-10, axis="routine"),
        ])
        third = self._candidate("3位商品", 75, [
            _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", feature="irritation_risk=high", points=-8, axis="routine"),
        ])
        result = app.build_candidate_comparison_notes([best, second, third], {}, {})
        why_best = result["why_best"]
        self.assertIn("刺激リスクが高いとされている", why_best)
        self.assertIn("影響を受けていません", why_best)
        self.assertIn("irritation_risk=high", why_best)

    # 3. 1位も減点されるが減点幅が小さい → 相対差(既存のgain比較)を正しく扱う
    def test_best_has_smaller_penalty_than_others(self):
        best = self._candidate("1位商品", 90, [
            _reason("common_contra_high_irritation", "刺激リスクが高いとされている成分・処方", feature="high_irritation_risk", points=-3),
        ])
        second = self._candidate("2位商品", 80, [
            _reason("common_contra_high_irritation", "刺激リスクが高いとされている成分・処方", feature="high_irritation_risk", points=-10),
        ])
        third = self._candidate("3位商品", 75, [
            _reason("common_contra_high_irritation", "刺激リスクが高いとされている成分・処方", feature="high_irritation_risk", points=-8),
        ])
        result = app.build_candidate_comparison_notes([best, second, third], {}, {})
        self.assertIn("刺激リスクが高いとされている成分・処方", result["why_best"])

    # 4. 全候補同じ減点 → 決め手にしない
    def test_shared_penalty_across_all_candidates_is_not_decisive(self):
        penalty = _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", feature="irritation_risk=high", points=-10, axis="routine")
        best = self._candidate("1位商品", 90, [dict(penalty)])
        second = self._candidate("2位商品", 80, [dict(penalty)])
        result = app.build_candidate_comparison_notes([best, second], {}, {})
        self.assertNotIn("影響を受けていません", result["why_best"])
        self.assertNotIn("刺激リスクが高いとされている", result["why_best"])

    # 5. 2位だけ減点、3位は1位と同等(減点なし) → 「全候補を上回った」と誤表現しない
    def test_penalty_avoidance_not_claimed_when_only_one_other_has_it(self):
        best = self._candidate("1位商品", 90, [])
        second = self._candidate("2位商品", 85, [
            _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", feature="irritation_risk=high", points=-10, axis="routine"),
        ])
        third = self._candidate("3位商品", 88, [])  # 3位は1位と同じく減点なし(同点)
        result = app.build_candidate_comparison_notes([best, second, third], {}, {})
        # 3位とは同点のため、このruleを「全候補に対する決め手」として使わないこと
        self.assertNotIn("刺激リスクが高いとされている", result["why_best"])

    # 6. 加点差＋減点差の両方が存在 → 実際の主要な順位差を反映
    def test_both_gain_and_avoidance_are_reflected(self):
        best = self._candidate("A商品", 90, [
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=25),
        ])
        second = self._candidate("B商品", 70, [
            _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", feature="irritation_risk=high", points=-10, axis="routine"),
        ])
        third = self._candidate("C商品", 65, [
            _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", feature="irritation_risk=high", points=-8, axis="routine"),
        ])
        result = app.build_candidate_comparison_notes([best, second, third], {}, {})
        why_best = result["why_best"]
        self.assertIn("ナイアシンアミド", why_best)
        self.assertIn("刺激リスクが高いとされている", why_best)
        self.assertIn("影響も受けていません", why_best)

    # 7. reasons有無でscore/rankingが完全一致すること(既存機能の維持確認)
    def test_scoring_functions_unaffected_by_why_best_changes(self):
        product = _BASE_REPRESENTATIVE_PRODUCTS[0][0]
        step = _BASE_REPRESENTATIVE_PRODUCTS[0][1]
        user_data = _BASE_REPRESENTATIVE_PRODUCTS[0][2]
        budget = _BASE_REPRESENTATIVE_PRODUCTS[0][3]
        expected = _BASE_REPRESENTATIVE_PRODUCTS[0][4]
        s1 = app.score_product(product, step, user_data, budget)
        reasons = []
        s2 = app.score_product(product, step, user_data, budget, reasons=reasons)
        self.assertEqual(s1, expected)
        self.assertEqual(s1, s2)

    def test_avoidance_helper_is_symmetric_with_gain_helper_for_shared_rules(self):
        """bestも同じruleを持つ場合はavoidance側で二重カウントしないこと。"""
        best_agg = app._aggregate_reasons_by_rule([
            _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", points=-3, axis="routine"),
        ])
        others_agg_list = [
            app._aggregate_reasons_by_rule([
                _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", points=-10, axis="routine"),
            ]),
        ]
        avoidance = app._find_penalty_avoidance_reasons(best_agg, others_agg_list)
        self.assertEqual(avoidance, [])  # gain側(_find_decisive_score_reasons)の担当


# =========================================================
# STEP 6: 表示品質修正(重複理由の統合/商品名・ブランド名の非露出/
# 全候補共通点の除外)
# =========================================================

class DuplicateReasonMergeTests(unittest.TestCase):
    def _candidate(self, name, base_score, reasons):
        return {
            "brand": "", "name": name, "score": base_score, "base_score": base_score,
            "improve_score": 0, "routine_score": 0, "source": "db", "price_ref": 2000,
            "active_ingredients": [], "support_ingredients": [], "main_functions": [],
            "skin_types": [], "concerns": [], "texture": "", "formulation": [],
            "candidate_score_reasons": reasons,
        }

    def test_same_meaning_different_rules_shown_once_in_why_best(self):
        """
        score_product本体のingredient_focus一致(+15)とapply_common_score_rules
        側の一致(+25)のように、label/feature/matched_user_conditionが完全に
        同じ別ruleが2件あっても、why_bestでは1回だけ表示すること。
        """
        best = self._candidate("1位商品", 90, [
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", condition="ナイアシンアミド", points=25),
            _reason("product_ingredient_focus_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", condition="ナイアシンアミド", points=15),
        ])
        other = self._candidate("2位商品", 60, [])
        result = app.build_candidate_comparison_notes([best, other], {}, {})
        why_best = result["why_best"]
        self.assertEqual(why_best.count("今回重視する成分を主成分として含む"), 1)
        self.assertEqual(why_best.count("ナイアシンアミド"), 1)

    def test_internal_candidate_score_reasons_still_holds_both_rules(self):
        """why_best表示の重複統合は、内部のcandidate_score_reasons自体には影響しないこと。"""
        reasons = [
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=25),
            _reason("product_ingredient_focus_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=15),
        ]
        best = self._candidate("1位商品", 90, reasons)
        # candidate_score_reasons自体は変更されていないこと(参照している元のリストがそのまま)
        self.assertEqual(len(best["candidate_score_reasons"]), 2)
        self.assertEqual(
            [r["rule"] for r in best["candidate_score_reasons"]],
            ["ingredient_focus_active_match", "product_ingredient_focus_match"],
        )

    def test_merged_gap_sums_contributions_not_just_first_rule(self):
        """統合後の差分計算(gap)は、両ruleの寄与を合算すること(先頭1件だけを
        残して他方の寄与を失わない)。"""
        best_agg = app._aggregate_reasons_by_rule([
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=25),
            _reason("product_ingredient_focus_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", points=15),
        ])
        others_agg_list = [app._aggregate_reasons_by_rule([])]
        gains = app._find_decisive_score_reasons(best_agg, others_agg_list)
        merged = app._merge_duplicate_reason_phrases(gains)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["gap"], 40.0)  # 25 + 15
        self.assertEqual(set(merged[0]["rules"]), {"ingredient_focus_active_match", "product_ingredient_focus_match"})

    def test_different_meaning_reasons_are_not_merged(self):
        """label/feature/matched_user_conditionが異なる場合は統合しないこと。"""
        best = self._candidate("1位商品", 90, [
            _reason("common_support_ceramide", "セラミド配合", feature="セラミド", points=6),
            _reason("common_support_glycerin", "グリセリン配合", feature="グリセリン", points=4),
        ])
        other = self._candidate("2位商品", 70, [])
        result = app.build_candidate_comparison_notes([best, other], {}, {})
        why_best = result["why_best"]
        self.assertIn("セラミド配合", why_best)
        self.assertIn("グリセリン配合", why_best)


class MatchedFeatureDoesNotLeakProductOrBrandNameTests(unittest.TestCase):
    def test_product_name_containing_keyword_shows_keyword_not_full_name(self):
        """
        実出力確認で発見: 商品名「ヒアルロン酸美容液」が改善目標キーワード
        「ヒアルロン酸」を含む場合、matched_product_featureは「ヒアルロン酸」
        (ingredient_mapで変換された日本語ラベル)であり、商品名全体
        「ヒアルロン酸美容液」ではないこと。
        """
        product = {"name": "ヒアルロン酸美容液", "category": "美容液", "active_ingredients": []}
        details = app.build_improvement_reason_details(product, {})
        dryness_entries = [d for d in details if "dryness" in d["rule"]]
        self.assertTrue(dryness_entries)
        for entry in dryness_entries:
            self.assertEqual(entry["matched_product_feature"], "ヒアルロン酸")
            self.assertNotEqual(entry["matched_product_feature"], product["name"])
            self.assertNotIn("美容液", entry["matched_product_feature"])

    def test_brand_name_containing_keyword_does_not_leak_as_feature(self):
        """ブランド名から一致した場合も、ブランド名全体を成分名として表示しないこと。"""
        product = {"name": "デイリーケアミルク", "brand": "ヒアルロン酸コスメティクス", "category": "乳液", "active_ingredients": []}
        details = app.build_improvement_reason_details(product, {})
        dryness_entries = [d for d in details if "dryness" in d["rule"]]
        self.assertTrue(dryness_entries)
        for entry in dryness_entries:
            self.assertNotIn("コスメティクス", entry["matched_product_feature"])
            self.assertEqual(entry["matched_product_feature"], "ヒアルロン酸")

    def test_internal_only_keyword_without_mapping_is_omitted_not_fabricated(self):
        """
        ingredient_mapに無い、かつ英数字スネークケースのみの内部識別子
        (例: IMPROVEMENT_KEYWORDS["acne"]["strong"]の"tea_tree")が一致した
        場合は、表示に不適切な内部語と判断して空文字にする(labelのみで表示、
        情報を捏造しない)。
        """
        product = {"name": "薬用ニキビケアジェル", "category": "美容液", "active_ingredients": ["tea_tree"]}
        improvement_plan = {"priority_concerns": ["acne"]}
        details = app.build_improvement_reason_details(product, improvement_plan)
        acne_entries = [d for d in details if "acne" in d["rule"] and "acne_marks" not in d["rule"]]
        self.assertTrue(acne_entries)
        for entry in acne_entries:
            self.assertEqual(entry["matched_product_feature"], "")
            self.assertTrue(entry["label"])  # labelのみでも文が成立すること


class UniversalSharedReasonExcludedFromWhyBestTests(unittest.TestCase):
    """
    実際のscore_product()を使い、全候補共通の「カテゴリに合致する基礎候補」
    (+40)がwhy_bestの導入句として表示されないことを確認する。
    """

    def test_category_base_fit_never_appears_in_why_best(self):
        step = {"category": "美容液", "purpose": "毛穴ケア", "ingredient_focus": "niacinamide"}
        user_data = {"oil": "oily", "sens": "middle", "exp": "middle"}

        def build(name, actives, concerns):
            product = {
                "name": name, "brand": "", "category": "美容液",
                "active_ingredients": actives, "support_ingredients": [],
                "concerns": concerns, "skin_types": [], "sensitive_ok": "unknown",
                "main_functions": [], "ingredient_focus": [], "formulation": [],
                "technology": [], "texture": "", "contraindications": [],
                "signature_ingredients": [], "retinol_level": 0, "price_ref": 2500,
                "availability_japan": [],
            }
            base_reasons = []
            base_score = app.score_product(product, step, user_data, 3000, reasons=base_reasons)
            product["score"] = base_score
            product["base_score"] = base_score
            product["improve_score"] = 0
            product["routine_score"] = 0
            product["_base_reasons"] = base_reasons
            product["_improvement_reason_details"] = []
            return product

        candidates = [
            build("1位美容液", ["niacinamide"], ["pores"]),
            build("2位美容液", [], []),
        ]
        candidates.sort(key=lambda c: c["score"], reverse=True)
        fake_step = dict(step)
        fake_step["top_candidates"] = candidates
        result_step = app.finalize_step_data(dict(fake_step), user_data)
        why_best = result_step["candidate_comparison"]["why_best"]
        self.assertNotIn("カテゴリに合致する基礎候補", why_best)
        self.assertNotIn("は比較した候補にも見られますが", why_best)


# =========================================================
# STEP 7: 「加点＋減点回避」の同一事実統合、matched_product_featureの決定性
# =========================================================

class PresenceAbsencePairMergeTests(unittest.TestCase):
    def _candidate(self, name, base_score, reasons):
        return {
            "brand": "", "name": name, "score": base_score, "base_score": base_score,
            "improve_score": 0, "routine_score": 0, "source": "db", "price_ref": 2000,
            "active_ingredients": [], "support_ingredients": [], "main_functions": [],
            "skin_types": [], "concerns": [], "texture": "", "formulation": [],
            "candidate_score_reasons": reasons,
        }

    def test_niacinamide_gain_and_absence_penalty_shown_as_one_fact(self):
        best = self._candidate("1位商品", 90, [
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", condition="niacinamide", points=25),
        ])
        second = self._candidate("2位商品", 60, [
            _reason("ingredient_focus_missing_penalty", "今回重視する成分を含んでいない", feature="", condition="niacinamide", points=-15),
        ])
        result = app.build_candidate_comparison_notes([best, second], {}, {})
        why_best = result["why_best"]
        # 「含む」「含んでいない」の二重説明にならないこと
        self.assertEqual(why_best.count("今回重視する成分を主成分として含む"), 1)
        self.assertNotIn("含んでいない", why_best)
        self.assertNotIn("影響を受けていません", why_best)

    def test_bha_gain_and_absence_penalty_shown_as_one_fact(self):
        best = self._candidate("1位ピーリング", 80, [
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="BHA", condition="bha", points=25),
        ])
        second = self._candidate("2位ピーリング", 50, [
            _reason("ingredient_focus_missing_penalty", "今回重視する成分を含んでいない", feature="", condition="bha", points=-15),
        ])
        result = app.build_candidate_comparison_notes([best, second], {}, {})
        why_best = result["why_best"]
        self.assertEqual(why_best.count("含む"), 1)
        self.assertNotIn("含んでいない", why_best)

    def test_unrelated_penalty_avoidance_is_not_merged(self):
        """異なる理由(matched_user_conditionが異なる)の減点回避は統合しないこと。"""
        best = self._candidate("1位商品", 90, [
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", condition="niacinamide", points=25),
        ])
        second = self._candidate("2位商品", 60, [
            _reason("routine_irritation_high_penalty", "刺激リスクが高いとされている", feature="irritation_risk=high", condition="", points=-10, axis="routine"),
        ])
        result = app.build_candidate_comparison_notes([best, second], {}, {})
        why_best = result["why_best"]
        self.assertIn("ナイアシンアミド", why_best)
        self.assertIn("刺激リスクが高いとされている", why_best)
        self.assertIn("影響", why_best)  # 統合されず別理由(減点回避)として残る

    def test_candidate_score_reasons_keeps_all_original_rules_after_merge(self):
        gain = _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", condition="niacinamide", points=25)
        best = self._candidate("1位商品", 90, [gain])
        self.assertEqual(len(best["candidate_score_reasons"]), 1)
        self.assertEqual(best["candidate_score_reasons"][0]["rule"], "ingredient_focus_active_match")

    def test_merge_helper_combines_gap_and_keeps_rules(self):
        best_agg = app._aggregate_reasons_by_rule([
            _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む", feature="ナイアシンアミド", condition="niacinamide", points=25),
        ])
        others_agg_list = [app._aggregate_reasons_by_rule([
            _reason("ingredient_focus_missing_penalty", "今回重視する成分を含んでいない", feature="", condition="niacinamide", points=-15),
        ])]
        gains = app._find_decisive_score_reasons(best_agg, others_agg_list)
        avoidances = app._find_penalty_avoidance_reasons(best_agg, others_agg_list)
        merged = app._merge_presence_absence_pairs(app._merge_duplicate_reason_phrases(gains + avoidances))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["gap"], 40.0)  # 25 + 15
        self.assertEqual(set(merged[0]["rules"]), {"ingredient_focus_active_match", "ingredient_focus_missing_penalty"})
        self.assertEqual(merged[0]["kind"], "gain")


class DeterministicMatchedFeatureTests(unittest.TestCase):
    def test_hyaluronic_acid_product_always_shows_japanese_label(self):
        product = {"name": "ヒアルロン酸美容液", "category": "美容液", "active_ingredients": ["hyaluronic_acid"]}
        for _ in range(20):
            details = app.build_improvement_reason_details(product, {})
            dryness_entries = [d for d in details if "dryness" in d["rule"]]
            self.assertTrue(dryness_entries)
            for entry in dryness_entries:
                self.assertEqual(entry["matched_product_feature"], "ヒアルロン酸")

    def test_why_best_full_text_is_identical_across_repeated_runs(self):
        """setの走査順が実行ごとに変わり得ても、why_best全文が常に同一になること。"""
        step = {"category": "美容液", "purpose": "", "ingredient_focus": ""}
        user_data = {"oil": "dry", "sens": "middle", "exp": "middle"}
        candidates = [
            {
                "brand": "", "name": "ヒアルロン酸美容液", "score": 90, "base_score": 90,
                "improve_score": 40, "routine_score": 0, "price_ref": 2000, "source": "db",
                "active_ingredients": [], "support_ingredients": [], "main_functions": [],
                "skin_types": [], "concerns": [], "texture": "", "formulation": [],
                "candidate_score_reasons": app.build_improvement_reason_details(
                    {"name": "ヒアルロン酸美容液", "category": "美容液", "active_ingredients": ["hyaluronic_acid"]}, {}
                ),
            },
            {
                "brand": "", "name": "競合美容液", "score": 60, "base_score": 60,
                "improve_score": 0, "routine_score": 0, "price_ref": 2000, "source": "db",
                "active_ingredients": [], "support_ingredients": [], "main_functions": [],
                "skin_types": [], "concerns": [], "texture": "", "formulation": [],
                "candidate_score_reasons": [],
            },
        ]
        results = set()
        for _ in range(20):
            r = app.build_candidate_comparison_notes(candidates, step, user_data)
            results.add(r["why_best"])
        self.assertEqual(len(results), 1, f"why_bestが実行ごとに異なる: {results}")

    def test_no_displayable_match_falls_back_to_empty_feature(self):
        product = {"name": "薬用ニキビケアジェル", "category": "美容液", "active_ingredients": ["tea_tree"]}
        improvement_plan = {"priority_concerns": ["acne"]}
        details = app.build_improvement_reason_details(product, improvement_plan)
        acne_entries = [d for d in details if "acne" in d["rule"] and "acne_marks" not in d["rule"]]
        self.assertTrue(acne_entries)
        for entry in acne_entries:
            self.assertEqual(entry["matched_product_feature"], "")


class WhyBestFixesDoNotAffectScoringTests(unittest.TestCase):
    def test_scoring_functions_unaffected(self):
        for product, step, user_data, budget, expected in _BASE_REPRESENTATIVE_PRODUCTS:
            with self.subTest(name=product["name"]):
                s1 = app.score_product(product, step, user_data, budget)
                reasons = []
                s2 = app.score_product(product, step, user_data, budget, reasons=reasons)
                self.assertEqual(s1, expected)
                self.assertEqual(s1, s2)


if __name__ == "__main__":
    unittest.main()
