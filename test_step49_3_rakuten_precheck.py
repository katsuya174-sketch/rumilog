"""Step49.3: Stage1前の楽天事前確認と、楽天照合での型番注記分離のテスト。

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

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s493_"
FORBIDDEN = AssertionError("呼ばれないはず")


class PrecheckTests(OrchestratorTestBase):

    def _run(self, candidates, precheck_results, category="サプリメント", target="zinc", max_failures=1):
        """precheck_results: 商品名→resolve_item_code_for_product(JANなし)の戻り値。"""
        self.resolved, self.collected = [], []

        def resolve(brand, product_name, category, jan_code=None):
            self.resolved.append(product_name)
            return precheck_results[product_name]

        def collect(brand, name, category, batch_id, existing_product=None, existing_jans=None):
            self.collected.append(name)
            raise StopIteration("Stage1へ進んだ")
        budget = orchestrator.BatchBudget(self._new_batch_id("s493"), 5, 20, 0.50)
        self.budget = budget
        with patch.object(orchestrator, "conservative_cost_estimate", return_value=0.01), \
             patch.object(pipeline, "resolve_item_code_for_product", side_effect=resolve), \
             patch.object(pipeline, "collect_one_product", side_effect=collect), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN), \
             patch.object(pipeline.citation_verification, "fetch_html", side_effect=FORBIDDEN):
            try:
                return orchestrator.process_coverage_gap_item(
                    {"category": category, "target": target, "shortage_count": 1}, "execute", budget.batch_id, budget,
                    lambda c, t, n: [dict(x, category=c) for x in candidates], max_failures, set())
            except StopIteration:
                return None

    def _cand(self, label):
        return {"brand": f"BR{TEST_NAME_SUFFIX}", "name": f"{label}{TEST_NAME_SUFFIX}"}

    def test_deterministic_mismatch_is_excluded_before_stage1(self):
        for reason in ("title_mismatch", "device_model_mismatch", "only_non_new_sale_listings", "only_set_items"):
            with self.subTest(reason=reason):
                a, b = self._cand("A"), self._cand("B")
                self._run([a, b], {a["name"]: {"status": "not_found", "reason": reason, "initial_candidate_count": 4},
                                   b["name"]: {"status": "confirmed", "item": {}, "initial_candidate_count": 1}})
                # 失敗上限1でも、事前確認の除外は失敗に数えずに次の候補(B)のStage1へ進む。
                self.assertEqual(self.collected, [b["name"]])
                self.assertEqual(self.budget.products_attempted, 1)

    def test_excluded_action_is_recorded(self):
        a = self._cand("A")
        actions = self._run([a], {a["name"]: {"status": "not_found", "reason": "device_model_mismatch",
                                              "initial_candidate_count": 2}})
        self.assertEqual(actions[0]["action"], "skipped_rakuten_precheck")
        self.assertEqual(actions[0]["rakuten_reason"], "device_model_mismatch")
        self.assertEqual(self.collected, [])

    def test_uncertain_results_proceed_to_stage1(self):
        for result in ({"status": "not_found", "initial_candidate_count": 0},                 # 検索0件/APIエラー
                       {"status": "not_found", "reason": "title_mismatch", "initial_candidate_count": 0},
                       {"status": "confirmed", "item": {}, "initial_candidate_count": 3},     # 確定一致
                       {"status": "not_found", "reason": "jan_mismatch", "initial_candidate_count": 2}):
            with self.subTest(result=result):
                a = self._cand("U")
                self._run([a], {a["name"]: result})
                self.assertEqual(self.collected, [a["name"]])

    def test_precheck_uses_name_without_model_annotation(self):
        a = {"brand": f"ReFa{TEST_NAME_SUFFIX}", "name": "ReFa CARAT LIFT（リファカラットリフト）（型番：RR-AT-02A 等）"}
        clean = "ReFa CARAT LIFT（リファカラットリフト）"
        self._run([a], {clean: {"status": "not_found", "initial_candidate_count": 0}}, category="美容機器", target="EMS")
        self.assertEqual(self.resolved, [clean])

    def test_cosmetics_have_no_precheck(self):
        a = self._cand("C")
        self._run([a], {}, category="美容液", target="vitamin_c")
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.collected, [a["name"]])


class StagingReuseNameTests(OrchestratorTestBase):

    def test_reuse_has_no_precheck_and_final_check_uses_clean_name_with_jan(self):
        brand, name = f"ReFa{TEST_NAME_SUFFIX}", "ReFa CARAT LIFT（リファカラットリフト）（型番：RR-AT-02A 等）"
        payload = {"jan_code": "4580000000000", "active_ingredients": [], "formulation_features": [],
                   "category_attributes": {"method": {"value": "EMS", "confidence": "high", "source_url": CIT}}}
        self._insert_staging_row(self._new_batch_id("s493-st"), brand, name, "美容機器", stage2_payload=payload,
                                 citations=[{"uri": CIT, "title": brand}], stage1_raw_text="")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        calls = []

        def resolve(brand_, product_name, category, jan_code=None):
            calls.append((product_name, jan_code))
            return {"status": "not_found", "initial_candidate_count": 0}
        budget = orchestrator.BatchBudget(self._new_batch_id("s493-reuse"), 5, 20, 0.50)
        with patch.object(pipeline, "resolve_item_code_for_product", side_effect=resolve), \
             patch.object(pipeline, "is_official_source_confirmed", return_value=True), \
             patch.object(pipeline.citation_verification, "fetch_html", side_effect=FORBIDDEN), \
             patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN):
            orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "EMS", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c, "discovery_source": "staging_reuse",
                                  "staging_id": sid}], 3, set())
        self.assertEqual(calls, [("ReFa CARAT LIFT（リファカラットリフト）", "4580000000000")])


if __name__ == "__main__":
    unittest.main()
