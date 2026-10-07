"""Step45.3: 公式情報源判定・staging再利用・BatchBudget事前停止のテスト。

Gemini/Rakuten実APIは一切呼ばない。
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

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s453_0001"
FORBIDDEN = AssertionError("Gemini/Stage1/2は呼ばれないはず")


def _cits(*titles):
    return [{"uri": f"{CIT}{i}", "title": t} for i, t in enumerate(titles)]


class OfficialSourceRuleTests(unittest.TestCase):

    def ok(self, brand, *titles):
        return pipeline.is_official_source_confirmed(brand, _cits(*titles))

    def test_japanese_brand_in_citation_title(self):
        self.assertTrue(self.ok("パナソニック", "パナソニック公式サイト - 美容家電"))
        self.assertTrue(self.ok("ヤーマン", "ヤーマン公式オンラインストア"))
        self.assertTrue(self.ok("ヤーマン", "ｾﾞﾛ ヤーマン 公式"))

    def test_ascii_brand_matches_domain_label(self):
        self.assertTrue(self.ok("DHC", "cosme.net", "dhc.co.jp"))
        self.assertTrue(self.ok("ＤＨＣ", "dhc.co.jp"))
        self.assertTrue(self.ok("YA-MAN", "ya-man.co.jp"))
        self.assertTrue(self.ok("ファンケル FANCL", "fancl.co.jp"))

    def test_no_brand_in_citations_is_false(self):
        self.assertFalse(self.ok("パナソニック", "cosme.net", "biccamera.com", "kakaku.com"))
        self.assertFalse(self.ok("DHC", "yahoo.co.jp"))

    def test_japanese_brand_is_not_transliterated_to_domain(self):
        # 固定のブランド→ドメイン対応表は持たない(推測で公式扱いしない)。
        self.assertFalse(self.ok("パナソニック", "panasonic.jp"))
        self.assertFalse(self.ok("ヤーマン", "ya-man.co.jp"))

    def test_similar_brand_names_are_false(self):
        self.assertFalse(self.ok("ヤーマン", "ヤーマンド公式"))
        self.assertFalse(self.ok("DHC", "dhcp.example.com"))
        self.assertFalse(self.ok("DHC", "adhc.jp"))
        self.assertFalse(self.ok("Anua", "anuashop.jp"))

    def test_unrelated_source_and_short_brand_are_false(self):
        self.assertFalse(self.ok("COSRX", "stylekorean.com"))
        self.assertFalse(self.ok("dプログラム", "shiseido.co.jp", "cosme.net"))
        self.assertFalse(pipeline.is_official_source_confirmed("", _cits("dhc.co.jp")))
        self.assertFalse(pipeline.is_official_source_confirmed("DHC", []))


class StagingReuseTests(OrchestratorTestBase):

    def _insert(self, label, citations, category="サプリメント", payload=None, stage2_status="ok"):
        brand, name = f"BR{label}{TEST_NAME_SUFFIX}", f"商品{label}{TEST_NAME_SUFFIX}"
        payload = payload if payload is not None else {
            "active_ingredients": [{"ingredient": "アスコルビン酸",
                                    "source_url": (citations[0].get("uri", "") if citations else "")}],
            "formulation_features": [], "official_source_confirmed": False,
            "category_attributes": {"primary_ingredients": {
                "value": "アスコルビン酸", "confidence": "high",
                "source_url": (citations[0].get("uri", "") if citations else "")}},
        }
        self._insert_staging_row(self._new_batch_id(f"reuse-{label}"), brand, name, category,
                                 stage2_payload=payload, citations=citations, stage2_status=stage2_status)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            return brand, name, cur.fetchone()[0]
        finally:
            conn.close()

    def _execute(self, cand, category="サプリメント", target="vitamin_c"):
        batch_id = self._new_batch_id("reuse-exec")
        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN), \
             patch.object(pipeline, "collect_one_product", side_effect=FORBIDDEN), \
             patch.object(pipeline.citation_verification, "fetch_html",
                          return_value={"status": "unverifiable", "reason": "http_403", "final_url": ""}), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}), patch.object(pipeline, "resolve_item_code_for_product", return_value={"status": "confirmed", "initial_candidate_count": 1, "item": {"itemCode": "test:1", "itemName": "test", "shopName": "test"}}):
            actions = orchestrator.process_coverage_gap_item(
                {"category": category, "target": target, "shortage_count": 1}, "execute", batch_id, budget,
                lambda c, t, n: [cand], 3, set(),
            )
        calls, cost = budget.usage_snapshot()
        return actions, calls, cost

    def test_reuse_candidate_carries_staging_id(self):
        brand, name, sid = self._insert("Id", _cits(f"BR{'Id'}{TEST_NAME_SUFFIX}"))
        with patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]):
            found = orchestrator._staging_reuse_candidates("サプリメント", "vitamin_c", set(), 3)
        match = [c for c in found if c["name"] == name]
        self.assertEqual(match[0]["staging_id"], sid)

    def test_reuse_reflects_from_saved_data_without_gemini(self):
        brand, name, sid = self._insert("Ok", _cits("cosme.net", f"BROk{TEST_NAME_SUFFIX}"))
        cand = {"brand": brand, "name": name, "category": "サプリメント",
                "discovery_source": "staging_reuse", "staging_id": sid}
        actions, calls, cost = self._execute(cand)
        self.assertEqual(actions[0]["action"], "reflected")
        self.assertTrue(actions[0]["reused_staging"])
        self.assertEqual((calls, cost), (0, 0.0))

    def test_reuse_official_failure_is_not_reflected_and_costs_nothing(self):
        brand, name, sid = self._insert("NoOfficial", _cits("cosme.net", "kakaku.com"))
        cand = {"brand": brand, "name": name, "category": "サプリメント",
                "discovery_source": "staging_reuse", "staging_id": sid}
        actions, calls, _ = self._execute(cand)
        self.assertEqual(actions[0]["action"], "not_reflected")
        self.assertEqual(actions[0]["reason"], "official_source_not_confirmed_variant_uncertain")
        self.assertEqual(calls, 0)

    def test_broken_saved_data_stops_without_recollection(self):
        brand, name, sid = self._insert("Broken", [{"title": "no uri"}])
        cand = {"brand": brand, "name": name, "category": "サプリメント",
                "discovery_source": "staging_reuse", "staging_id": sid}
        actions, calls, _ = self._execute(cand)
        self.assertEqual(actions[0]["action"], "staging_needs_recollection")
        self.assertEqual(actions[0]["reason"], "citations_missing_or_broken")
        self.assertEqual(calls, 0)

    def test_stage2_not_ok_staging_is_not_reused_for_reflect(self):
        brand, name, sid = self._insert("NotOk", _cits(f"BRNotOk{TEST_NAME_SUFFIX}"), stage2_status="failed")
        evaluation = orchestrator.evaluate_staging_for_reflect(sid, brand, name, "サプリメント")
        self.assertEqual((evaluation["reflectable"], evaluation["reason"]), (False, "stage_not_ok"))

    def test_dry_run_shows_staging_evaluation_without_writes(self):
        brand, name, sid = self._insert("Dry", _cits("cosme.net"))
        with patch.object(app, "get_coverage_report", return_value=[{
            "category": "サプリメント", "target": "vitamin_c", "target_count": 3,
            "effective_count": 0, "shortage_count": 3, "sufficient": False}]), \
             patch.object(app, "get_stale_product_master_candidates", return_value=[]), \
             patch.object(app, "get_needs_review_staging_items", return_value=[]), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN), \
             patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN):
            result = orchestrator.run_batch(mode="dry_run", batch_id=self._new_batch_id("reuse-dry"))
        would = [a for a in result["actions"] if a["action"] == "would_collect" and a["name"] == name][0]
        self.assertEqual(would["staging_evaluation"]["staging_id"], sid)
        self.assertFalse(would["staging_evaluation"]["reflectable"])
        self.assertFalse(would["staging_evaluation"]["official_source_confirmed"])


class BudgetPreCheckTests(OrchestratorTestBase):

    def _record_cost(self, batch_id, cost, stage="stage1", label="x"):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO product_collection_usage (batch_id, product_label, stage, model, estimated_cost_usd) "
                "VALUES (%s, %s, %s, 'test', %s)", (batch_id, label, stage, cost))
            conn.commit()
        finally:
            conn.close()

    def test_next_candidate_not_started_when_estimate_exceeds_budget(self):
        batch_id = self._new_batch_id("budget-stop")
        self._record_cost(batch_id, 0.19)
        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.25)
        with patch.object(orchestrator, "conservative_cost_estimate", return_value=0.09), \
             patch.object(pipeline, "collect_one_product", side_effect=FORBIDDEN), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", batch_id, budget,
                lambda c, t, n: [{"brand": "B", "name": "N", "category": c}], 3, set(),
            )
        self.assertEqual(actions[0]["action"], "batch_stop")
        self.assertEqual(actions[0]["reason"], "insufficient_budget_for_next")
        self.assertEqual(actions[0]["budget_estimate"]["estimated_next_cost"], 0.09)
        self.assertEqual(budget.usage_snapshot()[0], 1)  # 事前に記録した1行のみ(API呼び出し0)

    def test_within_budget_candidate_starts(self):
        budget = orchestrator.BatchBudget(self._new_batch_id("budget-ok"), 5, 20, 0.25)
        with patch.object(orchestrator, "conservative_cost_estimate", return_value=0.09):
            self.assertTrue(budget.can_afford("product_collection"))

    def test_staging_reuse_has_no_api_cost_estimate(self):
        batch_id = self._new_batch_id("budget-reuse")
        self._record_cost(batch_id, 0.20)
        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.25)
        self.assertEqual(orchestrator.conservative_cost_estimate("staging_reuse"), 0.0)
        self.assertTrue(budget.can_afford("staging_reuse"))

    def test_external_discovery_skipped_when_unaffordable(self):
        batch_id = self._new_batch_id("budget-disc")
        self._record_cost(batch_id, 0.20, stage="discovery_search")
        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.25)
        with patch.object(orchestrator, "conservative_cost_estimate", return_value=0.06), \
             patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(pipeline, "discover_candidates_via_gemini", side_effect=FORBIDDEN):
            source = orchestrator.make_discovery_candidate_source(batch_id, budget, "execute")
            self.assertEqual(source("美容機器", "RF", 3), [])
        self.assertFalse(source.last_report["external_discovery_executed"])
        self.assertEqual(budget.stopped_reason, "insufficient_budget_for_next")

    def test_estimate_uses_observed_maximum_with_margin(self):
        batch_id = self._new_batch_id("budget-hist")
        self._record_cost(batch_id, 4.0, stage="stage1", label="big product")
        self._record_cost(batch_id, 1.0, stage="stage2", label="big product")
        self.assertGreaterEqual(orchestrator.conservative_cost_estimate("product_collection"), 5.0 * 1.25)

    def test_pessimistic_floor_when_history_is_small(self):
        with patch.object(orchestrator.psycopg2, "connect") as mock_connect:
            mock_connect.return_value.cursor.return_value.fetchall.return_value = []
            self.assertEqual(orchestrator.conservative_cost_estimate("product_collection"),
                             orchestrator._pessimistic_cost_estimate("product_collection"))
        self.assertGreater(orchestrator._pessimistic_cost_estimate("product_collection"), 0.1)


if __name__ == "__main__":
    unittest.main()
