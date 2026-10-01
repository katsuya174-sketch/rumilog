"""
問診の新規項目(uv_frequency/avoid_preference/reaction_history/strict_avoid/
pregnant/breastfeeding)に関するテスト。

設計(ユーザーとの合意事項):
- 苦手な成分は3段階: avoid_preference(減点) < reaction_history(強い減点、
  ただしビタミンC×高濃度のみ除外) < strict_avoid(除外)。
- 妊娠中: retinoid(レチノール/レチナール)は常にハード除外。サリチル酸/BHAは
  ingredient_strengthがhigh(高濃度相当)の場合のみ除外、それ以外は医師相談
  表示。AHAは一律除外せず医師相談表示のみ(ACOGが局所使用のサリチル酸/
  グリコール酸を妊娠中使用可能なOTC成分として挙げているため)。
- 授乳中: 一切ハード除外しない。retinoid/サリチル酸・BHA配合の商品には
  「授乳中は医師にご相談ください」とだけ短く表示する(部位等の詳細は書かない)。

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


def _base_product(**overrides):
    product = {
        "name": "テスト商品", "brand": "テストブランド", "category": "美容液",
        "active_ingredients": [], "support_ingredients": [],
        "concerns": [], "skin_types": [], "sensitive_ok": "unknown",
        "main_functions": [], "ingredient_focus": [], "formulation": [],
        "technology": [], "texture": "", "contraindications": [],
        "signature_ingredients": [], "retinol_level": 0, "price_ref": 2500,
        "availability_japan": ["rakuten"], "ingredient_strength": {},
    }
    product.update(overrides)
    return product


def _valid_base_user_data(**overrides):
    user_data = {"oil": "", "sens": "", "exp": "beginner", "concerns": []}
    user_data.update(overrides)
    return user_data


class ValidateQuestionnaireValuesNewFieldsTests(unittest.TestCase):
    def test_valid_uv_frequency_passes(self):
        for value in ("", "rarely", "sometimes", "daily"):
            ok, _ = app.validate_questionnaire_values(_valid_base_user_data(uv_frequency=value))
            self.assertTrue(ok, msg=value)

    def test_invalid_uv_frequency_fails(self):
        ok, message = app.validate_questionnaire_values(_valid_base_user_data(uv_frequency="always"))
        self.assertFalse(ok)
        self.assertIn("uv_frequency", message)

    def test_valid_ingredient_concern_tags_pass_for_all_three_tiers(self):
        for field in ("avoid_preference", "reaction_history", "strict_avoid"):
            ok, _ = app.validate_questionnaire_values(
                _valid_base_user_data(**{field: ["alcohol", "fragrance", "uv_absorber", "vitamin_c"]})
            )
            self.assertTrue(ok, msg=field)

    def test_invalid_ingredient_concern_tag_fails(self):
        ok, message = app.validate_questionnaire_values(_valid_base_user_data(reaction_history=["hydroquinone"]))
        self.assertFalse(ok)
        self.assertIn("reaction_history", message)

    def test_missing_new_fields_default_to_valid(self):
        # 既存の問診データ(新規フィールド無し)でも後方互換でvalidであること
        ok, _ = app.validate_questionnaire_values({"oil": "", "sens": "", "exp": "beginner", "concerns": []})
        self.assertTrue(ok)


class IsExcludedForPregnancyTests(unittest.TestCase):
    def test_retinol_is_always_excluded(self):
        product = _base_product(active_ingredients=["retinol"])
        self.assertTrue(app.is_excluded_for_pregnancy(product))

    def test_retinal_is_always_excluded(self):
        product = _base_product(active_ingredients=["retinal"])
        self.assertTrue(app.is_excluded_for_pregnancy(product))

    def test_high_concentration_salicylic_acid_is_excluded(self):
        product = _base_product(
            active_ingredients=["salicylic_acid"],
            ingredient_strength={"salicylic_acid": "high"},
        )
        self.assertTrue(app.is_excluded_for_pregnancy(product))

    def test_high_concentration_bha_is_excluded(self):
        product = _base_product(active_ingredients=["bha"], ingredient_strength={"bha": "high"})
        self.assertTrue(app.is_excluded_for_pregnancy(product))

    def test_medium_concentration_salicylic_acid_is_not_excluded(self):
        product = _base_product(
            active_ingredients=["salicylic_acid"],
            ingredient_strength={"salicylic_acid": "medium"},
        )
        self.assertFalse(app.is_excluded_for_pregnancy(product))

    def test_unknown_concentration_salicylic_acid_is_not_excluded(self):
        product = _base_product(active_ingredients=["salicylic_acid"], ingredient_strength={})
        self.assertFalse(app.is_excluded_for_pregnancy(product))

    def test_aha_is_never_excluded_regardless_of_strength(self):
        product = _base_product(active_ingredients=["aha"], ingredient_strength={"aha": "high"})
        self.assertFalse(app.is_excluded_for_pregnancy(product))

    def test_glycolic_acid_is_never_excluded(self):
        product = _base_product(active_ingredients=["glycolic_acid"], ingredient_strength={"glycolic_acid": "high"})
        self.assertFalse(app.is_excluded_for_pregnancy(product))

    def test_unrelated_product_is_not_excluded(self):
        product = _base_product(active_ingredients=["niacinamide"])
        self.assertFalse(app.is_excluded_for_pregnancy(product))


class PregnancyBreastfeedingCautionTagsTests(unittest.TestCase):
    def test_pregnant_with_non_high_salicylic_acid_gets_consult_tag(self):
        product = _base_product(
            active_ingredients=["salicylic_acid"],
            ingredient_strength={"salicylic_acid": "medium"},
        )
        tags = app.get_pregnancy_breastfeeding_caution_tags(product, {"pregnant": True})
        self.assertIn("pregnancy_salicylic_bha_consult", tags)

    def test_pregnant_with_high_salicylic_acid_gets_no_consult_tag(self):
        # 高濃度はis_excluded_for_pregnancy側でハード除外される対象のため、
        # ここでは注意タグを重複して付与しない。
        product = _base_product(
            active_ingredients=["salicylic_acid"],
            ingredient_strength={"salicylic_acid": "high"},
        )
        tags = app.get_pregnancy_breastfeeding_caution_tags(product, {"pregnant": True})
        self.assertNotIn("pregnancy_salicylic_bha_consult", tags)

    def test_pregnant_with_aha_gets_consult_tag(self):
        product = _base_product(active_ingredients=["aha"])
        tags = app.get_pregnancy_breastfeeding_caution_tags(product, {"pregnant": True})
        self.assertIn("pregnancy_aha_consult", tags)

    def test_breastfeeding_with_retinol_gets_short_consult_tag(self):
        product = _base_product(active_ingredients=["retinol"])
        tags = app.get_pregnancy_breastfeeding_caution_tags(product, {"breastfeeding": True})
        self.assertIn("breastfeeding_consult", tags)
        self.assertEqual(len(tags), 1)

    def test_breastfeeding_with_salicylic_acid_gets_short_consult_tag(self):
        product = _base_product(active_ingredients=["bha"])
        tags = app.get_pregnancy_breastfeeding_caution_tags(product, {"breastfeeding": True})
        self.assertIn("breastfeeding_consult", tags)

    def test_breastfeeding_with_unrelated_product_gets_no_tag(self):
        product = _base_product(active_ingredients=["niacinamide"])
        tags = app.get_pregnancy_breastfeeding_caution_tags(product, {"breastfeeding": True})
        self.assertEqual(tags, [])

    def test_breastfeeding_label_text_has_no_body_part_detail(self):
        # ユーザー指摘により「乳首・乳輪」等の部位詳細は書かないと決まっている。
        from constants import contraindications_labels
        label = contraindications_labels["breastfeeding_consult"]
        self.assertEqual(label, "授乳中は医師にご相談ください")

    def test_neither_pregnant_nor_breastfeeding_gets_no_tags(self):
        product = _base_product(active_ingredients=["retinol", "salicylic_acid", "aha"])
        tags = app.get_pregnancy_breastfeeding_caution_tags(product, {})
        self.assertEqual(tags, [])


class IngredientConcernHardExcludeMatchTests(unittest.TestCase):
    def test_strict_avoid_vitamin_c_excludes_matching_product(self):
        product = _base_product(active_ingredients=["vitamin_c"])
        self.assertTrue(app._ingredient_concern_hard_exclude_match(product, {"strict_avoid": ["vitamin_c"]}))

    def test_strict_avoid_alcohol_excludes_product_without_free_tag(self):
        product = _base_product(formulation=[])
        self.assertTrue(app._ingredient_concern_hard_exclude_match(product, {"strict_avoid": ["alcohol"]}))

    def test_strict_avoid_fragrance_does_not_exclude_fragrance_free_product(self):
        product = _base_product(formulation=["fragrance_free"])
        self.assertFalse(app._ingredient_concern_hard_exclude_match(product, {"strict_avoid": ["fragrance"]}))

    def test_reaction_history_vitamin_c_high_strength_excludes(self):
        product = _base_product(active_ingredients=["vitamin_c"], ingredient_strength={"vitamin_c": "high"})
        self.assertTrue(app._ingredient_concern_hard_exclude_match(product, {"reaction_history": ["vitamin_c"]}))

    def test_reaction_history_vitamin_c_medium_strength_does_not_exclude(self):
        product = _base_product(active_ingredients=["vitamin_c"], ingredient_strength={"vitamin_c": "medium"})
        self.assertFalse(app._ingredient_concern_hard_exclude_match(product, {"reaction_history": ["vitamin_c"]}))

    def test_reaction_history_non_vitamin_c_never_hard_excludes(self):
        # alcohol/fragrance/uv_absorberのreaction_historyは強い減点のみで、除外はしない。
        product = _base_product(formulation=[])
        self.assertFalse(app._ingredient_concern_hard_exclude_match(product, {"reaction_history": ["alcohol"]}))


class ApplyCommonScoreRulesIngredientPenaltyTests(unittest.TestCase):
    def _score(self, product, user_data):
        step = {"category": "美容液", "purpose": "", "ingredient_focus": ""}
        reasons = []
        score = app.apply_common_score_rules(
            product, step, user_data, 0, concern_tags=[], ingredient_tag="", reasons=reasons
        )
        return score, reasons

    def test_avoid_preference_applies_small_penalty(self):
        product = _base_product(formulation=[])
        score, reasons = self._score(product, {"avoid_preference": ["fragrance"]})
        self.assertTrue(any(r["rule"] == "ingredient_avoid_preference_penalty" and r["points"] == -8 for r in reasons))

    def test_reaction_history_applies_stronger_penalty(self):
        product = _base_product(formulation=[])
        score, reasons = self._score(product, {"reaction_history": ["fragrance"]})
        self.assertTrue(any(r["rule"] == "ingredient_reaction_history_penalty" and r["points"] == -25 for r in reasons))

    def test_same_tag_in_both_tiers_is_not_double_counted(self):
        product = _base_product(formulation=[])
        score, reasons = self._score(
            product, {"avoid_preference": ["fragrance"], "reaction_history": ["fragrance"]}
        )
        matching = [r for r in reasons if r["matched_product_feature"] == "fragrance"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["rule"], "ingredient_reaction_history_penalty")

    def test_no_penalty_when_product_has_free_tag(self):
        product = _base_product(formulation=["fragrance_free"])
        score, reasons = self._score(product, {"avoid_preference": ["fragrance"]})
        self.assertFalse(any(r["matched_product_feature"] == "fragrance" for r in reasons))


class ScoreProductHardExcludeTests(unittest.TestCase):
    def _score(self, product, user_data, category="美容液"):
        step = {"category": category, "purpose": "", "ingredient_focus": ""}
        return app.score_product(product, step, user_data, 0)

    def test_pregnant_with_retinol_product_is_hard_excluded(self):
        product = _base_product(active_ingredients=["retinol"])
        score = self._score(product, {"pregnant": True})
        self.assertEqual(score, -9999)

    def test_pregnant_with_high_salicylic_acid_is_hard_excluded(self):
        product = _base_product(
            active_ingredients=["salicylic_acid"],
            ingredient_strength={"salicylic_acid": "high"},
        )
        score = self._score(product, {"pregnant": True})
        self.assertEqual(score, -9999)

    def test_pregnant_with_medium_salicylic_acid_is_not_excluded_and_gets_caution_tag(self):
        product = _base_product(
            active_ingredients=["salicylic_acid"],
            ingredient_strength={"salicylic_acid": "medium"},
        )
        score = self._score(product, {"pregnant": True})
        self.assertNotEqual(score, -9999)
        self.assertIn("pregnancy_salicylic_bha_consult", product.get("contraindications", []))

    def test_strict_avoid_vitamin_c_is_hard_excluded(self):
        product = _base_product(active_ingredients=["vitamin_c"])
        score = self._score(product, {"strict_avoid": ["vitamin_c"]})
        self.assertEqual(score, -9999)

    def test_reaction_history_vitamin_c_high_strength_is_hard_excluded(self):
        product = _base_product(active_ingredients=["vitamin_c"], ingredient_strength={"vitamin_c": "high"})
        score = self._score(product, {"reaction_history": ["vitamin_c"]})
        self.assertEqual(score, -9999)

    def test_non_pregnant_user_is_unaffected_by_pregnancy_rules(self):
        product = _base_product(active_ingredients=["retinol"])
        score = self._score(product, {"pregnant": False})
        self.assertNotEqual(score, -9999)


if __name__ == "__main__":
    unittest.main()
