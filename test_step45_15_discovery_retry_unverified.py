"""Step45.15: official evidenceのunverified分類と、Discoveryのno_search_evidence
再試行のテスト。実HTTP・実Gemini・実楽天・本番DBは使わない。
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
    OrchestratorTestBase, TEST_NAME_SUFFIX, _fake_response, _fake_stage1_response,
)

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s4515_"


def _done(domain, og):
    return {"fetch_status": "ok", "status": "not_confirmed", "final_domain": domain, "canonical_domain": domain,
            "site_identity": {"og_site_name": og, "jsonld_names": [], "title_site_name": None}}


def _cit(i, title, pv=None):
    c = {"uri": f"{CIT}{i}", "title": title}
    if pv is not None:
        c["page_verification"] = pv
    return c


class UnverifiedClassificationTests(unittest.TestCase):

    def classify(self, brand, citations):
        return orchestrator.classify_official_evidence(brand, citations)

    def test_citation_without_page_verification_is_unverified(self):
        result = self.classify("パナソニック", [_cit(1, "panasonic.jp")])
        self.assertEqual((result["status"], result["unverified"], result["not_confirmed"]),
                         ("undetermined", ["panasonic.jp"], []))

    def test_five_checked_and_rest_unchecked_is_not_failure(self):
        cits = [_cit(i, f"site{i}.example.com", _done(f"site{i}.example.com", "Other")) for i in range(5)]
        cits += [_cit(10 + i, "panasonic.jp") for i in range(3)]  # 上限・重複で未取得
        result = self.classify("パナソニック", cits)
        self.assertEqual(result["status"], "undetermined")
        self.assertEqual(len(result["not_confirmed"]), 5)
        self.assertEqual(len(result["unverified"]), 3)

    def test_all_verified_false_is_failure(self):
        cits = [_cit(1, "panasonic.jp", _done("panasonic.jp", "Panasonic")),
                _cit(2, "kakaku.com", _done("kakaku.com", "価格.com"))]
        self.assertEqual(self.classify("パナソニック", cits)["status"], "failed")

    def test_one_confirmed_is_official_even_with_unverified(self):
        cits = [_cit(1, "ya-man.co.jp", _done("www.ya-man.co.jp", "ヤーマン株式会社")), _cit(2, "cosme.net")]
        self.assertEqual(self.classify("ヤーマン", cits)["status"], "confirmed")

    def test_transient_or_unverified_blocks_failure(self):
        transient = {"fetch_status": "unverifiable", "status": "unverifiable", "reason": "timeout"}
        for extra in (_cit(9, "a.example.com", transient), _cit(9, "a.example.com")):
            with self.subTest(extra=extra.get("page_verification")):
                cits = [_cit(1, "kakaku.com", _done("kakaku.com", "価格.com")), extra]
                self.assertEqual(self.classify("パナソニック", cits)["status"], "undetermined")


class UnverifiedMarkerTests(OrchestratorTestBase):

    def _insert_and_execute(self, label, citations):
        brand, name = f"パナソニック{label}{TEST_NAME_SUFFIX}", f"RF機{label}{TEST_NAME_SUFFIX}"
        payload = {"active_ingredients": [], "formulation_features": [],
                   "category_attributes": {"method": {"value": "RF", "confidence": "high", "source_url": citations[0]["uri"]}}}
        self._insert_staging_row(self._new_batch_id(f"s4515-{label}"), brand, name, "美容機器",
                                 stage2_payload=payload, citations=citations)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        budget = orchestrator.BatchBudget(self._new_batch_id("s4515-exec"), 5, 20, 0.50)
        with patch.object(pipeline.citation_verification, "fetch_html", side_effect=AssertionError("no http")), \
             patch.object(pipeline, "resolve_item_code_for_product", side_effect=AssertionError("no rakuten")):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                  "discovery_source": "staging_reuse", "staging_id": sid}], 3, set(),
            )
        row = orchestrator._fetch_staging_row(sid)
        return actions, row["stage2_payload"].get(orchestrator.GATE_FAILURE_MARKER_KEY)

    def test_partially_verified_staging_gets_no_marker(self):
        cits = [_cit(1, "kakaku.com", _done("kakaku.com", "価格.com")), _cit(2, "panasonic.jp")]
        actions, marker = self._insert_and_execute("Partial", cits)
        self.assertEqual(actions[0]["official_evidence"]["status"], "undetermined")
        self.assertEqual(actions[0]["official_evidence"]["unverified"], ["panasonic.jp"])
        self.assertIsNone(marker)

    def test_fully_verified_false_staging_gets_marker(self):
        cits = [_cit(1, "kakaku.com", _done("kakaku.com", "価格.com")),
                _cit(2, "panasonic.jp", _done("panasonic.jp", "Panasonic"))]
        actions, marker = self._insert_and_execute("Full", cits)
        self.assertEqual(actions[0]["official_evidence"]["status"], "failed")
        self.assertEqual(marker["gate"], "official_source")


def _discovery_fake(statuses, candidates_on_ok=None):
    """discover_candidates_via_geminiの差し替え。呼ばれるたびにstatusesの順で
    diagnosticsを埋める。"""
    calls = []

    def fake(category, target, batch_id, max_candidates=3, diagnostics=None):
        status = statuses[len(calls)]
        calls.append(status)
        diagnostics.update({"status": status})
        return list(candidates_on_ok or []) if status == "ok" else []
    return fake, calls


class DiscoveryRetryTests(OrchestratorTestBase):

    def _source(self, fake, max_cost=0.50, estimate=0.01):
        budget = orchestrator.BatchBudget(self._new_batch_id("s4515-disc"), 5, 20, max_cost)
        patches = [patch.object(orchestrator, "_product_master_identity_keys", return_value=set()),
                   patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()),
                   patch.object(orchestrator, "_gate_failed_identities", return_value={}),
                   patch.object(app, "load_products", return_value=[]),
                   patch.object(app, "load_verified_products_cache", return_value=[]),
                   patch.object(orchestrator, "conservative_cost_estimate", return_value=estimate),
                   patch.object(pipeline, "discover_candidates_via_gemini", side_effect=fake)]
        for p in patches:
            p.start()
        try:
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
            found = source("美容機器", "RF", 3)
        finally:
            for p in reversed(patches):
                p.stop()
        return found, source.last_report["external_discovery_diagnostics"], budget

    def test_no_search_evidence_retries_once_and_uses_second_result(self):
        cand = {"brand": f"新規{TEST_NAME_SUFFIX}", "product_name": f"RF機新規{TEST_NAME_SUFFIX}", "source_url": CIT + "n"}
        fake, calls = _discovery_fake(["no_search_evidence", "ok"], [cand])
        found, diag, _ = self._source(fake)
        self.assertEqual(calls, ["no_search_evidence", "ok"])
        self.assertEqual([c["name"] for c in found], [cand["product_name"]])
        self.assertEqual([a["status"] for a in diag["attempts"]], ["no_search_evidence", "ok"])

    def test_two_no_search_evidence_ends_with_max_two_attempts(self):
        fake, calls = _discovery_fake(["no_search_evidence", "no_search_evidence", "ok"])
        found, diag, _ = self._source(fake)
        self.assertEqual((found, len(calls)), ([], 2))
        self.assertEqual(diag["status"], "no_search_evidence")

    def test_other_outcomes_are_not_retried(self):
        for status in ("structured_empty", "all_filtered", "structuring_error", "search_error", "ok"):
            with self.subTest(status=status):
                fake, calls = _discovery_fake([status, "ok"])
                self._source(fake)
                self.assertEqual(calls, [status])

    def test_no_retry_when_budget_preflight_fails(self):
        fake, calls = _discovery_fake(["no_search_evidence", "ok"])
        # 1回目のpreflightは通るが、2回目(使用済み+推定>上限)は通らない状況を作る。
        estimates = iter([0.01, 0.30])
        budget_holder = {}

        def estimate(kind):
            return next(estimates)
        with patch.object(orchestrator, "conservative_cost_estimate", side_effect=estimate):
            budget = orchestrator.BatchBudget(self._new_batch_id("s4515-budget"), 5, 20, 0.25)
            with patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
                 patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
                 patch.object(orchestrator, "_gate_failed_identities", return_value={}), \
                 patch.object(app, "load_products", return_value=[]), \
                 patch.object(app, "load_verified_products_cache", return_value=[]), \
                 patch.object(pipeline, "discover_candidates_via_gemini", side_effect=fake):
                source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
                source("美容機器", "RF", 3)
            budget_holder["b"] = budget
        self.assertEqual(calls, ["no_search_evidence"])
        diag = source.last_report["external_discovery_diagnostics"]
        self.assertEqual([a["status"] for a in diag["attempts"]], ["no_search_evidence", "skipped_budget"])
        self.assertEqual(budget_holder["b"].stopped_reason, "insufficient_budget_for_next")

    def test_retry_usage_is_recorded_per_attempt(self):
        # 実際のrun_discovery_search→record_usage_and_check_limit経路で、2回分の
        # 使用量がbatchへ記録されることを確認する(Geminiはモック)。
        no_evidence = _fake_stage1_response("検索なしの回答", citations=[], queries=[])
        budget = orchestrator.BatchBudget(self._new_batch_id("s4515-usage"), 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", return_value=no_evidence), \
             patch.object(orchestrator, "conservative_cost_estimate", return_value=0.01), \
             patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_gate_failed_identities", return_value={}), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]):
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
            self.assertEqual(source("美容機器", "RF", 3), [])
        calls, cost = budget.usage_snapshot()
        self.assertEqual(calls, 2)
        self.assertGreater(cost, 0)


class DiscoveryPromptTests(unittest.TestCase):

    def test_search_requirement_is_in_all_discovery_prompts(self):
        for category, target in (("美容機器", "RF"), ("サプリメント", "vitamin_c"), ("美容液", "vitamin_c")):
            with self.subTest(category=category):
                prompt = pipeline.build_discovery_prompt(category, target)
                self.assertIn(pipeline.DISCOVERY_SEARCH_REQUIREMENT, prompt)
                self.assertIn("必ず検索を実行し", prompt)


if __name__ == "__main__":
    unittest.main()
