"""Step48.3: Product Masterのサプリ行を、保存済みの適格性根拠で診断・coverageに
使うかを判定するテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402


def _row(label, **attrs):
    code = f"s483{label}"
    return {"brand": f"ブランド{label}", "name": f"亜鉛サプリ{label}", "category": "サプリメント",
            "active_ingredients": ["亜鉛"],
            "category_attributes": dict({"primary_ingredient_tags": ["zinc"]}, **attrs),
            "item_code": code, "rakuten_link": f"https://item.rakuten.co.jp/shop/{code}/",
            "image": f"https://image.example.com/{code}.jpg",
            "rakuten_title": f"ブランド{label} 亜鉛サプリ{label}", "price_ref": 1500}


ELIGIBLE = {
    "classification": _row("A", product_classification="supplement", supplement_eligibility_basis="classification"),
    "function_claims": _row("B", product_classification="foods_with_function_claims",
                            supplement_eligibility_basis="classification"),
    "stage1_evidence": _row("C", product_classification="unknown", supplement_eligibility_basis="stage1_evidence"),
}
INELIGIBLE = {
    "drug": _row("D", product_classification="drug", supplement_eligibility_basis="stage1_evidence"),
    "quasi_drug": _row("E", product_classification="quasi_drug", supplement_eligibility_basis="classification"),
    "no_basis": _row("F"),
    "no_basis_with_food_class": _row("G", product_classification="supplement"),
    "explicitly_ineligible": _row("H", product_classification="unknown", supplement_eligibility_basis=None),
    "unknown_class_with_classification_basis": _row("I", product_classification="unknown",
                                                    supplement_eligibility_basis="classification"),
    "invalid_basis": _row("J", product_classification="supplement", supplement_eligibility_basis="product_name"),
}


def _coverage(rows):
    with patch.object(app, "query_product_master_candidates", side_effect=lambda c, limit=30: [dict(r) for r in rows]):
        return {a["target"]: a["effective_count"] for a in app.get_coverage_report(
            "supplement", db_products=[], verified_products=[])}["zinc"]


def _diagnosis_candidates(rows):
    return app._product_master_candidates_for_live_style_ranking(
        "サプリメント", "zinc", "亜鉛 サプリメント", "", master_rows=[dict(r) for r in rows])


class EligibilityFunctionTests(unittest.TestCase):

    def test_eligible_rows(self):
        for label, row in ELIGIBLE.items():
            with self.subTest(label=label):
                self.assertTrue(app.is_supplement_product_master_eligible(row))

    def test_ineligible_rows(self):
        for label, row in INELIGIBLE.items():
            with self.subTest(label=label):
                self.assertFalse(app.is_supplement_product_master_eligible(row))
        self.assertFalse(app.is_supplement_product_master_eligible({"category_attributes": None}))

    def test_reflect_basis_values_are_accepted(self):
        # reflect/backfillで保存するbasisは、診断側の判定でそのまま適格になる。
        for text, product_class in (("1日2粒を目安に。", None), ("栄養機能食品。", "nutrient_function_food")):
            payload = {"category_attributes": {} if product_class is None else {
                "product_classification": {"value": product_class, "source_url": "u"}}}
            result = pipeline.supplement_eligibility(payload, text)
            row = {"category_attributes": {"product_classification": result["product_classification"],
                                           "supplement_eligibility_basis": result["basis"]}}
            self.assertTrue(app.is_supplement_product_master_eligible(row))


class DiagnosisAndCoverageTests(unittest.TestCase):

    def test_coverage_counts_only_eligible_rows(self):
        self.assertEqual(_coverage(list(ELIGIBLE.values())), 3)
        self.assertEqual(_coverage(list(INELIGIBLE.values())), 0)
        for label, row in INELIGIBLE.items():
            with self.subTest(label=label):
                self.assertEqual(_coverage([ELIGIBLE["classification"], row]), 1)

    def test_diagnosis_candidates_use_same_rule(self):
        names = {item.get("_pm_name") for _, item in _diagnosis_candidates(list(ELIGIBLE.values()) + list(INELIGIBLE.values()))}
        self.assertEqual(names, {r["name"] for r in ELIGIBLE.values()})

    def test_diagnosis_does_not_read_staging(self):
        with patch.object(orchestrator, "_fetch_staging_row", side_effect=AssertionError("staging参照禁止")), \
             patch.object(pipeline, "supplement_eligibility", side_effect=AssertionError("staging判定禁止")), \
             patch.object(app.psycopg2, "connect", side_effect=AssertionError("DB参照禁止")):
            self.assertEqual(len(_diagnosis_candidates(list(ELIGIBLE.values()))), 3)

    def test_device_rows_are_unaffected(self):
        row = {"brand": "D", "name": "美顔器X", "category": "美容機器", "category_attributes": {"method": "RF"},
               "item_code": "dev:1", "rakuten_link": "https://item.rakuten.co.jp/shop/dev1/",
               "image": "https://image.example.com/dev1.jpg", "rakuten_title": "D 美顔器X", "price_ref": 30000}
        self.assertEqual(len(app._product_master_candidates_for_live_style_ranking(
            "美容機器", "RF", "RF美顔器", "", master_rows=[row])), 1)


if __name__ == "__main__":
    unittest.main()
