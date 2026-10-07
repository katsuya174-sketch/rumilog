"""Step44.6: DB reuseとCoverageの意味を統一するためのテスト。

- Candidate Discoveryのidentity集合: product_master登録済み=duplicate除外、
  products.json/verified_products_cacheのみ=DB reuse探索対象、
  recently_failed=除外
- coverageはproduct_masterの実効候補だけで数える(既存DBにあるだけでは
  達成扱いにしない/reflect後は増える)
- DB reuse候補もStage1→Stage2→citation→validator→conflict→reflectを迂回しない

Gemini/Rakuten実APIは一切呼ばない(Geminiはモック)。
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
    OrchestratorTestBase, TEST_NAME_SUFFIX, make_mock_call_gemini,
)

CATEGORY, TARGET = "美容液", "vitamin_c"
CITATION = "https://official.example.com/step44-6"


def _db_product(label, ingredients=("vitamin_c",)):
    return {
        "brand": f"既存{label}{TEST_NAME_SUFFIX}", "name": f"商品既存{label}{TEST_NAME_SUFFIX}",
        "category": CATEGORY, "active_ingredients": list(ingredients), "price_ref": 2000,
    }


def _gap(shortage=1):
    return {"type": "coverage_gap", "category": CATEGORY, "target": TARGET,
            "shortage_count": shortage, "priority": "high", "reason": "test"}


class DiscoveryTestBase(OrchestratorTestBase):

    def _discover(self, products=(), verified=(), master_keys=(), failed_keys=(), mode="dry_run"):
        budget = orchestrator.BatchBudget(self._new_batch_id("disc"), 10, 20, 0.50)
        with patch.object(app, "load_products", return_value=list(products)), \
             patch.object(app, "load_verified_products_cache", return_value=list(verified)), \
             patch.object(orchestrator, "_product_master_identity_keys", return_value=set(master_keys)), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set(failed_keys)), \
             patch.object(orchestrator, "_gemini_discovery_candidates", return_value=[]):
            source = orchestrator.make_discovery_candidate_source("b", budget, mode)
            return source(CATEGORY, TARGET, 3), source.last_report

    @staticmethod
    def _key(product):
        return app.make_verified_product_key(product)


class IdentitySetSeparationTests(DiscoveryTestBase):

    def test_products_json_only_product_is_db_reuse_candidate(self):
        product = _db_product("JSON")
        result, _ = self._discover(products=[product])
        self.assertEqual([(c["name"], c["discovery_source"]) for c in result], [(product["name"], "db_reuse")])

    def test_verified_cache_only_product_is_db_reuse_candidate(self):
        product = _db_product("Verified")
        result, _ = self._discover(verified=[product])
        self.assertEqual([(c["name"], c["discovery_source"]) for c in result], [(product["name"], "db_reuse")])

    def test_product_master_registered_identity_is_duplicate(self):
        product = _db_product("Master")
        result, report = self._discover(products=[product], master_keys={self._key(product)})
        self.assertEqual(result, [])
        self.assertEqual([(e["name"], e["reason"]) for e in report["excluded"]], [(product["name"], "duplicate")])

    def test_recently_failed_identity_is_excluded(self):
        product = _db_product("Failed")
        result, report = self._discover(products=[product], failed_keys={self._key(product)})
        self.assertEqual(result, [])
        self.assertEqual([(e["name"], e["reason"]) for e in report["excluded"]], [(product["name"], "recent_failure")])

    def test_db_reuse_candidate_carries_identity_only(self):
        product = _db_product("IdentityOnly", ingredients=("vitamin_c", "既存DB専用成分"))
        result, _ = self._discover(products=[product])
        self.assertEqual(len(result), 1)
        self.assertNotIn("active_ingredients", result[0])
        self.assertNotIn("price_ref", result[0])
        self.assertEqual((result[0]["brand"], result[0]["name"], result[0]["category"]),
                         (product["brand"], product["name"], CATEGORY))

    def test_staging_and_db_reuse_do_not_duplicate_same_identity(self):
        product = _db_product("Both")
        self._insert_staging_row(
            self._new_batch_id("both"), product["brand"], product["name"], CATEGORY,
            stage2_payload={"active_ingredients": [{"ingredient": "アスコルビン酸"}],
                            "official_source_confirmed": True},
            citations=[{"uri": CITATION, "title": product["brand"]}],
        )
        result, report = self._discover(products=[product], verified=[product])
        self.assertEqual([(c["name"], c["discovery_source"]) for c in result], [(product["name"], "staging_reuse")])
        self.assertTrue(report["excluded"])
        self.assertEqual({e["reason"] for e in report["excluded"]}, {"already_selected"})

    def test_product_master_registered_staging_row_is_not_recollected(self):
        product = _db_product("StagingMaster")
        self._insert_staging_row(
            self._new_batch_id("stgmaster"), product["brand"], product["name"], CATEGORY,
            stage2_payload={"active_ingredients": [{"ingredient": "アスコルビン酸"}],
                            "official_source_confirmed": True},
            citations=[{"uri": CITATION, "title": product["brand"]}],
        )
        result, report = self._discover(master_keys={self._key(product)})
        self.assertEqual(result, [])
        self.assertEqual({e["reason"] for e in report["excluded"]}, {"duplicate"})


class CoverageDefinitionTests(unittest.TestCase):

    def test_existing_db_products_do_not_count_toward_coverage(self):
        products = [_db_product(f"Cov{i}") for i in range(3)]
        with patch.object(app, "query_product_master_candidates", return_value=[]):
            count = app.calculate_effective_candidates(CATEGORY, TARGET, db_products=products,
                                                       verified_products=products)
        self.assertEqual(count, 0)

    def test_product_master_rows_count_even_if_also_in_existing_db(self):
        # 以前は既存DBと重複するproduct_master行を数えなかった(DB reuseで
        # reflectしてもcoverageが増えなかった)。
        products = [_db_product(f"Both{i}") for i in range(2)]
        with patch.object(app, "query_product_master_candidates", return_value=[dict(p) for p in products]):
            count = app.calculate_effective_candidates(CATEGORY, TARGET, db_products=products,
                                                       verified_products=[])
        self.assertEqual(count, 2)

    def test_duplicate_product_master_identity_and_extra_counted_once(self):
        product = _db_product("Once")
        with patch.object(app, "query_product_master_candidates", return_value=[dict(product), dict(product)]):
            count = app.calculate_effective_candidates(CATEGORY, TARGET, extra_candidates=[dict(product)])
        self.assertEqual(count, 1)


class DbReuseExecuteTests(OrchestratorTestBase):

    def _run_execute(self, product, stage1, stage2_payload):
        batch_id = self._new_batch_id("dbreuse-exec")
        mock_gemini = make_mock_call_gemini({
            f"{product['brand']} {product['name']}": {"stage1": stage1, "stage2_payload": stage2_payload},
        })
        with patch.object(app, "generate_product_master_work_queue", return_value=[_gap()]), \
             patch.object(app, "load_products", return_value=[product]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(orchestrator, "_gemini_discovery_candidates", return_value=[]), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}), \
             patch.object(pipeline, "collect_one_product", wraps=pipeline.collect_one_product) as spy_collect, \
             patch.object(app, "fetch_rakuten_candidates", side_effect=AssertionError("楽天は呼ばないはず")):
            result = orchestrator.run_batch(mode="execute", batch_id=batch_id)
        return result, spy_collect

    def _master_row(self, product):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT active_ingredients, price_ref FROM product_master WHERE identity_key = %s",
                        (app._normalize_product_master_identity_key(product["brand"], product["name"], CATEGORY),))
            return cur.fetchone()
        finally:
            conn.close()

    def test_db_reuse_goes_through_stage1_stage2_and_increases_coverage_after_reflect(self):
        product = _db_product("E2E", ingredients=("vitamin_c", "既存DB専用成分"))
        product["price_ref"] = 99999
        with patch.object(app, "load_products", return_value=[product]):
            before = app.calculate_effective_candidates(CATEGORY, TARGET)

        result, spy_collect = self._run_execute(
            product,
            stage1={"text": "アスコルビン酸を配合しています。", "citations": [{"uri": CITATION, "title": product["brand"]}]},
            stage2_payload={
                "brand": product["brand"], "product_name": product["name"], "jan_code": "unknown",
                "active_ingredients": [{"ingredient": "アスコルビン酸", "concentration": "unknown",
                                        "confidence": "high", "source_url": CITATION}],
                "formulation_features": [], "official_source_confirmed": True,
            },
        )

        spy_collect.assert_called_once()
        self.assertEqual(spy_collect.call_args.args[:3], (product["brand"], product["name"], CATEGORY))
        self.assertEqual(len([a for a in result["actions"] if a["action"] == "reflected"]), 1)
        actives, price_ref = self._master_row(product)
        # product_masterの中身はStage2の検証結果であり、既存DBの値のコピーではない。
        self.assertIn("アスコルビン酸", actives)
        self.assertNotIn("既存DB専用成分", actives)
        self.assertNotEqual(price_ref, 99999)

        with patch.object(app, "load_products", return_value=[product]):
            after = app.calculate_effective_candidates(CATEGORY, TARGET)
        self.assertEqual(after, before + 1)

    def test_db_reuse_without_citation_is_not_reflected_and_coverage_unchanged(self):
        product = _db_product("NoCitation")
        before = app.calculate_effective_candidates(CATEGORY, TARGET)
        result, spy_collect = self._run_execute(
            product,
            stage1={"text": "詳細不明", "citations": [], "queries": ["q"]},
            stage2_payload={"active_ingredients": [], "formulation_features": [], "official_source_confirmed": False},
        )
        spy_collect.assert_called_once()
        self.assertEqual([a for a in result["actions"] if a["action"] == "reflected"], [])
        self.assertTrue([a for a in result["actions"] if a["action"] == "not_reflected"])
        self.assertIsNone(self._master_row(product))
        self.assertEqual(app.calculate_effective_candidates(CATEGORY, TARGET), before)

    def test_execute_skips_product_master_registered_identity(self):
        product = _db_product("ExecDup")
        with patch.object(orchestrator, "_product_master_identity_keys",
                          return_value={app.make_verified_product_key(product)}):
            result, spy_collect = self._run_execute(product, stage1={}, stage2_payload={})
        spy_collect.assert_not_called()
        self.assertEqual(result["actions"], [])


class DbReuseDryRunTests(OrchestratorTestBase):

    def test_dry_run_shows_would_collect_without_writes_or_api(self):
        product = _db_product("DryRun")
        forbidden = AssertionError("dry-runでは呼ばれないはず")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()

            def counts():
                out = {}
                for t in ("product_master", "product_collection_staging", "product_collection_usage"):
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    out[t] = cur.fetchone()[0]
                return out

            before = counts()
            with patch.object(app, "generate_product_master_work_queue", return_value=[_gap()]), \
                 patch.object(app, "load_products", return_value=[product]), \
                 patch.object(app, "load_verified_products_cache", return_value=[]), \
                 patch.object(pipeline, "call_gemini_for_collection", side_effect=forbidden), \
                 patch.object(pipeline, "discover_candidates_via_gemini", side_effect=forbidden), \
                 patch.object(pipeline, "collect_one_product", side_effect=forbidden), \
                 patch.object(pipeline, "reflect_staging_to_product_master", side_effect=forbidden), \
                 patch.object(app, "fetch_rakuten_candidates", side_effect=forbidden), \
                 patch.object(app, "upsert_product_master", side_effect=forbidden):
                result = orchestrator.run_batch(mode="dry_run", batch_id=self._new_batch_id("dbreuse-dry"))
            self.assertEqual(counts(), before)
        finally:
            conn.close()
        would = [a for a in result["actions"] if a["action"] == "would_collect"]
        self.assertIn(product["name"], [a["name"] for a in would])


if __name__ == "__main__":
    unittest.main()
