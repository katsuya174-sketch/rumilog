"""Step44.5: dry-runを実行計画として完成させるためのテスト。

dry-runでもexecuteと同じCandidate Discoveryのread-only tier(staging再利用・
DB再利用・identity/relevance判定)を実行し、各coverage_gapについて
would_collect / external_discovery_required / excluded_candidatesを返す。
Gemini外部探索・Stage1/2・Rakuten・reflect・DB書き込みは一切行わない。

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
from test_product_master_pipeline_orchestrator import (  # noqa: E402
    OrchestratorTestBase, TEST_NAME_SUFFIX,
)

FORBIDDEN = AssertionError("dry-runでは呼ばれないはず")


def _gap(category, target, shortage=3):
    return {
        "category": category, "target": target, "target_count": 3,
        "effective_count": 3 - shortage, "shortage_count": shortage, "sufficient": shortage == 0,
    }


class DryRunPlanTestBase(OrchestratorTestBase):

    def _run(self, gaps, mode="dry_run", existing_keys=frozenset(), failed_keys=frozenset(),
             db_products=None, extra_patches=()):
        patches = [
            patch.object(app, "get_coverage_report", return_value=gaps),
            patch.object(app, "get_stale_product_master_candidates", return_value=[]),
            patch.object(app, "get_needs_review_staging_items", return_value=[]),
            patch.object(orchestrator, "_product_master_identity_keys", return_value=set(existing_keys)),
            patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set(failed_keys)),
            patch.object(app, "load_products", return_value=list(db_products or [])),
            patch.object(app, "load_verified_products_cache", return_value=[]),
        ]
        if mode == "dry_run":
            patches += [
                patch.object(pipeline, "discover_candidates_via_gemini", side_effect=FORBIDDEN),
                patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN),
                patch.object(pipeline, "collect_one_product", side_effect=FORBIDDEN),
                patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN),
                patch.object(orchestrator, "_resolve_item_code_safely", side_effect=FORBIDDEN),
                patch.object(app, "fetch_rakuten_candidates", side_effect=FORBIDDEN),
                patch.object(app, "fetch_rakuten_item_by_item_code", side_effect=FORBIDDEN),
                patch.object(app, "upsert_product_master", side_effect=FORBIDDEN),
            ]
        patches += list(extra_patches)
        for p in patches:
            p.start()
        try:
            return orchestrator.run_batch(coverage_policy_name="cosmetics", mode=mode,
                                          batch_id=self._new_batch_id("plan"))
        finally:
            for p in reversed(patches):
                p.stop()

    @staticmethod
    def _actions(result, action):
        return [a for a in result["actions"] if a["action"] == action]

    def _insert_vitamin_c_staging(self, brand, name):
        self._insert_staging_row(
            self._new_batch_id("plan-stg"), brand, name, "美容液",
            stage2_payload={"active_ingredients": [{"ingredient": "アスコルビン酸"}],
                            "official_source_confirmed": True},
            citations=[{"uri": "https://official.example.com/plan", "title": brand}],
        )


class DryRunPlanCandidateTests(DryRunPlanTestBase):

    def test_staging_candidate_becomes_would_collect(self):
        brand, name = f"計画ステ{TEST_NAME_SUFFIX}", f"商品計画ステ{TEST_NAME_SUFFIX}"
        self._insert_vitamin_c_staging(brand, name)
        result = self._run([_gap("美容液", "vitamin_c")])

        would = self._actions(result, "would_collect")
        self.assertIn(name, [a["name"] for a in would])
        external = self._actions(result, "external_discovery_required")
        self.assertEqual(len(external), 1)
        self.assertEqual(external[0]["requested_candidates"], 3 - len(would))
        self.assertEqual(external[0]["shortage_after_existing"], 3 - len(would))

    def test_db_candidate_becomes_would_collect(self):
        product = {
            "brand": f"計画DB{TEST_NAME_SUFFIX}", "name": f"商品計画DB{TEST_NAME_SUFFIX}",
            "category": "サプリメント", "active_ingredients": ["亜鉛"],
        }
        result = self._run([_gap("サプリメント", "zinc")], db_products=[product])

        would = self._actions(result, "would_collect")
        self.assertEqual([a["name"] for a in would], [product["name"]])
        self.assertEqual(self._actions(result, "external_discovery_required")[0]["requested_candidates"], 2)

    def test_no_candidates_requires_external_discovery(self):
        result = self._run([_gap("美容機器", "RF")])
        self.assertEqual(self._actions(result, "would_collect"), [])
        external = self._actions(result, "external_discovery_required")
        self.assertEqual(external, [{
            "action": "external_discovery_required", "category": "美容機器", "target": "RF",
            "requested_candidates": 3, "shortage_after_existing": 3,
        }])

    def test_enough_existing_candidates_need_no_external_discovery(self):
        products = [
            {"brand": f"計画充足{i}{TEST_NAME_SUFFIX}", "name": f"商品計画充足{i}{TEST_NAME_SUFFIX}",
             "category": "サプリメント", "active_ingredients": ["亜鉛"]}
            for i in range(3)
        ]
        result = self._run([_gap("サプリメント", "zinc")], db_products=products)
        self.assertEqual(len(self._actions(result, "would_collect")), 3)
        self.assertEqual(self._actions(result, "external_discovery_required"), [])

    def test_duplicate_and_recent_failure_are_reported_as_excluded(self):
        dup = {
            "brand": f"計画重複{TEST_NAME_SUFFIX}", "name": f"商品計画重複{TEST_NAME_SUFFIX}",
            "category": "美容液", "active_ingredients": ["vitamin_c"],
        }
        fail_brand, fail_name = f"計画失敗{TEST_NAME_SUFFIX}", f"商品計画失敗{TEST_NAME_SUFFIX}"
        self._insert_vitamin_c_staging(fail_brand, fail_name)
        fail_key = app.make_verified_product_key({"brand": fail_brand, "name": fail_name, "category": "美容液"})
        result = self._run(
            [_gap("美容液", "vitamin_c")], db_products=[dup],
            existing_keys={app.make_verified_product_key(dup)}, failed_keys={fail_key},
        )

        names = [a["name"] for a in self._actions(result, "would_collect")]
        self.assertNotIn(dup["name"], names)
        self.assertNotIn(fail_name, names)
        excluded = self._actions(result, "excluded_candidates")
        self.assertEqual(len(excluded), 1)
        reasons = {e["name"]: e["reason"] for e in excluded[0]["examples"]}
        self.assertEqual(reasons[dup["name"]], "duplicate")
        self.assertEqual(reasons[fail_name], "recent_failure")
        self.assertGreaterEqual(excluded[0]["by_reason"]["duplicate"], 1)
        self.assertGreaterEqual(excluded[0]["by_reason"]["recent_failure"], 1)

    def test_irrelevant_db_products_are_not_reported_as_excluded(self):
        # 関連性の無い既存商品は除外理由の表示対象にしない(計画のノイズを防ぐ)。
        other = {
            "brand": f"計画無関係{TEST_NAME_SUFFIX}", "name": f"商品計画無関係{TEST_NAME_SUFFIX}",
            "category": "サプリメント", "active_ingredients": ["コラーゲン"],
        }
        result = self._run([_gap("サプリメント", "zinc")], db_products=[other],
                           existing_keys={app.make_verified_product_key(other)})
        self.assertEqual(self._actions(result, "excluded_candidates"), [])


class DryRunPlanSafetyTests(DryRunPlanTestBase):

    def _table_counts(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            counts = {}
            for table in ("product_master", "product_collection_staging",
                          "product_collection_usage", "product_field_sources"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cur.fetchone()[0]
            return counts
        finally:
            conn.close()

    def test_dry_run_plan_has_no_external_calls_or_db_writes(self):
        brand, name = f"計画安全{TEST_NAME_SUFFIX}", f"商品計画安全{TEST_NAME_SUFFIX}"
        self._insert_vitamin_c_staging(brand, name)
        before = self._table_counts()
        result = self._run([_gap("美容液", "vitamin_c"), _gap("美容機器", "LED"), _gap("サプリメント", "omega3")])
        self.assertEqual(self._table_counts(), before)
        self.assertNotIn("coverage_after", result)
        self.assertEqual(len(self._actions(result, "external_discovery_required")), 3)

    def test_injected_candidate_source_gets_no_plan_actions(self):
        # 記録を持たない注入candidate_sourceでは従来通りの出力のみ。
        with patch.object(app, "get_coverage_report", return_value=[_gap("美容液", "vitamin_c")]), \
             patch.object(app, "get_stale_product_master_candidates", return_value=[]), \
             patch.object(app, "get_needs_review_staging_items", return_value=[]):
            result = orchestrator.run_batch(mode="dry_run", candidate_source=lambda c, t, n: [])
        self.assertEqual(result["actions"], [])


class ExecuteRegressionTests(DryRunPlanTestBase):

    def test_execute_still_calls_external_discovery_and_emits_no_plan_actions(self):
        gemini = patch.object(orchestrator, "_gemini_discovery_candidates", return_value=[])
        result = self._run([_gap("美容機器", "EMS")], mode="execute", extra_patches=[gemini])
        self.assertEqual(result["mode"], "execute")
        self.assertEqual(self._actions(result, "external_discovery_required"), [])
        self.assertEqual(self._actions(result, "excluded_candidates"), [])
        self.assertEqual(result["actions"], [])

    def test_candidate_source_report_marks_external_execution_only_in_execute(self):
        for mode, executed in (("dry_run", False), ("execute", True)):
            budget = orchestrator.BatchBudget(self._new_batch_id(f"report-{mode}"), 10, 20, 0.50)
            with patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
                 patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
                 patch.object(app, "load_products", return_value=[]), \
                 patch.object(app, "load_verified_products_cache", return_value=[]), \
                 patch.object(orchestrator, "_gemini_discovery_candidates", return_value=[]) as mock_gemini:
                source = orchestrator.make_discovery_candidate_source("b", budget, mode)
                self.assertEqual(source("美容機器", "RF", 3), [])
            with self.subTest(mode=mode):
                self.assertEqual(source.last_report["external_discovery_requested"], 3)
                self.assertEqual(source.last_report["external_discovery_executed"], executed)
                self.assertEqual(mock_gemini.called, executed)


if __name__ == "__main__":
    unittest.main()
