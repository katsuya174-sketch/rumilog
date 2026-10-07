"""Step45.17: 美容機器の型番ベース重複防止のテスト。

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

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s4517_"
FORBIDDEN = AssertionError("呼ばれないはず")


class SameDeviceTests(unittest.TestCase):

    def same(self, a, b):
        return pipeline.same_device_by_model(a[0], a[1], b[0], b[1])

    def test_97_and_101_are_same_product(self):
        self.assertEqual(self.same(("パナソニック", "ソニック RF リフト EH-SR75"), ("Panasonic", "ソニック RF リフト EH-SR75")),
                         (True, "same_model_number"))

    def test_model_number_cases(self):
        base = ("Panasonic", "ソニック RF リフト EH-SR75")
        self.assertTrue(self.same(base, ("Panasonic", "ソニック RF リフト EH-SR75"))[0])
        self.assertEqual(self.same(base, ("Panasonic", "バイタリフト RF EH-SR85"))[1], "model_number_differs")
        self.assertEqual(self.same(base, ("Panasonic", "ソニック RF リフト EH-SR750"))[1], "model_number_differs")
        self.assertTrue(self.same(base, ("パナソニック", "ソニックRFリフト EH-SR75-N"))[0])  # 色suffixのみの差
        self.assertTrue(self.same(base, ("パナソニック", "ソニック RF リフト ＥＨ－ＳＲ７５"))[0])  # 表記ゆれ

    def test_same_name_different_model_is_different(self):
        self.assertFalse(self.same(("A", "リフト美顔器 AB-100"), ("A", "リフト美顔器 AB-200"))[0])

    def test_conflicting_series_is_not_merged(self):
        self.assertEqual(self.same(("Panasonic", "ソニック RF リフト EH-SR75"), ("Panasonic", "バイタリフト EH-SR75")),
                         (False, "series_conflict"))

    def test_without_model_number_falls_back(self):
        self.assertEqual(self.same(("ヤーマン", "フォトプラス EX"), ("YA-MAN", "フォトプラス EX")),
                         (False, "no_model_number"))
        self.assertIsNone(pipeline.find_device_duplicate("美容機器", "ヤーマン", "フォトプラス EX",
                                                         [{"brand": "YA-MAN", "name": "フォトプラス EX"}]))

    def test_other_categories_are_unchanged(self):
        entries = [{"brand": "ハルメク", "name": "C35プレミアム"}]
        for category in ("美容液", "サプリメント"):
            with self.subTest(category=category):
                self.assertIsNone(pipeline.find_device_duplicate(category, "別ブランド", "C35プレミアム", entries))


class DuplicateGuardPipelineTests(OrchestratorTestBase):

    def setUp(self):
        super().setUp()
        self.registered_name = f"ソニック RF リフト EH-SR75 {TEST_NAME_SUFFIX}"
        app.upsert_product_master({"brand": "Panasonic", "name": self.registered_name, "category": "美容機器",
                                   "category_attributes": {"method": "RF"}}, data_source="ai_precollected")

    def _source(self, discover=None):
        budget = orchestrator.BatchBudget(self._new_batch_id("s4517"), 5, 20, 0.50)
        patches = [patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()),
                   patch.object(orchestrator, "_gate_failed_identities", return_value={}),
                   patch.object(app, "load_products", return_value=[]),
                   patch.object(app, "load_verified_products_cache", return_value=[]),
                   patch.object(orchestrator, "conservative_cost_estimate", return_value=0.0),
                   patch.object(pipeline, "discover_candidates_via_gemini", side_effect=discover or (lambda *a, **k: []))]
        for p in patches:
            p.start()
        try:
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
            found = source("美容機器", "RF", 3)
        finally:
            for p in reversed(patches):
                p.stop()
        return found, source.last_report

    def test_staging_reuse_excludes_same_model_with_other_brand_notation(self):
        katakana_name = self.registered_name  # 同じ商品名・ブランド表記違い(#97相当)
        self._insert_staging_row(self._new_batch_id("s4517-97"), "パナソニック", katakana_name, "美容機器",
                                 stage2_payload={"active_ingredients": [], "category_attributes": {
                                     "method": {"value": "RF", "confidence": "high", "source_url": CIT + "97"}}},
                                 citations=[{"uri": CIT + "97", "title": "panasonic.jp"}])
        found, report = self._source()
        self.assertNotIn(katakana_name, [c["name"] for c in found])
        reasons = {(e["discovery_source"], e["reason"]) for e in report["excluded"] if e["name"] == katakana_name}
        self.assertEqual(reasons, {("staging_reuse", "duplicate_model")})

    def test_discovery_candidates_are_excluded_before_stage1(self):
        def discover(category, target, batch_id, max_candidates=3, diagnostics=None):
            diagnostics.update({"status": "ok"})
            return [{"brand": "パナソニック", "product_name": "ソニックRFリフト EH-SR75-N", "source_url": CIT + "d1"},
                    {"brand": "X", "product_name": f"リフト美顔器 XY-300 {TEST_NAME_SUFFIX}", "source_url": CIT + "d2"},
                    {"brand": "Xブランド", "product_name": f"リフト美顔器 XY-300 {TEST_NAME_SUFFIX}", "source_url": CIT + "d3"}]
        found, report = self._source(discover)
        self.assertEqual([c["name"] for c in found], [f"リフト美顔器 XY-300 {TEST_NAME_SUFFIX}"])
        reasons = sorted(e["reason"] for e in report["excluded"] if e["discovery_source"] == "gemini_grounding")
        self.assertEqual(reasons, ["already_selected_model", "duplicate_model"])

    def test_execute_skips_registered_model_before_collect(self):
        budget = orchestrator.BatchBudget(self._new_batch_id("s4517-exec"), 5, 20, 0.50)
        with patch.object(pipeline, "collect_one_product", side_effect=FORBIDDEN), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": "パナソニック", "name": "ソニック RF リフト EH-SR75", "category": c}], 3, set(),
            )
        self.assertEqual(actions[0]["action"], "skipped_duplicate_model")
        self.assertEqual(budget.products_attempted, 0)

    def test_reflect_gate_blocks_duplicate_registered_during_run(self):
        brand, name = "パナソニック", f"ソニック RF リフト EH-SR75 ゲート{TEST_NAME_SUFFIX}"
        self._insert_staging_row(self._new_batch_id("s4517-gate"), brand, name, "美容機器",
                                 stage2_payload={"active_ingredients": [], "formulation_features": [],
                                                 "category_attributes": {"method": {"value": "RF", "confidence": "high",
                                                                                    "source_url": CIT + "g"}}},
                                 citations=[{"uri": CIT + "g", "title": f"パナソニック{TEST_NAME_SUFFIX}"}])
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE product_name = %s", (name,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        registered = orchestrator._registered_device_entries()
        # 1回目(収集前)は未登録、2回目(reflect直前)には登録済み、という状況を再現する。
        side_effect = [[e for e in registered if e["name"] != self.registered_name], registered]
        budget = orchestrator.BatchBudget(self._new_batch_id("s4517-gate2"), 5, 20, 0.50)
        with patch.object(orchestrator, "_registered_device_entries", side_effect=side_effect), \
             patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN), \
             patch.object(pipeline, "resolve_item_code_for_product", side_effect=FORBIDDEN):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                  "discovery_source": "staging_reuse", "staging_id": sid}], 3, set(),
            )
        self.assertEqual(actions[0]["reason"], "duplicate_model_in_product_master")

    def test_cosmetics_identity_behavior_unchanged(self):
        found_dup = pipeline.find_device_duplicate("化粧水", "A", "ローション AB-100", [{"brand": "B", "name": "ローション AB-100"}])
        self.assertIsNone(found_dup)
        guard = orchestrator.DeviceDuplicateGuard("化粧水", [{"brand": "B", "name": "ローション AB-100"}])
        self.assertIsNone(guard.reason("A", "ローション AB-100"))


if __name__ == "__main__":
    unittest.main()
