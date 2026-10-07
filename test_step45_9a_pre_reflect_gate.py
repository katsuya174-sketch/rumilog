"""Step45.9a: reflect前の楽天販売確認と、決定論的gate failureの再利用抑制のテスト。

実Gemini/実楽天/citation HTTPは一切使わない(conftest.pyの安全装置+モック)。
"""

import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s459a_"
FORBIDDEN = AssertionError("呼ばれないはず")
CONFIRMED = {"status": "confirmed", "initial_candidate_count": 2, "title_matched_count": 1,
             "item": {"itemCode": "shop:999", "itemName": "確認済み新品", "shopName": "公式ショップ"}}
RENTAL_ONLY = {"status": "not_found", "reason": "only_non_new_sale_listings", "initial_candidate_count": 2}
SEARCH_UNAVAILABLE = {"status": "not_found", "initial_candidate_count": 0}


class GateTestBase(OrchestratorTestBase):

    def _insert(self, label, category="美容機器", brand_official=True, payload=None, citations=None):
        brand, name = f"BR{label}{TEST_NAME_SUFFIX}", f"機器{label}{TEST_NAME_SUFFIX}"
        citations = citations if citations is not None else [
            {"uri": CIT + label, "title": brand if brand_official else "cosme.net"}]
        if payload is None:
            if category == "美容機器":
                payload = {"active_ingredients": [], "formulation_features": [],
                           "category_attributes": {"method": {"value": "RF", "confidence": "high",
                                                              "source_url": citations[0]["uri"]}}}
            else:
                payload = {"active_ingredients": [{"ingredient": "アスコルビン酸", "source_url": citations[0]["uri"]}],
                           "formulation_features": [],
                           # Step48.1: 出典付きの商品区分(本文に区分表示あり)。
                           "category_attributes": {"product_classification": {
                               "value": "supplement", "confidence": "high", "source_url": citations[0]["uri"]}}}
        self._insert_staging_row(self._new_batch_id(f"g-{label}"), brand, name, category,
                                 stage2_payload=payload, citations=citations,
                                 stage1_raw_text="ビタミンCのサプリメント。" if category == "サプリメント" else None)
        return brand, name, self._row(brand)[0]

    def _row(self, brand):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id, stage2_payload, stage1_citations FROM product_collection_staging "
                        "WHERE brand = %s ORDER BY staging_id DESC LIMIT 1", (brand,))
            return cur.fetchone()
        finally:
            conn.close()

    def _master_row(self, brand, name, category):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT item_code, last_known_rakuten_link FROM product_master WHERE identity_key = %s",
                        (app._normalize_product_master_identity_key(brand, name, category),))
            return cur.fetchone()
        finally:
            conn.close()

    def _execute(self, cand, category="美容機器", target="RF", extra=()):
        budget = orchestrator.BatchBudget(self._new_batch_id("g-exec"), 5, 20, 0.50)
        patches = [patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN),
                   patch.object(pipeline, "collect_one_product", side_effect=FORBIDDEN),
                   patch.object(pipeline.citation_verification, "fetch_html",
                                return_value={"status": "unverifiable", "reason": "http_403", "final_url": ""}),
                   *extra]
        for p in patches:
            p.start()
        try:
            return orchestrator.process_coverage_gap_item(
                {"category": category, "target": target, "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [cand], 3, set(),
            )
        finally:
            for p in reversed(patches):
                p.stop()

    @staticmethod
    def _cand(brand, name, sid, category="美容機器"):
        return {"brand": brand, "name": name, "category": category,
                "discovery_source": "staging_reuse", "staging_id": sid}


class PreReflectSaleCheckTests(GateTestBase):

    def test_not_found_is_not_reflected_and_marked(self):
        brand, name, sid = self._insert("NoSale")
        actions = self._execute(self._cand(brand, name, sid), extra=[
            patch.object(pipeline, "resolve_item_code_for_product", return_value=RENTAL_ONLY),
            patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN)])
        self.assertEqual(actions[0]["action"], "not_reflected")
        self.assertEqual(actions[0]["reason"], "rakuten_new_listing_not_found")
        self.assertEqual(actions[0]["rakuten_reason"], "only_non_new_sale_listings")
        self.assertIsNone(self._master_row(brand, name, "美容機器"))
        marker = self._row(brand)[1][orchestrator.GATE_FAILURE_MARKER_KEY]
        self.assertEqual((marker["gate"], marker["reason"]), ("rakuten_sale_listing", "only_non_new_sale_listings"))

    def test_confirmed_reflects_and_does_not_search_again(self):
        brand, name, sid = self._insert("Sale", category="サプリメント")
        resolve_calls = []

        def resolve(*a, **k):
            resolve_calls.append(a)
            return CONFIRMED
        verified_item = {"ok": True, "http_status": 200, "item": {
            "itemPrice": 2480, "itemUrl": "https://item.rakuten.co.jp/shop/999/",
            "mediumImageUrls": [{"imageUrl": "https://img.example/999.jpg"}]}}
        with patch.object(app, "fetch_rakuten_item_by_item_code", return_value=verified_item) as by_code:
            actions = self._execute(self._cand(brand, name, sid, "サプリメント"), category="サプリメント",
                                    target="vitamin_c", extra=[
                patch.object(pipeline, "resolve_item_code_for_product", side_effect=resolve),
                patch.object(app, "fetch_rakuten_candidates", side_effect=AssertionError("再検索しないはず"))])
        self.assertEqual(actions[0]["action"], "reflected")
        self.assertEqual(actions[0]["item_code_result"]["status"], "resolved")
        self.assertEqual(len(resolve_calls), 1)
        by_code.assert_called_once_with("shop:999")
        self.assertEqual(self._master_row(brand, name, "サプリメント"),
                         ("shop:999", "https://item.rakuten.co.jp/shop/999/"))

    def test_search_unavailable_is_not_marked_as_product_failure(self):
        brand, name, sid = self._insert("Transient")
        actions = self._execute(self._cand(brand, name, sid), extra=[
            patch.object(pipeline, "resolve_item_code_for_product", return_value=SEARCH_UNAVAILABLE),
            patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN)])
        self.assertEqual(actions[0]["reason"], "rakuten_check_unavailable")
        self.assertNotIn(orchestrator.GATE_FAILURE_MARKER_KEY, self._row(brand)[1])

    def test_cosmetics_keep_reflect_then_item_code_order(self):
        brand, name, sid = self._insert("Cosme", category="美容液")
        with patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}) as verify:
            actions = self._execute(self._cand(brand, name, sid, "美容液"), category="美容液", target="vitamin_c",
                                    extra=[patch.object(pipeline, "resolve_item_code_for_product",
                                                        side_effect=AssertionError("化粧品はreflect前確認しない"))])
        self.assertEqual(actions[0]["action"], "reflected")
        self.assertIsNone(verify.call_args.kwargs.get("resolution"))


class GateFailureMarkerTests(GateTestBase):

    def test_official_failure_is_marked(self):
        brand, name, sid = self._insert("Official", brand_official=False)
        actions = self._execute(self._cand(brand, name, sid))
        self.assertEqual(actions[0]["reason"], "official_source_not_confirmed_variant_uncertain")
        self.assertEqual(self._row(brand)[1][orchestrator.GATE_FAILURE_MARKER_KEY]["gate"], "official_source")

    def test_official_failure_due_to_page_timeout_is_not_marked(self):
        cits = [{"uri": CIT + "to", "title": "cosme.net",
                 "page_verification": {"fetch_status": "unverifiable", "status": "unverifiable", "reason": "timeout"}}]
        brand, name, sid = self._insert("Timeout", brand_official=False, citations=cits)
        actions = self._execute(self._cand(brand, name, sid))
        self.assertEqual(actions[0]["reason"], "official_source_not_confirmed_variant_uncertain")
        self.assertNotIn(orchestrator.GATE_FAILURE_MARKER_KEY, self._row(brand)[1])

    def test_validator_failure_is_marked(self):
        payload = {"active_ingredients": [], "formulation_features": [],
                   "category_attributes": {"method": {"value": "unknown", "confidence": "unknown", "source_url": "unknown"}}}
        brand, name, sid = self._insert("Validator", payload=payload)
        actions = self._execute(self._cand(brand, name, sid))
        self.assertEqual(self._row(brand)[1][orchestrator.GATE_FAILURE_MARKER_KEY]["gate"], "category_validator")
        self.assertNotEqual(actions[0]["action"], "reflected")


class MarkerLifecycleTests(GateTestBase):

    def _mark(self, label, gate="official_source", reason="official_source_not_confirmed_variant_uncertain", **kw):
        brand, name, sid = self._insert(label, **kw)
        orchestrator._record_gate_failure(sid, gate, reason)
        return brand, name, sid, app.make_verified_product_key({"brand": brand, "name": name, "category": "美容機器"})

    def test_marker_excludes_within_ttl_and_expires_after(self):
        brand, name, sid, key = self._mark("Ttl")
        self.assertIn(key, orchestrator._gate_failed_identities())
        later = orchestrator._now() + timedelta(hours=24, minutes=1)
        self.assertNotIn(key, orchestrator._gate_failed_identities(now=later))

    def test_marker_invalidated_when_staging_data_changes(self):
        brand, name, sid, key = self._mark("Changed")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT stage2_payload FROM product_collection_staging WHERE staging_id = %s", (sid,))
            payload = cur.fetchone()[0]
            payload["active_ingredients"] = [{"ingredient": "新しいStage2結果"}]
            cur.execute("UPDATE product_collection_staging SET stage2_payload = %s WHERE staging_id = %s",
                        (json.dumps(payload, ensure_ascii=False), sid))
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn(key, orchestrator._gate_failed_identities())

    def test_newer_staging_for_same_identity_overrides_old_marker(self):
        brand, name, sid, key = self._mark("Newer")
        self._insert_staging_row(self._new_batch_id("g-newer2"), brand, name, "美容機器",
                                 stage2_payload={"active_ingredients": []},
                                 citations=[{"uri": CIT + "newer2", "title": brand}])
        self.assertNotIn(key, orchestrator._gate_failed_identities())

    def test_reuse_and_external_discovery_exclude_marked_identity_with_reason(self):
        brand, name, sid, key = self._mark("Excl", gate="rakuten_sale_listing", reason="only_non_new_sale_listings")
        budget = orchestrator.BatchBudget(self._new_batch_id("g-disc"), 5, 20, 0.50)

        def discover(category, target, batch_id, max_candidates=3, diagnostics=None):
            diagnostics.update({"status": "ok"})
            return [{"brand": brand, "product_name": name, "source_url": CIT + "x"}]
        with patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(pipeline, "discover_candidates_via_gemini", side_effect=discover):
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
            found = source("美容機器", "RF", 3)
        self.assertNotIn(name, [c["name"] for c in found])
        excluded = [e for e in source.last_report["excluded"] if e["name"] == name]
        self.assertEqual({e["discovery_source"] for e in excluded}, {"staging_reuse", "gemini_grounding"})
        self.assertTrue(all(e["reason"] == "gate_failure_recent" and e["gate"] == "rakuten_sale_listing"
                            for e in excluded))


class Rf97to99FixtureTests(GateTestBase):
    """#97/#98(official gate failure)・#99(楽天新品listingなし)を記録した状態を
    テストDBのfixtureで再現し、RFのstaging再利用枠が空いて外部Discoveryへ
    進むことを確認する(本番DBには書かない)。"""

    def test_rf_slots_free_up_for_external_discovery(self):
        marked = []
        for label, gate, reason in (("97", "official_source", "official_source_not_confirmed_variant_uncertain"),
                                    ("98", "official_source", "official_source_not_confirmed_variant_uncertain"),
                                    ("99", "rakuten_sale_listing", "only_non_new_sale_listings")):
            brand, name, sid = self._insert(f"RF{label}")
            orchestrator._record_gate_failure(sid, gate, reason)
            marked.append(name)
        budget = orchestrator.BatchBudget(self._new_batch_id("g-rf"), 5, 20, 0.50)
        with patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(orchestrator, "conservative_cost_estimate", return_value=0.0), \
             patch.object(pipeline, "discover_candidates_via_gemini", return_value=[]) as discover:
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
            found = source("美容機器", "RF", 3)
        self.assertFalse([c for c in found if c["name"] in marked])
        report = source.last_report
        self.assertEqual(report["external_discovery_requested"], 3)
        self.assertTrue(report["external_discovery_executed"])
        discover.assert_called_once()
        reasons = {e["name"]: e["reason"] for e in report["excluded"] if e["name"] in marked}
        self.assertEqual(set(reasons), set(marked))
        self.assertEqual(set(reasons.values()), {"gate_failure_recent"})


if __name__ == "__main__":
    unittest.main()
