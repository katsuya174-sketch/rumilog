"""Step46: サプリ成分(omega3/probiotics)の正規化修正のテスト。"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


class Omega3Tests(unittest.TestCase):

    def test_explicit_omega3_expressions(self):
        for text in ("オメガ3脂肪酸", "オメガ３脂肪酸", "オメガ-3", "オメガ 3", "omega-3 fatty acids", "Omega 3",
                     "omega3", "EPA", "DHA", "EPA・DHA含有精製魚油", "ＤＨＡ"):
            with self.subTest(text=text):
                self.assertEqual(app.normalize_ingredient_tag(text), "omega3")

    def test_generic_fatty_acids_are_not_omega3(self):
        for text in ("脂肪酸", "fatty acid", "不飽和脂肪酸", "オメガ6脂肪酸", "omega-6"):
            with self.subTest(text=text):
                self.assertNotEqual(app.normalize_ingredient_tag(text), "omega3")
        self.assertEqual(app.normalize_ingredient_tag("脂肪酸"), "fatty_acid")

    def test_epa_dha_substring_false_positives_do_not_return(self):
        for text in ("heparinoid", "Heparin", "Adhatoda Vasica Leaf Extract", "ヘパリン類似物質"):
            with self.subTest(text=text):
                self.assertNotEqual(app.normalize_ingredient_tag(text), "omega3")


class ProbioticsTests(unittest.TestCase):

    def test_explicit_bacteria_are_probiotics(self):
        for text in ("乳酸菌", "有胞子性乳酸菌", "ビフィズス菌", "ビフィズス菌BB536"):
            with self.subTest(text=text):
                self.assertEqual(app.normalize_ingredient_tag(text), "probiotics")

    def test_lactic_acid_and_lactates_are_not_probiotics(self):
        for text, expected in (("乳酸", "lactic_acid"), ("lactic acid", "lactic_acid"), ("乳酸Na", "lactic_acid"),
                               ("乳酸カルシウム", "lactic_acid")):
            with self.subTest(text=text):
                self.assertEqual(app.normalize_ingredient_tag(text), expected)
        self.assertNotEqual(app.normalize_ingredient_tag("sodium lactate"), "probiotics")

    def test_ferment_derivatives_keep_existing_cosmetic_tags(self):
        self.assertEqual(app.normalize_ingredient_tag("ビフィズス菌発酵エキス"), "bifida")
        self.assertEqual(app.normalize_ingredient_tag("Bifida Ferment Lysate"), "bifida")
        self.assertEqual(app.normalize_ingredient_tag("probiotic_ferment"), "probiotic_ferment")

    def test_supplement_relevance_uses_new_tags(self):
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "omega3", {"active_ingredients": ["オメガ3脂肪酸"]}))
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "probiotics", {"active_ingredients": ["有胞子性乳酸菌"]}))
        self.assertFalse(app.is_candidate_relevant_to_target("サプリメント", "probiotics", {"active_ingredients": ["乳酸カルシウム"]}))
        self.assertFalse(app.is_candidate_relevant_to_target("サプリメント", "omega3", {"active_ingredients": ["脂肪酸"]}))


class CosmeticsRegressionTests(unittest.TestCase):

    def test_representative_cosmetic_tags_unchanged(self):
        cases = {"ナイアシンアミド": "niacinamide", "ビタミンC誘導体": "vitamin_c", "ヒアルロン酸Na": "hyaluronic_acid",
                 "セラミドNP": "ceramide", "グリコール酸": "glycolic_acid", "サリチル酸": "salicylic_acid",
                 "パンテノール": "panthenol", "fatty_acid": "fatty_acid", "乳酸Na": "lactic_acid",
                 "乳酸球菌／ヒアルロン酸発酵液（乳酸発酵ヒアルロン酸）": "hyaluronic_acid"}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(app.normalize_ingredient_tag(text), expected)


if __name__ == "__main__":
    unittest.main()
