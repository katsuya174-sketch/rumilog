"""Step48.2: サプリ適格性(eligibility)をclassificationと分離した判定のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s482_"


def _payload(product_class=None):
    attrs = {}
    if product_class is not None:
        attrs["product_classification"] = {"value": product_class, "confidence": "high", "source_url": CIT}
    return {"active_ingredients": [{"ingredient": "ビタミンC", "source_url": CIT}],
            "formulation_features": [], "category_attributes": attrs}


def eligibility(text, product_class=None):
    return pipeline.supplement_eligibility(_payload(product_class), text)


# 保存済みStage1本文(#104〜#106)と同じ書式。区分表示と食品らしい摂取表現が混在する。
DRUG_TEXT_WITH_INTAKE = ("本製品はサプリメント（健康食品）ではなく、第3類医薬品として販売されています。\n"
                         "* **1日の摂取目安量：** 成人は4錠（1日2回、1回2錠）\n1日4錠を目安に。")
# 保存済みStage1本文(既存サプリ)と同じ見出し+値の書式。
SUPPLEMENT_HEADING_TEXT = "*   **1日の摂取目安量**\n    *   2粒\n*   **1回あたりの摂取量**\n    *   1粒"


class ExclusionTests(unittest.TestCase):

    def test_drug_classes_are_never_eligible(self):
        for label in ("第1類医薬品", "第2類医薬品", "指定第2類医薬品", "第3類医薬品", "要指導医薬品", "第２類医薬品"):
            with self.subTest(label=label):
                result = eligibility(f"【{label}】1日2粒を目安にお召し上がりください。サプリメント。", "supplement")
                self.assertEqual((result["eligible"], result["product_classification"]), (False, "drug"))

    def test_quasi_drug_is_never_eligible(self):
        result = eligibility("医薬部外品。1日2粒を目安に。", "foods_with_function_claims")
        self.assertEqual((result["eligible"], result["reason"]), (False, "quasi_drug"))

    def test_drug_with_daily_tablets_is_drug(self):
        result = eligibility(DRUG_TEXT_WITH_INTAKE)
        self.assertEqual((result["eligible"], result["product_classification"]), (False, "drug"))
        self.assertTrue(pipeline.supplement_intake_evidence(DRUG_TEXT_WITH_INTAKE))  # 食品らしい表現があってもdrug優先

    def test_stage2_drug_value_is_excluded(self):
        self.assertFalse(eligibility("1日2粒を目安に。", "drug")["eligible"])

    def test_medicine_precaution_alone_is_not_drug(self):
        text = "医薬品を服用している場合は医師、薬剤師に相談してください。1日2粒を目安に。"
        result = eligibility(text)
        self.assertEqual(result["product_classification"], "unknown")
        self.assertTrue(result["eligible"])


class ClassificationBasisTests(unittest.TestCase):

    def test_food_classes_are_eligible(self):
        for text, product_class in (("機能性表示食品。", "foods_with_function_claims"),
                                    ("栄養機能食品(ビタミンC)。", "nutrient_function_food"),
                                    ("サプリメント。", "supplement"), ("健康食品。", "health_food")):
            with self.subTest(product_class=product_class):
                result = eligibility(text, product_class)
                self.assertEqual((result["eligible"], result["basis"]), (True, "classification"))


class UnknownEvidenceTests(unittest.TestCase):

    def assertEligibleBy(self, text, kind):
        result = eligibility(text)
        self.assertEqual((result["product_classification"], result["eligible"], result["basis"]),
                         ("unknown", True, "stage1_evidence"))
        self.assertIn(kind, [e["kind"] for e in result["evidence"]])

    def test_daily_intake_guideline(self):
        self.assertEligibleBy("1日2粒を目安にお召し上がりください。", "daily_intake_guideline")
        self.assertEligibleBy("目安として1日2粒。", "daily_intake_guideline")
        self.assertEligibleBy("1日の摂取目安量：3カプセル", "daily_intake_guideline")
        self.assertEligibleBy(SUPPLEMENT_HEADING_TEXT, "daily_intake_guideline")

    def test_serving_directions(self):
        self.assertEligibleBy("お召し上がり方：水などと一緒に1日2粒", "serving_directions")

    def test_explicit_supplement_terms(self):
        self.assertEligibleBy("ビタミンCの栄養補助食品です。", "explicit_supplement_term")
        self.assertEligibleBy("DHCのサプリメント。", "explicit_supplement_term")

    def test_nutrition_label_with_intake(self):
        text = "エネルギー：8.34kcal、タンパク質：0.2g、脂質：0.7g。摂取目安量を守ってください。"
        self.assertEligibleBy(text, "nutrition_label_with_intake")
        self.assertFalse(eligibility("エネルギー：8.34kcal、タンパク質：0.2g、脂質：0.7g。")["eligible"])

    def test_shape_alone_is_not_evidence(self):
        for text in ("ハードカプセルタイプ。", "1粒あたりビタミンC 500mg。", "120粒入り", "1日2錠。"):
            with self.subTest(text=text):
                result = eligibility(text)
                self.assertEqual((result["eligible"], result["reason"]), (False, "no_supplement_evidence"))

    def test_no_evidence_is_not_eligible(self):
        self.assertEqual(eligibility("")["reason"], "no_supplement_evidence")
        self.assertEqual(eligibility("ビタミンC 1000mg配合。")["reason"], "no_supplement_evidence")

    def test_negated_supplement_term_is_not_evidence(self):
        for text in ("本製品はサプリメントではありません。", "サプリメント（健康食品）ではなく、錠剤です。",
                     "栄養補助を目的とした「サプリメント」とは異なります。"):
            with self.subTest(text=text):
                self.assertFalse(eligibility(text)["eligible"])

    def test_drug_usage_context_blocks_unknown_evidence(self):
        result = eligibility("用法・用量：1日2粒を目安に。服用後、発疹があらわれた場合は中止。")
        self.assertEqual((result["eligible"], result["reason"]), (False, "drug_usage_context"))
        # 食品区分が明示されていればclassificationで適格。
        self.assertTrue(eligibility("機能性表示食品。副作用の報告は無い。", "foods_with_function_claims")["eligible"])

    def test_product_name_alone_is_not_evidence(self):
        # 名前・ブランドはStage1本文の根拠ではないため使わない(本文が空なら不適格)。
        self.assertFalse(pipeline.supplement_eligibility(_payload(), None)["eligible"])


class SavedStagingShapeTests(unittest.TestCase):

    def test_saved_drug_texts_remain_drug(self):
        for text in ("なお、本製品は「第3類医薬品」であり、栄養補助を目的とした「サプリメント」とは異なります。",
                     DRUG_TEXT_WITH_INTAKE,
                     "本製品はサプリメントではなく、第3類医薬品となります。\n成人（15歳以上）：1日3回、計6錠"):
            with self.subTest(text=text[:20]):
                result = eligibility(text)
                self.assertEqual((result["eligible"], result["product_classification"]), (False, "drug"))


class GateTests(OrchestratorTestBase):

    def _insert(self, label, text, product_class=None):
        brand, name = f"BR{label}{TEST_NAME_SUFFIX}", f"商品{label}{TEST_NAME_SUFFIX}"
        self._insert_staging_row(self._new_batch_id(f"s482-{label}"), brand, name, "サプリメント",
                                 stage2_payload=_payload(product_class),
                                 citations=[{"uri": CIT + label, "title": "cosme.net"}], stage1_raw_text=text)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            return brand, name, cur.fetchone()[0]
        finally:
            conn.close()

    def test_unknown_with_evidence_proceeds_to_official_and_rakuten(self):
        brand, name, sid = self._insert("Ev", "1日2粒を目安にお召し上がりください。")
        evaluation = orchestrator.evaluate_staging_for_reflect(sid, brand, name, "サプリメント")
        self.assertNotEqual(evaluation["reason"], "supplement_not_eligible")
        self.assertEqual(evaluation["reason"], "official_source_not_confirmed_variant_uncertain")

    def test_unknown_without_evidence_stops_before_rakuten(self):
        brand, name, sid = self._insert("NoEv", "ビタミンC 1000mg配合。")
        budget = orchestrator.BatchBudget(self._new_batch_id("s482-exec"), 5, 20, 0.50)
        forbidden = AssertionError("適格性ゲート後に進まないはず")
        with patch.object(pipeline, "resolve_item_code_for_product", side_effect=forbidden), \
             patch.object(pipeline.citation_verification, "fetch_html", side_effect=forbidden):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "サプリメント", "target": "vitamin_c", "shortage_count": 1}, "execute", budget.batch_id,
                budget, lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                          "discovery_source": "staging_reuse", "staging_id": sid}], 3, set())
        self.assertEqual((actions[0]["reason"], actions[0]["eligibility_reason"]),
                         ("supplement_not_eligible", "no_supplement_evidence"))

    def test_reflect_records_basis(self):
        brand, name, sid = self._insert("Basis", "1日2粒を目安に。")
        attrs = pipeline.reflect_staging_to_product_master(sid, dry_run=True)["product"]["category_attributes"]
        self.assertEqual((attrs["product_classification"], attrs["supplement_eligibility_basis"]),
                         ("unknown", "stage1_evidence"))
        brand, name, sid = self._insert("NoBasis", "ビタミンC配合。")
        self.assertEqual(pipeline.reflect_staging_to_product_master(sid, dry_run=True)["reason"], "supplement_not_eligible")

    def test_cosmetics_and_device_unchanged(self):
        self.assertIsNone(orchestrator._supplement_eligibility("美容機器", {"stage1_raw_text": "第3類医薬品"}))
        self.assertIsNone(orchestrator._supplement_eligibility("美容液", {"stage1_raw_text": ""}))
        self.assertTrue(app.is_candidate_relevant_to_target("美容機器", "RF", {"category_attributes": {"method": "RF"}}))


if __name__ == "__main__":
    unittest.main()
