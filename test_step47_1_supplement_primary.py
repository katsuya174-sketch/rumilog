"""Step47.1: サプリの主要成分(primary)と副成分の分離のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s471_"


def _payload(actives, primary):
    return {
        "active_ingredients": [{"ingredient": a, "source_url": CIT} for a in actives],
        "category_attributes": {"primary_ingredients": {"value": primary, "confidence": "high", "source_url": CIT}},
    }


def _product(actives, primary_tags):
    attrs = {} if primary_tags is None else {"primary_ingredient_tags": primary_tags}
    return {"active_ingredients": list(actives), "category_attributes": attrs}


def relevant(target, product):
    return app.is_candidate_relevant_to_target("サプリメント", target, product)


class DerivePrimaryTagsTests(unittest.TestCase):

    def test_vitamin_c_with_b2_secondary(self):
        tags = pipeline.derive_primary_ingredient_tags(_payload(["ビタミンC", "ビタミンB2"], "ビタミンC"))
        self.assertEqual(tags, ["vitamin_c"])
        product = _product(["ビタミンC", "ビタミンB2"], tags)
        self.assertTrue(relevant("vitamin_c", product))
        self.assertFalse(relevant("vitamin_b", product))

    def test_vitamin_b_complex_product(self):
        tags = pipeline.derive_primary_ingredient_tags(
            _payload(["ビタミンB1", "ビタミンB2", "ビタミンB6", "ナイアシン"], "ビタミンB1、ビタミンB2、ビタミンB6"))
        self.assertEqual(tags, ["vitamin_b"])
        self.assertTrue(relevant("vitamin_b", _product([], tags)))

    def test_omega3_primary_with_secondary_vitamin_e(self):
        tags = pipeline.derive_primary_ingredient_tags(_payload(["DHA", "EPA", "ビタミンE"], "DHA、EPA"))
        self.assertEqual(tags, ["omega3"])
        product = _product(["DHA", "EPA", "ビタミンE"], tags)
        self.assertTrue(relevant("omega3", product))
        self.assertFalse(relevant("vitamin_e", product))

    def test_minor_epa_dha_only_is_not_omega3(self):
        tags = pipeline.derive_primary_ingredient_tags(_payload(["ビタミンC", "亜鉛", "DHA"], "ビタミンC、亜鉛"))
        self.assertEqual(tags, ["vitamin_c", "zinc"])  # 複合主成分は複数可
        self.assertFalse(relevant("omega3", _product(["ビタミンC", "亜鉛", "DHA"], tags)))

    def test_primary_unknown_is_not_counted(self):
        self.assertEqual(pipeline.derive_primary_ingredient_tags(_payload(["ビタミンC"], "unknown")), [])
        # Step47.2: 項目自体が無い旧データはNone(呼び出し元が商品名から決定論的に補完)。
        self.assertIsNone(pipeline.derive_primary_ingredient_tags({"active_ingredients": [{"ingredient": "ビタミンC"}]}))
        self.assertFalse(relevant("vitamin_c", _product(["ビタミンC"], [])))

    def test_no_fallback_to_all_active_ingredients(self):
        # primary_ingredient_tagsが無い既存サプリは、全active成分をprimary扱いしない。
        self.assertFalse(relevant("vitamin_c", {"active_ingredients": ["ビタミンC", "vitamin_c"]}))
        self.assertFalse(relevant("vitamin_c", _product(["ビタミンC"], None)))

    def test_primary_not_in_active_ingredients_is_rejected(self):
        self.assertEqual(pipeline.derive_primary_ingredient_tags(_payload(["ビタミンC"], "コラーゲン")), [])
        # タグ文字列そのもの(Stage2が任意に生成した値)は成分名として一致しないため採用しない。
        self.assertEqual(pipeline.derive_primary_ingredient_tags(_payload(["ビタミンC"], "vitamin_c")), [])

    def test_name_notation_variants_match(self):
        self.assertEqual(pipeline.derive_primary_ingredient_tags(_payload(["ビタミンC"], " ビタミンＣ ")), ["vitamin_c"])

    def test_uncited_primary_is_forced_unknown_by_sanitize(self):
        payload = _payload(["ビタミンC"], "ビタミンC")
        payload["category_attributes"]["primary_ingredients"]["source_url"] = "https://not-a-citation.example/"
        sanitized = pipeline.sanitize_stage2_payload(payload, {CIT}, "本文", brand="B", citations=[{"uri": CIT, "title": "x"}])
        self.assertEqual(pipeline.derive_primary_ingredient_tags(sanitized), [])

    def test_stage2_schema_has_primary_ingredients_for_supplements_only(self):
        self.assertIn("primary_ingredients", pipeline._CATEGORY_ATTRIBUTES_EXTRACTION_SPECS["サプリメント"])
        self.assertNotIn("primary_ingredients", pipeline._CATEGORY_ATTRIBUTES_EXTRACTION_SPECS["美容機器"])


class OtherCategoriesUnchangedTests(unittest.TestCase):

    def test_device_and_cosmetics_relevance_unchanged(self):
        self.assertTrue(app.is_candidate_relevant_to_target("美容機器", "RF", {"category_attributes": {"method": "RF"}}))
        product = {"brand": "B", "name": "テスト美容液", "category": "美容液", "active_ingredients": ["vitamin_c"]}
        reasons = []
        score = app.score_product(dict(product), {"category": "美容液", "purpose": "", "ingredient_focus": "vitamin_c"},
                                  app._EFFECTIVE_CANDIDATE_NEUTRAL_USER_DATA, app._EFFECTIVE_CANDIDATE_BUDGET_VALUE,
                                  reasons=reasons)
        self.assertEqual(app.is_candidate_relevant_to_target("美容液", "vitamin_c", product),
                         app._is_relevant_scored_candidate(score, reasons, "vitamin_c"))


class CoverageTests(unittest.TestCase):

    def test_coverage_counts_only_primary_products(self):
        def row(i, actives, primary):
            return {"brand": f"S{i}", "name": f"サプリ{i}", "category": "サプリメント", "active_ingredients": actives,
                    "category_attributes": dict({"supplement_eligibility_basis": "stage1_evidence"},
                                                **({} if primary is None else {"primary_ingredient_tags": primary})),
                    "item_code": f"s:{i}", "rakuten_link": f"https://item.rakuten.co.jp/s/{i}/",
                    "rakuten_title": f"S{i} サプリ{i}", "price_ref": 2000}
        rows = [row(1, ["ビタミンC", "ビタミンB2"], ["vitamin_c"]),
                row(2, ["ビタミンB1", "ビタミンB6"], ["vitamin_b"]),
                row(3, ["ビタミンB2"], None)]  # primary不明
        with patch.object(app, "query_product_master_candidates", return_value=rows):
            report = {a["target"]: a["effective_count"] for a in app.get_coverage_report("supplement",
                                                                                         db_products=[], verified_products=[])}
        self.assertEqual((report["vitamin_c"], report["vitamin_b"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
