"""Step45.5: citation先ページの「サイトの名乗り」による公式判定のテスト。

実HTTP・実DNSは一切使わない(conftest.pyの安全装置に加え、各テストで
citation_verification._getaddrinfo/_open_url/fetch_htmlをモックする)。
"""

import json
import os
import socket
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402
import urllib3  # noqa: E402

import app  # noqa: E402
import citation_verification as cv  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import (  # noqa: E402
    OrchestratorTestBase, TEST_NAME_SUFFIX, make_mock_call_gemini,
)

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s455_"


# ---------- low-level fakes ----------

def _addrinfo(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", chunk=65536):
        self.status = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self._body, self._chunk = body, chunk
        self.released = False

    def stream(self, amt, decode_content=True):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i:i + self._chunk]

    def release_conn(self):
        self.released = True


def _fetch_with(dns, responses):
    """dns: {host: ip}、responses: {host: FakeResponse} でfetch_htmlを実行する。"""
    calls = []

    def fake_getaddrinfo(host, *a, **k):
        return _addrinfo(dns[host])

    def fake_open(scheme, host, port, path, ip):
        calls.append((scheme, host, port, path, str(ip)))
        return responses[host]

    def run(url):
        with patch.object(cv, "_getaddrinfo", side_effect=fake_getaddrinfo), \
             patch.object(cv, "_open_url", side_effect=fake_open):
            return cv.fetch_html(url)
    return run, calls


def _html(og=None, title=None, jsonld=None, canonical=None, body=""):
    parts = ["<html><head>"]
    if title:
        parts.append(f"<title>{title}</title>")
    if og:
        parts.append(f'<meta property="og:site_name" content="{og}">')
    if canonical:
        parts.append(f'<link rel="canonical" href="{canonical}">')
    if jsonld is not None:
        parts.append(f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>')
    parts.append(f"</head><body>{body}</body></html>")
    return "".join(parts)


class SafeFetchTests(unittest.TestCase):

    def test_connects_to_verified_ip_with_original_host(self):
        run, calls = _fetch_with({"www.ya-man.co.jp": "93.184.215.10"},
                                 {"www.ya-man.co.jp": FakeResponse(body=b"<html></html>")})
        result = run("https://www.ya-man.co.jp/news/1/?a=b")
        self.assertEqual(result["status"], cv.OK)
        self.assertEqual(calls, [("https", "www.ya-man.co.jp", 443, "/news/1/?a=b", "93.184.215.10")])

    def test_non_global_addresses_are_rejected(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.2", "169.254.169.254", "::1", "fe80::1", "0.0.0.0",
                   "100.64.0.1", "203.0.113.9", "224.0.0.1", "fc00::1"):
            with self.subTest(ip=ip):
                run, calls = _fetch_with({"evil.example": ip}, {})
                result = run("https://evil.example/")
                self.assertEqual(result["status"], cv.REJECTED)
                self.assertTrue(result["reason"].startswith("non_global_ip"))
                self.assertEqual(calls, [])

    def test_redirect_to_private_ip_is_rejected_midway(self):
        run, calls = _fetch_with(
            {"start.example": "93.184.215.1", "internal.example": "10.1.2.3"},
            {"start.example": FakeResponse(302, {"Location": "https://internal.example/admin"})},
        )
        result = run("https://start.example/")
        self.assertEqual(result["status"], cv.REJECTED)
        self.assertTrue(result["reason"].startswith("non_global_ip"))
        self.assertEqual([c[1] for c in calls], ["start.example"])

    def test_scheme_port_literal_ip_and_credentials_are_rejected(self):
        run, calls = _fetch_with({}, {})
        for url, reason in (("ftp://a.example/", "scheme_not_allowed:ftp"),
                            ("https://a.example:8443/", "port_not_allowed:8443"),
                            ("http://93.184.215.5/", "ip_literal_host"),
                            ("https://user:pw@a.example/", "credentials_in_url")):
            with self.subTest(url=url):
                self.assertEqual(run(url)["reason"], reason)
        self.assertEqual(calls, [])

    def test_too_many_redirects_are_rejected(self):
        loop = FakeResponse(302, {"Location": "https://loop.example/next"})
        run, calls = _fetch_with({"loop.example": "93.184.215.2"}, {"loop.example": loop})
        result = run("https://loop.example/")
        self.assertEqual((result["status"], result["reason"]), (cv.REJECTED, "too_many_redirects"))
        self.assertEqual(len(calls), cv.MAX_REDIRECTS + 1)

    def test_response_over_1mb_is_rejected(self):
        big = FakeResponse(body=b"a" * (cv.MAX_RESPONSE_BYTES + 10))
        run, _ = _fetch_with({"big.example": "93.184.215.3"}, {"big.example": big})
        self.assertEqual(run("https://big.example/")["reason"], "response_too_large")

    def test_non_html_is_rejected(self):
        pdf = FakeResponse(headers={"Content-Type": "application/pdf"}, body=b"%PDF")
        run, _ = _fetch_with({"doc.example": "93.184.215.4"}, {"doc.example": pdf})
        self.assertEqual(run("https://doc.example/x.pdf")["reason"], "non_html")

    def test_timeout_and_403_are_unverifiable(self):
        run, _ = _fetch_with({"deny.example": "93.184.215.5"}, {"deny.example": FakeResponse(403)})
        self.assertEqual((run("https://deny.example/")["status"]), cv.UNVERIFIABLE)

        def raise_timeout(*a, **k):
            raise urllib3.exceptions.ConnectTimeoutError(None, "https://slow.example/", "timed out")
        with patch.object(cv, "_getaddrinfo", return_value=_addrinfo("93.184.215.6")), \
             patch.object(cv, "_open_url", side_effect=raise_timeout):
            result = cv.fetch_html("https://slow.example/")
        self.assertEqual((result["status"], result["reason"]), (cv.UNVERIFIABLE, "timeout"))

    def test_meta_charset_is_honored(self):
        body = '<html><head><meta charset="shift_jis"><title>テスト | ヤーマン株式会社</title></head></html>'.encode("shift_jis")
        resp = FakeResponse(headers={"Content-Type": "text/html"}, body=body)
        run, _ = _fetch_with({"sjis.example": "93.184.215.7"}, {"sjis.example": resp})
        identity = cv.extract_site_identity(run("https://sjis.example/")["html"])
        self.assertEqual(identity["title_site_name"], "ヤーマン株式会社")


class SiteIdentityExtractionTests(unittest.TestCase):

    def test_only_site_level_fields_are_extracted(self):
        html = _html(
            og="ヤーマン株式会社 企業情報サイト", title="フォトPLUS EX 発売 | ヤーマン株式会社",
            canonical="https://www.ya-man.co.jp/news/2657/",
            jsonld=[{"@type": "WebSite", "name": "ヤーマン株式会社"},
                    {"@type": "Product", "name": "フォトPLUS", "brand": {"@type": "Organization", "name": "別ブランド"}}],
        )
        identity = cv.extract_site_identity(html)
        self.assertEqual(identity["og_site_name"], "ヤーマン株式会社 企業情報サイト")
        self.assertEqual(identity["title_site_name"], "ヤーマン株式会社")
        self.assertEqual(identity["jsonld_names"], ["ヤーマン株式会社"])
        self.assertEqual(identity["canonical"], "https://www.ya-man.co.jp/news/2657/")

    def test_graph_and_title_without_separator(self):
        html = _html(title="単一タイトル", jsonld={"@graph": [{"@type": ["Organization"], "name": "Org名"}]})
        identity = cv.extract_site_identity(html)
        self.assertIsNone(identity["title_site_name"])
        self.assertEqual(identity["jsonld_names"], ["Org名"])


# ---------- pipeline-level official decision ----------

def _ok(final_domain, html, final_url=None):
    return {"status": cv.OK, "reason": None, "final_url": final_url or f"https://{final_domain}/p",
            "final_domain": final_domain, "html": html}


class PageVerificationDecisionTests(unittest.TestCase):

    def _confirm(self, brand, citations, responses):
        """responses: citationのuri→fetch結果。fetchされたuriの一覧も返す。"""
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return responses[url]
        with patch.object(cv, "fetch_html", side_effect=fake_fetch):
            verified = pipeline.attach_citation_page_verification(brand, citations)
        return pipeline.is_official_source_confirmed(brand, verified), verified, fetched

    def test_yaman_site_identity_confirms(self):
        cits = [{"uri": REDIRECT + "1", "title": "ya-man.co.jp"}]
        ok, verified, _ = self._confirm("ヤーマン", cits, {REDIRECT + "1": _ok(
            "www.ya-man.co.jp", _html(og="ヤーマン株式会社 企業情報サイト", title="新製品 | ヤーマン株式会社",
                                      canonical="https://www.ya-man.co.jp/news/1/"))})
        self.assertTrue(ok)
        pv = verified[0]["page_verification"]
        self.assertEqual((pv["status"], pv["final_domain"], pv["canonical_domain"]),
                         ("confirmed", "www.ya-man.co.jp", "www.ya-man.co.jp"))
        self.assertNotIn("html", json.dumps(pv))

    def test_panasonic_ascii_only_identity_does_not_confirm_katakana_brand(self):
        cits = [{"uri": REDIRECT + "2", "title": "panasonic.jp"}]
        ok, verified, _ = self._confirm("パナソニック", cits, {REDIRECT + "2": _ok(
            "panasonic.jp", _html(og="Panasonic", title="EH-SR75 | 美顔器 | Panasonic",
                                  canonical="https://panasonic.jp/face/products/EH-SR75.html",
                                  body="パナソニック株式会社"))})
        self.assertFalse(ok)
        self.assertEqual(verified[0]["page_verification"]["status"], "not_confirmed")

    def test_retail_product_brand_body_and_title_product_part_do_not_confirm(self):
        cases = {
            "product_brand": _html(og="Yahoo!ショッピング", title="ヤーマン 美顔器 - Yahoo!ショッピング",
                                   jsonld={"@type": "Product", "name": "美顔器", "brand": "ヤーマン"}),
            "body_only": _html(og="flarii", title="レビュー | flarii", body="ヤーマン公式 ヤーマン"),
            "title_product_part": _html(title="ヤーマン「フォトプラス」徹底解説 | flarii"),
            "koushiki_only": _html(og="公式オンラインストア", title="商品 | 公式"),
        }
        for label, html in cases.items():
            with self.subTest(case=label):
                cits = [{"uri": REDIRECT + label, "title": "shop.example.com"}]
                ok, _, _ = self._confirm("ヤーマン", cits, {REDIRECT + label: _ok("shop.example.com", html)})
                self.assertFalse(ok)

    def test_final_domain_mismatch_is_rejected(self):
        cits = [{"uri": REDIRECT + "m", "title": "ya-man.co.jp"}]
        ok, verified, _ = self._confirm("ヤーマン", cits, {REDIRECT + "m": _ok(
            "ya-man.co.jp.evil.example", _html(og="ヤーマン株式会社"))})
        self.assertFalse(ok)
        self.assertEqual(verified[0]["page_verification"]["reason"], "final_domain_mismatch")

    def test_canonical_other_domain_is_rejected(self):
        cits = [{"uri": REDIRECT + "c", "title": "ya-man.co.jp"}]
        ok, verified, _ = self._confirm("ヤーマン", cits, {REDIRECT + "c": _ok(
            "www.ya-man.co.jp", _html(og="ヤーマン株式会社", canonical="https://mirror.example.com/x"))})
        self.assertFalse(ok)
        self.assertEqual(verified[0]["page_verification"]["reason"], "canonical_domain_mismatch")

    def test_unverifiable_source_moves_to_next_and_early_stop(self):
        cits = [{"uri": REDIRECT + "a", "title": "biccamera.com"},
                {"uri": REDIRECT + "b", "title": "ya-man.co.jp"},
                {"uri": REDIRECT + "c", "title": "cosme.net"}]
        ok, verified, fetched = self._confirm("ヤーマン", cits, {
            REDIRECT + "a": {"status": cv.UNVERIFIABLE, "reason": "timeout", "final_url": REDIRECT + "a"},
            REDIRECT + "b": _ok("www.ya-man.co.jp", _html(og="ヤーマン株式会社")),
        })
        self.assertTrue(ok)
        self.assertEqual(fetched, [REDIRECT + "a", REDIRECT + "b"])  # 確認後は3件目を取得しない
        self.assertEqual(verified[0]["page_verification"]["status"], cv.UNVERIFIABLE)
        self.assertNotIn("page_verification", verified[2])

    def test_same_domain_checked_once_and_at_most_five_domains(self):
        cits = [{"uri": REDIRECT + f"d{i}", "title": "panasonic.jp"} for i in range(3)]
        cits += [{"uri": REDIRECT + f"e{i}", "title": f"site{i}.example.com"} for i in range(6)]
        responses = {c["uri"]: _ok(c["title"], _html(og="Other")) for c in cits}
        ok, _, fetched = self._confirm("ヤーマン", cits, responses)
        self.assertFalse(ok)
        self.assertEqual(fetched, [REDIRECT + "d0"] + [REDIRECT + f"e{i}" for i in range(4)])

    def test_title_rule_confirmation_skips_http(self):
        cits = [{"uri": REDIRECT + "t", "title": "dhc.co.jp"}]
        with patch.object(cv, "fetch_html", side_effect=AssertionError("HTTP不要のはず")):
            verified = pipeline.attach_citation_page_verification("DHC", cits)
        self.assertIs(verified, cits)
        self.assertTrue(pipeline.is_official_source_confirmed("DHC", verified))

    def test_saved_verification_is_reused_without_http(self):
        cits = [{"uri": REDIRECT + "s", "title": "ya-man.co.jp", "page_verification": {
            "fetch_status": cv.OK, "status": "confirmed", "final_domain": "www.ya-man.co.jp",
            "canonical_domain": None,
            "site_identity": {"og_site_name": "ヤーマン株式会社", "jsonld_names": [], "title_site_name": None}}}]
        with patch.object(cv, "fetch_html", side_effect=AssertionError("保存値を使うはず")):
            self.assertIs(pipeline.attach_citation_page_verification("ヤーマン", cits), cits)
            self.assertTrue(pipeline.is_official_source_confirmed("ヤーマン", cits))
        # 保存値の改ざん(ドメイン不整合)は再計算で拒否される。
        tampered = [dict(cits[0], page_verification=dict(cits[0]["page_verification"], final_domain="evil.example"))]
        self.assertFalse(pipeline.is_official_source_confirmed("ヤーマン", tampered))


class StagingAndCollectionTests(OrchestratorTestBase):

    def _staging_id(self, brand):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id, stage1_citations, stage2_payload FROM product_collection_staging "
                        "WHERE brand = %s ORDER BY staging_id DESC LIMIT 1", (brand,))
            return cur.fetchone()
        finally:
            conn.close()

    def _insert_device(self, label, citations):
        brand, name = f"ヤーマン{label}{TEST_NAME_SUFFIX}", f"RF機{label}{TEST_NAME_SUFFIX}"
        self._insert_staging_row(self._new_batch_id(f"s455-{label}"), brand, name, "美容機器",
                                 stage2_payload={"active_ingredients": [], "formulation_features": [],
                                                 "category_attributes": {"method": {"value": "RF", "confidence": "high",
                                                                                    "source_url": citations[0]["uri"]}}},
                                 citations=citations)
        return brand, name, self._staging_id(brand)[0]

    def _execute_reuse(self, brand, name, sid, fetch):
        budget = orchestrator.BatchBudget(self._new_batch_id("s455-exec"), 5, 20, 0.50)
        with patch.object(cv, "fetch_html", side_effect=fetch), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=AssertionError("Gemini不要")), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}), patch.object(pipeline, "resolve_item_code_for_product", return_value={"status": "confirmed", "initial_candidate_count": 1, "item": {"itemCode": "test:1", "itemName": "test", "shopName": "test"}}):
            return orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                  "discovery_source": "staging_reuse", "staging_id": sid}], 3, set(),
            )

    def test_staging_reuse_uses_saved_verification_without_http(self):
        site = lambda label: {  # noqa: E731
            "fetch_status": cv.OK, "status": "confirmed", "final_domain": "www.ya-man.co.jp",
            "canonical_domain": "www.ya-man.co.jp",
            "site_identity": {"og_site_name": f"ヤーマンSaved{TEST_NAME_SUFFIX}株式会社",
                              "jsonld_names": [], "title_site_name": None}}
        cits = [{"uri": REDIRECT + "saved", "title": "ya-man.co.jp", "page_verification": site("Saved")}]
        brand, name, sid = self._insert_device("Saved", cits)
        actions = self._execute_reuse(brand, name, sid, AssertionError("保存済み結果を使うはず"))
        self.assertEqual(actions[0]["action"], "reflected")

    def test_staging_reuse_without_verification_fetches_once_and_persists(self):
        cits = [{"uri": REDIRECT + "new", "title": "ya-man.co.jp"}]
        brand, name, sid = self._insert_device("New", cits)
        calls = []

        def fetch(url):
            calls.append(url)
            return _ok("www.ya-man.co.jp", _html(og=f"ヤーマンNew{TEST_NAME_SUFFIX}株式会社"))
        actions = self._execute_reuse(brand, name, sid, fetch)
        self.assertEqual(actions[0]["action"], "reflected")
        self.assertEqual(calls, [REDIRECT + "new"])
        saved = self._staging_id(brand)[1]
        self.assertEqual(saved[0]["page_verification"]["status"], "confirmed")

    def test_dry_run_evaluation_does_not_fetch(self):
        cits = [{"uri": REDIRECT + "dry", "title": "ya-man.co.jp"}]
        brand, name, sid = self._insert_device("Dry", cits)
        with patch.object(cv, "fetch_html", side_effect=AssertionError("dry-runでは取得しない")):
            evaluation = orchestrator.evaluate_staging_for_reflect(sid, brand, name, "美容機器")
        self.assertFalse(evaluation["reflectable"])
        self.assertTrue(evaluation["page_verification_pending"])

    def test_collection_attaches_verification_after_stage1(self):
        brand, name = f"ヤーマンCol{TEST_NAME_SUFFIX}", f"RF機Col{TEST_NAME_SUFFIX}"
        mock = make_mock_call_gemini({f"{brand} {name}": {
            "stage1": {"text": "RF高周波方式。", "citations": [{"uri": REDIRECT + "col", "title": "ya-man.co.jp"}]},
            "stage2_payload": {"brand": brand, "product_name": name, "jan_code": "unknown",
                               "active_ingredients": [], "formulation_features": [], "official_source_confirmed": False,
                               "category_attributes": {"method": {"value": "RF", "confidence": "high",
                                                                  "source_url": REDIRECT + "col"}}},
        }})
        budget = orchestrator.BatchBudget(self._new_batch_id("s455-col"), 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=mock), \
             patch.object(cv, "fetch_html", return_value=_ok(
                 "www.ya-man.co.jp", _html(og=f"ヤーマンCol{TEST_NAME_SUFFIX}株式会社"))), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}), patch.object(pipeline, "resolve_item_code_for_product", return_value={"status": "confirmed", "initial_candidate_count": 1, "item": {"itemCode": "test:1", "itemName": "test", "shopName": "test"}}):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c}], 3, set(),
            )
        self.assertEqual(actions[0]["action"], "reflected")
        _, citations, payload = self._staging_id(brand)
        self.assertEqual(citations[0]["page_verification"]["status"], "confirmed")
        self.assertTrue(payload["official_source_confirmed"])


if __name__ == "__main__":
    unittest.main()
