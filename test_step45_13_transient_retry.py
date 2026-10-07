"""Step45.13: transient citationの再確認と、staging再利用の候補枠の詰まり解消のテスト。

実HTTP・実Gemini・実楽天・本番DBは使わない。
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import citation_verification as cv  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s4513_"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
FORBIDDEN = AssertionError("呼ばれないはず")


def _transient(at=None, retry_count=0, exhausted=False):
    record = {"fetch_status": "unverifiable", "status": "unverifiable", "reason": "timeout", "final_domain": None}
    if at:
        record["checked_at"] = at.isoformat()
    if retry_count:
        record.update({"retry_count": retry_count, "last_attempt_at": at.isoformat()})
    if exhausted:
        record["transient_exhausted"] = True
    return record


def _evaluated(domain, og):
    return {"fetch_status": "ok", "status": "not_confirmed", "final_domain": domain, "canonical_domain": domain,
            "checked_at": (NOW - timedelta(days=3)).isoformat(),
            "site_identity": {"og_site_name": og, "jsonld_names": [], "title_site_name": None}}


def _ok_page(domain, og):
    return {"status": cv.OK, "reason": None, "final_url": f"https://{domain}/", "final_domain": domain,
            "html": f'<html><head><meta property="og:site_name" content="{og}"></head></html>'}


class RetryTests(unittest.TestCase):

    def _retry(self, brand, citations, fetch, now=NOW):
        calls = []

        def fake(url):
            calls.append(url)
            return fetch(url) if callable(fetch) else fetch
        with patch.object(cv, "fetch_html", side_effect=fake), patch.object(pipeline, "_utcnow", return_value=now):
            out, changed = pipeline.retry_transient_page_verifications(brand, citations, now=now)
        return out, changed, calls

    def test_only_transient_citations_are_refetched(self):
        cits = [{"uri": CIT + "ok", "title": "cosme.net", "page_verification": _evaluated("www.cosme.net", "@cosme")},
                {"uri": CIT + "to", "title": "shop.example.com", "page_verification": _transient(NOW - timedelta(hours=25))}]
        out, changed, calls = self._retry("ヤーマン", cits, _ok_page("shop.example.com", "Shop"))
        self.assertTrue(changed)
        self.assertEqual(calls, [CIT + "to"])
        self.assertEqual(out[0]["page_verification"], cits[0]["page_verification"])
        pv = out[1]["page_verification"]
        self.assertEqual((pv["status"], pv["retry_count"]), ("not_confirmed", 1))

    def test_no_retry_before_24h(self):
        cits = [{"uri": CIT + "a", "title": "a.example.com", "page_verification": _transient(NOW - timedelta(hours=23))}]
        out, changed, calls = self._retry("ヤーマン", cits, FORBIDDEN)
        self.assertEqual((changed, calls), (False, []))

    def test_legacy_record_without_time_starts_clock_without_fetch(self):
        cits = [{"uri": CIT + "l", "title": "ksdenki.com", "page_verification": _transient()}]
        out, changed, calls = self._retry("パナソニック", cits, FORBIDDEN)
        self.assertTrue(changed)
        self.assertEqual(calls, [])
        self.assertEqual(out[0]["page_verification"]["last_attempt_at"], NOW.isoformat())

    def test_retry_success_transitions_to_confirmed(self):
        cits = [{"uri": CIT + "c", "title": "ya-man.co.jp", "page_verification": _transient(NOW - timedelta(days=2))}]
        out, _, _ = self._retry("ヤーマン", cits, _ok_page("www.ya-man.co.jp", "ヤーマン株式会社"))
        self.assertEqual(out[0]["page_verification"]["status"], "confirmed")
        self.assertTrue(pipeline.is_official_source_confirmed("ヤーマン", out))

    def test_max_two_retries_then_exhausted_not_official_false(self):
        timeout = {"status": cv.UNVERIFIABLE, "reason": "timeout", "final_url": ""}
        cits = [{"uri": CIT + "x", "title": "ksdenki.com", "page_verification": _transient(NOW - timedelta(days=3))},
                {"uri": CIT + "p", "title": "panasonic.jp", "page_verification": _evaluated("panasonic.jp", "Panasonic")}]
        out, _, calls = self._retry("パナソニック", cits, timeout)
        self.assertEqual(out[0]["page_verification"]["retry_count"], 1)
        out, _, calls2 = self._retry("パナソニック", out, timeout, now=NOW + timedelta(hours=25))
        pv = out[0]["page_verification"]
        self.assertEqual((pv["retry_count"], pv.get("transient_exhausted")), (2, True))
        out, changed, calls3 = self._retry("パナソニック", out, FORBIDDEN, now=NOW + timedelta(days=5))
        self.assertEqual((len(calls), len(calls2), calls3, changed), (1, 1, [], False))
        evidence = orchestrator.classify_official_evidence("パナソニック", out)
        self.assertEqual(evidence["status"], "undetermined")  # 非公式(failed)にはしない
        self.assertEqual(evidence["transient_exhausted"], ["ksdenki.com"])

    def test_retry_uses_same_safe_fetch(self):
        # 再確認もStep45.5のfetch_html(SSRF対策付き)を通る: private IPは拒否される。
        cits = [{"uri": "https://internal.example/", "title": "internal.example",
                 "page_verification": _transient(NOW - timedelta(days=2))}]
        with patch.object(cv, "_getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.8", 0))]), \
             patch.object(cv, "_open_url", side_effect=FORBIDDEN), patch.object(pipeline, "_utcnow", return_value=NOW):
            out, _ = pipeline.retry_transient_page_verifications("ヤーマン", cits, now=NOW)
        self.assertEqual(out[0]["page_verification"]["fetch_status"], cv.REJECTED)


class SlotPlanTests(OrchestratorTestBase):

    def _insert(self, label, citations, marker=None):
        brand, name = f"パナソニック{label}{TEST_NAME_SUFFIX}", f"RF機{label}{TEST_NAME_SUFFIX}"
        payload = {"active_ingredients": [], "formulation_features": [],
                   "category_attributes": {"method": {"value": "RF", "confidence": "high", "source_url": citations[0]["uri"]}}}
        self._insert_staging_row(self._new_batch_id(f"s4513-{label}"), brand, name, "美容機器",
                                 stage2_payload=payload, citations=citations)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        if marker:
            orchestrator._record_gate_failure(sid, *marker)
        return name, sid

    def _plan(self):
        budget = orchestrator.BatchBudget(self._new_batch_id("s4513-plan"), 5, 20, 0.50)
        with patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(pipeline, "discover_candidates_via_gemini", side_effect=FORBIDDEN), \
             patch.object(cv, "fetch_html", side_effect=FORBIDDEN):
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "dry_run")
            found = source("美容機器", "RF", 3)
        return found, source.last_report

    def _fixture_99(self):
        return self._insert("99", [{"uri": CIT + "99", "title": "ya-man-tokyo-japan.com"}],
                            marker=("rakuten_sale_listing", "only_non_new_sale_listings"))

    def _fixture_98(self):
        return self._insert("98", [
            {"uri": CIT + "98a", "title": "panasonic.jp", "page_verification": _evaluated("panasonic.jp", "Panasonic")},
            {"uri": CIT + "98b", "title": "ksdenki.com", "page_verification": _transient(NOW - timedelta(hours=2))}])

    def test_pending_97_normal_transient_98_one_slot_99_excluded(self):
        name99, _ = self._fixture_99()
        name98, _ = self._fixture_98()
        name97, _ = self._insert("97", [{"uri": CIT + "97", "title": "panasonic.jp"}])  # 未評価(pending)
        found, report = self._plan()
        states = {c["name"]: c["reuse_state"] for c in found}
        self.assertEqual(states, {name97: "normal", name98: "transient"})
        self.assertEqual(report["external_discovery_requested"], 1)
        self.assertIn((name99, "gate_failure_recent"), [(e["name"], e["reason"]) for e in report["excluded"]])

    def test_two_transients_use_one_slot_and_discovery_gets_two(self):
        name99, _ = self._fixture_99()
        name98, _ = self._fixture_98()
        name97, _ = self._insert("97t", [
            {"uri": CIT + "97a", "title": "biccamera.com", "page_verification": _transient(NOW - timedelta(hours=1))}])
        found, report = self._plan()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["reuse_state"], "transient")
        self.assertEqual(report["external_discovery_requested"], 2)
        deferred = [e["name"] for e in report["excluded"] if e["reason"] == "transient_slot_deferred"]
        self.assertEqual(len(deferred), 1)
        self.assertIn(deferred[0], {name97, name98})

    def test_three_transients_still_leave_two_slots(self):
        for label in ("t1", "t2", "t3"):
            self._insert(label, [{"uri": CIT + label, "title": f"{label}.example.com",
                                  "page_verification": _transient(NOW - timedelta(hours=1))}])
        found, report = self._plan()
        self.assertEqual([c["reuse_state"] for c in found], ["transient"])
        self.assertEqual(report["external_discovery_requested"], 2)
        self.assertEqual(sum(1 for e in report["excluded"] if e["reason"] == "transient_slot_deferred"), 2)


class ExecuteRetryPathTests(OrchestratorTestBase):

    def test_execute_reuse_retries_due_transient_and_persists(self):
        brand, name = f"ヤーマンR{TEST_NAME_SUFFIX}", f"RF機R{TEST_NAME_SUFFIX}"
        cits = [{"uri": CIT + "r", "title": "ya-man.co.jp", "page_verification": _transient(NOW - timedelta(days=2))}]
        payload = {"active_ingredients": [], "formulation_features": [],
                   "category_attributes": {"method": {"value": "RF", "confidence": "high", "source_url": CIT + "r"}}}
        self._insert_staging_row(self._new_batch_id("s4513-exec"), brand, name, "美容機器",
                                 stage2_payload=payload, citations=cits)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        budget = orchestrator.BatchBudget(self._new_batch_id("s4513-exec2"), 5, 20, 0.50)
        with patch.object(pipeline, "_utcnow", return_value=NOW), \
             patch.object(cv, "fetch_html", return_value=_ok_page("www.ya-man.co.jp", f"ヤーマンR{TEST_NAME_SUFFIX}株式会社")), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN), \
             patch.object(pipeline, "resolve_item_code_for_product", return_value={"status": "not_found", "initial_candidate_count": 0}):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                  "discovery_source": "staging_reuse", "staging_id": sid}], 3, set(),
            )
        # 再確認で公式確認でき、次のゲート(楽天確認)まで進む。
        self.assertEqual(actions[0]["reason"], "rakuten_check_unavailable")
        saved = orchestrator._fetch_staging_row(sid)["stage1_citations"][0]["page_verification"]
        self.assertEqual((saved["status"], saved["retry_count"]), ("confirmed", 1))


if __name__ == "__main__":
    unittest.main()
