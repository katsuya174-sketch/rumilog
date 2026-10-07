"""Step45.12: official gate failureの確定/未確定判定(citationごとの分類)のテスト。

実HTTP・実Gemini・実楽天・本番DBは使わない。
"""

import os
import unittest
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s4512_"


def _pv(final_domain, og=None, fetch_status="ok", reason=None, title_site_name=None):
    record = {"fetch_status": fetch_status, "final_domain": final_domain, "reason": reason}
    if fetch_status == "ok":
        record.update({"canonical_domain": final_domain, "status": "not_confirmed",
                       "site_identity": {"og_site_name": og, "jsonld_names": [], "title_site_name": title_site_name}})
    else:
        record["status"] = fetch_status
    return record


def _cit(i, title, pv=None):
    c = {"uri": f"{CIT}{i}", "title": title}
    if pv is not None:
        c["page_verification"] = pv
    return c


# 本番staging #98に保存されているpage verificationと同じ形(Step45.10判定で全てFalse)。
CITATIONS_98 = [
    _cit(1, "panasonic.jp", _pv("panasonic.jp", og="Panasonic")),
    _cit(2, "cosme.net", _pv("www.cosme.net")),
    _cit(3, "ksdenki.com", _pv(None, fetch_status="unverifiable", reason="timeout")),
    _cit(4, "kakaku.com", dict(_pv("kakaku.com", og="価格.com",
                                   title_site_name="パナソニック バイタリフト RF EH-SR85 スペック・仕様"), status="confirmed")),
]


class ClassificationTests(unittest.TestCase):

    def classify(self, brand, citations):
        return orchestrator.classify_official_evidence(brand, citations)

    def test_all_not_confirmed_is_failed(self):
        result = self.classify("ヤーマン", [_cit(1, "cosme.net", _pv("www.cosme.net", og="@cosme")),
                                           _cit(2, "kakaku.com", _pv("kakaku.com", og="価格.com"))])
        self.assertEqual(result["status"], "failed")

    def test_confirmed_with_timeout_is_confirmed(self):
        result = self.classify("ヤーマン", [_cit(1, "ksdenki.com", _pv(None, fetch_status="unverifiable", reason="timeout")),
                                           _cit(2, "ya-man.co.jp", _pv("www.ya-man.co.jp", og="ヤーマン株式会社"))])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["confirmed"], ["ya-man.co.jp"])

    def test_not_confirmed_with_transient_is_undetermined(self):
        for reason in ("timeout", "connection_error", "http_500", "http_503"):
            with self.subTest(reason=reason):
                result = self.classify("ヤーマン", [_cit(1, "cosme.net", _pv("www.cosme.net")),
                                                   _cit(2, "shop.example.com", _pv(None, fetch_status="unverifiable", reason=reason))])
                self.assertEqual(result["status"], "undetermined")
                self.assertEqual(result["transient_unverified"], ["shop.example.com"])

    def test_transient_only_is_undetermined(self):
        result = self.classify("ヤーマン", [_cit(1, "a.example.com", _pv(None, fetch_status="unverifiable", reason="timeout"))])
        self.assertEqual(result["status"], "undetermined")

    def test_non_transient_unverifiable_and_rejected_are_determinate(self):
        result = self.classify("ヤーマン", [_cit(1, "a.example.com", _pv(None, fetch_status="unverifiable", reason="http_403")),
                                           _cit(2, "b.example.com", _pv(None, fetch_status="rejected", reason="non_global_ip:10.0.0.1"))])
        self.assertEqual(result["status"], "failed")

    def test_saved_status_is_recomputed_with_step45_10_rules(self):
        # 保存値status="confirmed"(価格.comの旧誤判定)は使わず、現行判定器で再計算する。
        result = self.classify("パナソニック", [CITATIONS_98[3]])
        self.assertEqual((result["status"], result["confirmed"]), ("failed", []))

    def test_98_fixture_is_undetermined(self):
        result = self.classify("パナソニック", CITATIONS_98)
        self.assertEqual(result["status"], "undetermined")
        self.assertEqual(result["confirmed"], [])
        self.assertEqual(result["transient_unverified"], ["ksdenki.com"])
        self.assertEqual(set(result["not_confirmed"]), {"panasonic.jp", "cosme.net", "kakaku.com"})


class MarkerDecisionTests(OrchestratorTestBase):

    def _insert(self, label, citations):
        brand, name = f"パナソニック{label}{TEST_NAME_SUFFIX}", f"RF機{label}{TEST_NAME_SUFFIX}"
        payload = {"active_ingredients": [], "formulation_features": [],
                   "category_attributes": {"method": {"value": "RF", "confidence": "high", "source_url": citations[0]["uri"]}}}
        self._insert_staging_row(self._new_batch_id(f"s4512-{label}"), brand, name, "美容機器",
                                 stage2_payload=payload, citations=citations)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            return brand, name, cur.fetchone()[0]
        finally:
            conn.close()

    def _execute(self, brand, name, sid):
        budget = orchestrator.BatchBudget(self._new_batch_id("s4512-exec"), 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=AssertionError("no gemini")), \
             patch.object(pipeline.citation_verification, "fetch_html", side_effect=AssertionError("no http")), \
             patch.object(pipeline, "resolve_item_code_for_product", side_effect=AssertionError("no rakuten")):
            return orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                  "discovery_source": "staging_reuse", "staging_id": sid}], 3, set(),
            )

    def _marker(self, sid):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT stage2_payload FROM product_collection_staging WHERE staging_id = %s", (sid,))
            return cur.fetchone()[0].get(orchestrator.GATE_FAILURE_MARKER_KEY)
        finally:
            conn.close()

    def test_98_like_staging_gets_no_marker(self):
        brand, name, sid = self._insert("98like", CITATIONS_98)
        actions = self._execute(brand, name, sid)
        self.assertEqual(actions[0]["reason"], "official_source_not_confirmed_variant_uncertain")
        self.assertEqual(actions[0]["official_evidence"]["status"], "undetermined")
        self.assertIsNone(self._marker(sid))

    def test_all_not_confirmed_staging_gets_marker_with_existing_ttl(self):
        cits = [_cit(1, "panasonic.jp", _pv("panasonic.jp", og="Panasonic")),
                _cit(2, "kakaku.com", _pv("kakaku.com", og="価格.com"))]
        brand, name, sid = self._insert("AllNo", cits)
        actions = self._execute(brand, name, sid)
        self.assertEqual(actions[0]["official_evidence"]["status"], "failed")
        marker = self._marker(sid)
        self.assertEqual(marker["gate"], "official_source")
        key = orchestrator.app.make_verified_product_key({"brand": brand, "name": name, "category": "美容機器"})
        self.assertIn(key, orchestrator._gate_failed_identities())
        later = orchestrator._now() + orchestrator.GATE_FAILURE_TTL + timedelta(minutes=1)
        self.assertNotIn(key, orchestrator._gate_failed_identities(now=later))


if __name__ == "__main__":
    unittest.main()
