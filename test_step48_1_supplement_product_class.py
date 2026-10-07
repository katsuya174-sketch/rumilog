"""Step48.1: サプリ候補からの医薬品・医薬部外品の除外(商品区分ゲート)のテスト。

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

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s481_"


def _payload(product_class=None, cited=True):
    attrs = {"primary_ingredients": {"value": "L-システイン", "confidence": "high", "source_url": CIT}}
    if product_class is not None:
        attrs["product_classification"] = {"value": product_class, "confidence": "high",
                                           "source_url": CIT if cited else "unknown"}
    return {"active_ingredients": [{"ingredient": "L-システイン", "source_url": CIT}],
            "formulation_features": [], "category_attributes": attrs}


def classify(text, product_class=None):
    return pipeline.supplement_product_classification(_payload(product_class), text)


class ClassificationTests(unittest.TestCase):

    def test_drug_classes_are_drug(self):
        for text in ("本製品は第3類医薬品として販売されています。", "【第2類医薬品】", "指定第2類医薬品",
                     "第１類医薬品", "第 3 類医薬品", "要指導医薬品"):
            with self.subTest(text=text):
                self.assertEqual(classify(text, "supplement"), "drug")
                self.assertFalse(pipeline.is_supplement_product_class_reflectable(classify(text, "supplement")))

    def test_quasi_drug_is_excluded(self):
        self.assertEqual(classify("医薬部外品。有効成分配合。", "supplement"), "quasi_drug")
        self.assertFalse(pipeline.is_supplement_product_class_reflectable("quasi_drug"))

    def test_stage2_drug_value_is_excluded(self):
        self.assertEqual(classify("本品は医薬品です。", "drug"), "drug")

    def test_allowed_food_classes(self):
        for text, product_class in (("機能性表示食品(届出番号あり)", "foods_with_function_claims"),
                                    ("栄養機能食品(ビタミンC)", "nutrient_function_food"),
                                    ("健康食品です", "health_food"),
                                    ("栄養補助食品(サプリメント)", "supplement")):
            with self.subTest(product_class=product_class):
                self.assertEqual(classify(text, product_class), product_class)
                self.assertTrue(pipeline.is_supplement_product_class_reflectable(product_class))

    def test_unknown_and_missing_are_excluded(self):
        self.assertEqual(classify("1日2粒。", "unknown"), "unknown")
        self.assertEqual(classify("サプリメント。", None), "unknown")  # 項目の無い旧データ
        self.assertEqual(classify("サプリメント。", "not_a_class"), "unknown")
        self.assertFalse(pipeline.is_supplement_product_class_reflectable("unknown"))

    def test_allowed_class_requires_term_in_stage1_text(self):
        # Stage2(Gemini)の値だけでは許可区分にしない。
        self.assertEqual(classify("1日2粒を目安に。", "supplement"), "unknown")
        self.assertEqual(classify("サプリメント。", "foods_with_function_claims"), "unknown")

    def test_negated_class_term_is_not_evidence(self):
        self.assertEqual(classify("本製品はサプリメントではなく、医薬品です。", "supplement"), "unknown")
        self.assertEqual(classify("栄養補助を目的とした「サプリメント」とは異なります。", "supplement"), "unknown")
        self.assertEqual(classify("健康食品(いわゆる)ではありません", "health_food"), "unknown")

    def test_generic_mention_of_medicine_is_not_drug(self):
        # 注意書きの「医薬品を服用している場合」は区分表示ではない。
        text = "サプリメント。医薬品を服用している場合は医師に相談してください。"
        self.assertEqual(classify(text, "supplement"), "supplement")

    def test_uncited_classification_is_forced_unknown_by_sanitize(self):
        payload = _payload("supplement", cited=False)
        sanitized = pipeline.sanitize_stage2_payload(payload, {CIT}, "サプリメント", brand="B",
                                                     citations=[{"uri": CIT, "title": "x"}])
        self.assertEqual(pipeline.supplement_product_classification(sanitized, "サプリメント"), "unknown")


class SchemaAndPromptTests(unittest.TestCase):

    def test_stage2_schema_has_classification_for_supplements_only(self):
        spec = pipeline._CATEGORY_ATTRIBUTES_EXTRACTION_SPECS["サプリメント"]["product_classification"]
        self.assertEqual(list(spec.enum), list(app.SUPPLEMENT_PRODUCT_CLASSES))
        self.assertNotIn("product_classification", pipeline._CATEGORY_ATTRIBUTES_EXTRACTION_SPECS["美容機器"])

    def test_stage1_and_discovery_prompts(self):
        self.assertIn("商品区分", pipeline.build_stage1_prompt("B", "N", category="サプリメント"))
        self.assertIn("医薬部外品は候補にしない", pipeline.build_discovery_prompt("サプリメント", "vitamin_c"))
        for category in ("美容機器", "美容液", None):
            with self.subTest(category=category):
                self.assertNotIn("商品区分", pipeline.build_stage1_prompt("B", "N", category=category))
        self.assertNotIn("医薬部外品", pipeline.build_discovery_prompt("美容機器", "RF"))


class DiagnosisCandidateTests(unittest.TestCase):

    def _product(self, product_class):
        attrs = {"primary_ingredient_tags": ["l_cysteine"]}
        if product_class:
            attrs["product_classification"] = product_class
        return {"name": "テスト", "active_ingredients": ["L-システイン"], "category_attributes": attrs}

    def test_drug_and_quasi_drug_are_not_relevant(self):
        for product_class in ("drug", "quasi_drug"):
            self.assertFalse(app.is_candidate_relevant_to_target("サプリメント", "l_cysteine", self._product(product_class)))
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "l_cysteine", self._product("supplement")))
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "l_cysteine", self._product(None)))

    def test_live_title_with_drug_class_is_not_a_candidate(self):
        self.assertIsNone(app.live_supplement_title_primary_target("【第3類医薬品】Lシステイン配合 ビタミンC 240錠"))
        self.assertIsNone(app.live_supplement_title_primary_target("【医薬部外品】ビタミンC 薬用"))
        self.assertEqual(app.live_supplement_title_primary_target("ビタミンC サプリ 60日分"), "vitamin_c")

    def test_device_and_cosmetics_unchanged(self):
        self.assertTrue(app.is_candidate_relevant_to_target(
            "美容機器", "RF", {"category_attributes": {"method": "RF", "product_classification": "drug"}}))
        product = {"brand": "B", "name": "テスト美容液", "category": "美容液", "active_ingredients": ["vitamin_c"],
                   "category_attributes": {"product_classification": "quasi_drug"}}
        reasons = []
        score = app.score_product(dict(product), {"category": "美容液", "purpose": "", "ingredient_focus": "vitamin_c"},
                                  app._EFFECTIVE_CANDIDATE_NEUTRAL_USER_DATA, app._EFFECTIVE_CANDIDATE_BUDGET_VALUE,
                                  reasons=reasons)
        self.assertEqual(app.is_candidate_relevant_to_target("美容液", "vitamin_c", product),
                         app._is_relevant_scored_candidate(score, reasons, "vitamin_c"))


class GateOrderTests(OrchestratorTestBase):

    def _insert(self, label, stage1_text, product_class=None, category="サプリメント"):
        brand, name = f"BR{label}{TEST_NAME_SUFFIX}", f"商品{label}{TEST_NAME_SUFFIX}"
        payload = _payload(product_class)
        payload["jan_code"] = "4987000000000"
        self._insert_staging_row(self._new_batch_id(f"s481-{label}"), brand, name, category,
                                 stage2_payload=payload, citations=[{"uri": CIT + label, "title": "cosme.net"}],
                                 stage1_raw_text=stage1_text)
        return brand, name, self._staging_id(brand)

    def _staging_id(self, brand):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            return cur.fetchone()[0]
        finally:
            conn.close()

    def _execute(self, brand, name, sid):
        budget = orchestrator.BatchBudget(self._new_batch_id("s481-exec"), 5, 20, 0.50)
        forbidden = AssertionError("区分ゲート後に進まないはず")
        with patch.object(pipeline, "resolve_item_code_for_product", side_effect=forbidden), \
             patch.object(pipeline.citation_verification, "fetch_html", side_effect=forbidden), \
             patch.object(pipeline, "reflect_staging_to_product_master", side_effect=forbidden), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=forbidden):
            return orchestrator.process_coverage_gap_item(
                {"category": "サプリメント", "target": "l_cysteine", "shortage_count": 1}, "execute", budget.batch_id,
                budget, lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                          "discovery_source": "staging_reuse", "staging_id": sid}], 3, set())

    def test_drug_stops_before_official_http_and_rakuten(self):
        brand, name, sid = self._insert("Drug", "本製品はサプリメントではなく、第3類医薬品となります。", "supplement")
        actions = self._execute(brand, name, sid)
        self.assertEqual(actions[0]["action"], "not_reflected")
        self.assertEqual(actions[0]["reason"], "supplement_not_eligible")
        self.assertEqual(actions[0]["product_classification"], "drug")
        self.assertEqual(actions[0]["gate_failure_marker"]["gate"], "supplement_eligibility")

    def test_unknown_class_is_not_reflected(self):
        brand, name, sid = self._insert("Unknown", "L-システイン配合。1日2錠。", None)
        actions = self._execute(brand, name, sid)
        self.assertEqual((actions[0]["reason"], actions[0]["product_classification"]),
                         ("supplement_not_eligible", "unknown"))

    def test_allowed_class_proceeds_to_rakuten_precheck(self):
        brand, name, sid = self._insert("Allowed", "機能性表示食品。L-システイン配合。", "foods_with_function_claims")
        with patch.object(pipeline, "is_official_source_confirmed", return_value=True), \
             patch.object(pipeline, "resolve_item_code_for_product",
                          return_value={"status": "not_found", "reason": "no_candidates"}) as resolver:
            budget = orchestrator.BatchBudget(self._new_batch_id("s481-ok"), 5, 20, 0.50)
            actions = orchestrator.process_coverage_gap_item(
                {"category": "サプリメント", "target": "l_cysteine", "shortage_count": 1}, "execute", budget.batch_id,
                budget, lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                          "discovery_source": "staging_reuse", "staging_id": sid}], 3, set())
        resolver.assert_called_once()
        self.assertEqual(actions[0]["rakuten_reason"], "no_candidates")

    def test_reflect_itself_refuses_drug(self):
        brand, name, sid = self._insert("Direct", "【第2類医薬品】", "supplement")
        result = pipeline.reflect_staging_to_product_master(sid, dry_run=False)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "supplement_not_eligible"))
        self.assertIsNone(self._product_master_row(brand, name, "サプリメント"))

    def test_reflected_supplement_keeps_classification(self):
        brand, name, sid = self._insert("Keep", "栄養機能食品(L-システイン)。", "nutrient_function_food")
        result = pipeline.reflect_staging_to_product_master(sid, dry_run=True)
        self.assertEqual(result["product"]["category_attributes"]["product_classification"], "nutrient_function_food")

    def test_device_is_not_gated_by_classification(self):
        brand, name, sid = self._insert("Dev", "第3類医薬品", None, category="美容機器")
        evaluation = orchestrator.evaluate_staging_for_reflect(sid, brand, name, "美容機器")
        self.assertNotEqual(evaluation["reason"], "supplement_not_eligible")
        self.assertNotIn("product_classification", evaluation)

    def test_collect_skips_page_verification_for_regulated_text(self):
        stage1 = {"status": "ok", "raw_text": "第3類医薬品です。", "citations": [{"uri": CIT, "title": "x"}],
                  "search_queries": ["q"]}
        with patch.object(pipeline, "run_stage1_collection", return_value=stage1), \
             patch.object(pipeline, "attach_citation_page_verification", side_effect=AssertionError("no http")), \
             patch.object(pipeline, "run_stage2_structuring", return_value={"status": "ok", "payload": _payload()}), \
             patch.object(pipeline, "write_staging_record", return_value=1):
            self.assertEqual(pipeline.collect_one_product("B", "N", "サプリメント", "b")["staging_id"], 1)
        with patch.object(pipeline, "run_stage1_collection", return_value=stage1), \
             patch.object(pipeline, "attach_citation_page_verification", return_value=[]) as attach, \
             patch.object(pipeline, "run_stage2_structuring", return_value={"status": "ok", "payload": {}}), \
             patch.object(pipeline, "write_staging_record", return_value=1):
            pipeline.collect_one_product("B", "N", "美容機器", "b")
        attach.assert_called_once()


if __name__ == "__main__":
    unittest.main()
