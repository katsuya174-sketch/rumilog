"""Step50: 承認済みブランド別表記・メーカー対応(brand_aliases.json)による公式情報源の照合と、
403/429の「確認保留」扱いのテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s50_"

DEAR = {"id": "dear-natura", "status": "approved", "names": ["ディアナチュラ", "Dear-Natura"], "makers": ["アサヒグループ食品"]}
IMUSE = {"id": "imuse", "status": "approved", "names": ["iMUSE", "イミューズ"], "makers": ["キリン"]}
PANA = {"id": "panasonic", "status": "approved", "names": ["パナソニック", "Panasonic"], "makers": []}


def _cit(key, domain, identity=None, fetch="ok"):
    c = {"uri": CIT + key, "title": domain}
    if identity is not None or fetch != "ok":
        c["page_verification"] = (
            {"fetch_status": "ok", "status": "not_confirmed", "final_domain": domain, "canonical_domain": domain,
             "site_identity": {"og_site_name": identity, "jsonld_names": [], "title_site_name": None}}
            if fetch == "ok" else {"fetch_status": "unverifiable", "status": "unverifiable", "reason": fetch})
    return c


def _payload(*source_keys):
    return {"active_ingredients": [{"ingredient": "亜鉛", "source_url": CIT + k} for k in source_keys]}


class OfficialConfirmationTests(unittest.TestCase):

    def confirm(self, brand, cits, payload, entries):
        return pipeline.official_confirmation(brand, cits, payload, entries=entries)

    def test_approved_alias_confirms_via_existing_domain_rule_with_spec_source(self):
        cits = [_cit("d", "dear-natura.com", fetch="http_403"), _cit("y", "yahoo.co.jp", "Yahoo!ショッピング")]
        result = self.confirm("ディアナチュラ（アサヒグループ食品）", cits, _payload("d"), [DEAR])
        self.assertEqual((result["confirmed"], result["basis"], result["domain"]), (True, "alias", "dear-natura.com"))

    def test_alias_alone_does_not_confirm_the_product(self):
        # 公式ドメインが商品の仕様の出典になっていなければ不合格(対応関係だけで商品を確認済みにしない)。
        cits = [_cit("d", "dear-natura.com", fetch="http_403"), _cit("y", "yahoo.co.jp", "Yahoo!ショッピング")]
        self.assertFalse(self.confirm("ディアナチュラ", cits, _payload("y"), [DEAR])["confirmed"])
        self.assertFalse(self.confirm("ディアナチュラ", cits, {}, [DEAR])["confirmed"])

    def test_maker_identity_confirms_registered_brand(self):
        cits = [_cit("k", "kirin.co.jp", "キリン")]
        result = self.confirm("iMUSE（イミューズ）", cits, _payload("k"), [IMUSE])
        self.assertEqual((result["confirmed"], result["basis"], result["matched_name"]), (True, "maker", "キリン"))

    def test_registered_notation_variant(self):
        cits = [_cit("p", "panasonic.jp", "Panasonic")]
        self.assertFalse(pipeline.is_official_source_confirmed("パナソニック", cits))  # 既存の判定は不変
        self.assertTrue(self.confirm("パナソニック", cits, _payload("p"), [PANA])["confirmed"])

    def test_unregistered_or_unapproved_brand_is_unchanged(self):
        cits = [_cit("k", "kirin.co.jp", "キリン")]
        self.assertFalse(self.confirm("iMUSE（イミューズ）", cits, _payload("k"), [])["confirmed"])
        self.assertFalse(self.confirm("別ブランド", cits, _payload("k"), [IMUSE])["confirmed"])

    def test_retail_site_never_confirms_via_registry(self):
        cits = [_cit("y", "yahoo.co.jp", "Yahoo!ショッピング"), _cit("r", "rakuten.co.jp", "楽天市場")]
        self.assertFalse(self.confirm("iMUSE", cits, _payload("y", "r"), [IMUSE])["confirmed"])

    def test_domain_mismatch_is_still_rejected(self):
        bad = _cit("k", "kirin.co.jp", "キリン")
        bad["page_verification"]["final_domain"] = "evil.example.com"
        self.assertFalse(self.confirm("iMUSE", [bad], _payload("k"), [IMUSE])["confirmed"])

    def test_ambiguous_registry_match_is_not_used(self):
        other = {"id": "x", "status": "approved", "names": ["イミューズ"], "makers": ["別会社"]}
        self.assertIsNone(pipeline.brand_alias_entry("iMUSE（イミューズ）", [IMUSE, other]))

    def test_direct_confirmation_is_unchanged(self):
        cits = [_cit("d", "dhc.co.jp")]
        self.assertEqual(self.confirm("DHC", cits, {}, [])["basis"], "direct")


class RegistryFileTests(unittest.TestCase):

    def test_only_approved_entries_are_loaded(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"entries": [dict(IMUSE, status="proposed"), DEAR]}, f, ensure_ascii=False)
            path = f.name
        try:
            self.assertEqual([e["id"] for e in pipeline.load_approved_brand_aliases(path)], ["dear-natura"])
        finally:
            os.unlink(path)

    def test_repository_approved_entries_have_verified_evidence(self):
        # 有効化された項目はすべて、取得できた公式ページのURLと確認内容を持つ。
        # 例外: dear-natura は公式ページが403で内容を確認できないが、2026-10-16にユーザーの判断で承認した
        # (公式ドメインのドメイン名による確認)。承認者・承認日時・経緯の記載を必須とする。
        with open(pipeline.BRAND_ALIASES_PATH, encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        approved = [e for e in entries if e["status"] == "approved"]
        self.assertEqual(sorted(e["id"] for e in approved),
                         ["dear-natura", "imuse", "morinaga-bifidus", "nippi-collagen", "panasonic"])
        dear = next(e for e in approved if e["id"] == "dear-natura")
        self.assertTrue(dear["approved_by"].startswith("user") and dear["approved_at"] and "ユーザーの判断で承認" in dear["evidence_note"])
        for e in [e for e in approved if e["id"] != "dear-natura"]:
            with self.subTest(entry=e["id"]):
                ok_pages = [p for p in e["evidence"] if p["status"] == "ok" and p["final_url"].startswith("https://")]
                self.assertTrue(ok_pages)
                self.assertTrue(e["evidence_note"] and e["approved_at"])
                self.assertTrue(e["alias_relations"] or e["maker_relations"])


class PendingTests(unittest.TestCase):

    def test_403_and_429_are_pending_not_failed(self):
        for reason in ("http_403", "http_429", "timeout"):
            self.assertTrue(pipeline.is_transient_page_verification({"fetch_status": "unverifiable", "reason": reason}))
        self.assertFalse(pipeline.is_transient_page_verification({"fetch_status": "unverifiable", "reason": "http_404"}))
        result = orchestrator.classify_official_evidence("ブランド", [_cit("d", "brand.example.com", fetch="http_403")])
        self.assertEqual(result["status"], "undetermined")


class GateIntegrationTests(OrchestratorTestBase):

    def _insert(self, brand, cits, payload):
        name = f"亜鉛{TEST_NAME_SUFFIX}"
        self._insert_staging_row(self._new_batch_id("s50"), brand, name, "サプリメント", stage2_payload=payload,
                                 citations=cits, stage1_raw_text="亜鉛のサプリメント。1日1粒を目安に。")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            return name, cur.fetchone()[0]
        finally:
            conn.close()

    def test_evaluation_uses_registry_and_records_basis(self):
        brand = f"iMUSE{TEST_NAME_SUFFIX}（イミューズ）"
        entry = dict(IMUSE, names=[f"iMUSE{TEST_NAME_SUFFIX}"])
        payload = dict(_payload("k"), category_attributes={"product_classification": {
            "value": "supplement", "confidence": "high", "source_url": CIT + "k"}})
        name, sid = self._insert(brand, [_cit("k", "kirin.co.jp", "キリン")], payload)
        with patch.object(pipeline, "load_approved_brand_aliases", return_value=[]):
            self.assertEqual(orchestrator.evaluate_staging_for_reflect(sid, brand, name, "サプリメント")["reason"],
                             "official_source_not_confirmed_variant_uncertain")
        with patch.object(pipeline, "load_approved_brand_aliases", return_value=[entry]):
            evaluation = orchestrator.evaluate_staging_for_reflect(sid, brand, name, "サプリメント")
        self.assertEqual((evaluation["official_source_confirmed"], evaluation["official_basis"]), (True, "maker"))

    def test_pending_label_on_403(self):
        brand = f"保留{TEST_NAME_SUFFIX}"
        name, sid = self._insert(brand, [_cit("p", "brand.example.com", fetch="http_403")], _payload("p"))
        budget = orchestrator.BatchBudget(self._new_batch_id("s50-exec"), 5, 20, 0.50)
        with patch.object(pipeline.citation_verification, "fetch_html", side_effect=AssertionError("no http")), \
             patch.object(pipeline, "resolve_item_code_for_product", side_effect=AssertionError("no rakuten")):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "サプリメント", "target": "zinc", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c, "discovery_source": "staging_reuse",
                                  "staging_id": sid}], 3, set())
        self.assertEqual(actions[0]["official_check"], "pending")
        self.assertNotIn("gate_failure_marker", actions[0])


if __name__ == "__main__":
    unittest.main()
