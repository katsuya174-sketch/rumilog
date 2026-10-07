"""Step45.18: official evidenceのドメイン単位評価のテスト。

実HTTP・実Gemini・実楽天・本番DBは使わない。
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import citation_verification as cv  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s4518_"


def _ok(domain, og=None, canonical=None, title_site_name=None):
    return {"fetch_status": "ok", "status": "not_confirmed", "final_domain": domain,
            "canonical_domain": canonical if canonical is not None else domain,
            "site_identity": {"og_site_name": og, "jsonld_names": [], "title_site_name": title_site_name}}


def _transient(**extra):
    return dict({"fetch_status": "unverifiable", "status": "unverifiable", "reason": "timeout"}, **extra)


def _cit(i, title, pv=None):
    c = {"uri": f"{CIT}{i}", "title": title}
    if pv is not None:
        c["page_verification"] = pv
    return c


def classify(brand, citations):
    return orchestrator.classify_official_evidence(brand, citations)


class DomainSharingTests(unittest.TestCase):

    def test_same_domain_citations_share_one_verification(self):
        cits = [_cit(1, "panasonic.jp", _ok("panasonic.jp", "Panasonic")), _cit(2, "panasonic.jp"), _cit(3, "panasonic.jp"),
                _cit(4, "kakaku.com", _ok("kakaku.com", "価格.com")), _cit(5, "kakaku.com")]
        result = classify("パナソニック", cits)
        self.assertEqual(result["unverified"], [])
        self.assertEqual(len(result["not_confirmed"]), 5)
        self.assertEqual(result["status"], "failed")

    def test_confirmed_is_shared_after_recheck(self):
        cits = [_cit(1, "ya-man.co.jp", _ok("www.ya-man.co.jp", "ヤーマン株式会社")), _cit(2, "ya-man.co.jp")]
        result = classify("ヤーマン", cits)
        self.assertEqual(result["confirmed"], ["ya-man.co.jp", "ya-man.co.jp"])
        self.assertEqual(result["status"], "confirmed")

    def test_transient_is_shared(self):
        cits = [_cit(1, "ksdenki.com", _transient()), _cit(2, "ksdenki.com"), _cit(3, "kakaku.com", _ok("kakaku.com", "価格.com"))]
        result = classify("パナソニック", cits)
        self.assertEqual(result["transient_unverified"], ["ksdenki.com", "ksdenki.com"])
        self.assertEqual(result["unverified"], [])
        self.assertEqual(result["status"], "undetermined")

    def test_only_unchecked_domains_are_unverified(self):
        cits = [_cit(1, "panasonic.jp", _ok("panasonic.jp", "Panasonic")), _cit(2, "panasonic.jp"), _cit(3, "wowma.jp")]
        result = classify("パナソニック", cits)
        self.assertEqual(result["unverified"], ["wowma.jp"])
        self.assertEqual(result["status"], "undetermined")

    def test_inconsistent_verification_is_not_shared(self):
        # final domainがcitation titleドメインと整合しない取得結果は、同一title
        # ドメインの他citationへ肯定・否定とも流用しない。
        for pv in (_ok("evil.example.com", "ヤーマン株式会社"),
                   _ok("www.ya-man.co.jp", "ヤーマン株式会社", canonical="mirror.example.com")):
            with self.subTest(pv=pv["final_domain"] + "/" + str(pv["canonical_domain"])):
                result = classify("ヤーマン", [_cit(1, "ya-man.co.jp", pv), _cit(2, "ya-man.co.jp")])
                self.assertEqual(result["confirmed"], [])
                self.assertEqual(result["not_confirmed"], ["ya-man.co.jp"])  # 自身の結果は完了済み
                self.assertEqual(result["unverified"], ["ya-man.co.jp"])    # 流用されない

    def test_other_domain_verification_is_never_used(self):
        cits = [_cit(1, "ya-man.co.jp", _ok("www.ya-man.co.jp", "ヤーマン株式会社")), _cit(2, "cosme.net")]
        result = classify("ヤーマン", cits)
        self.assertEqual(result["confirmed"], ["ya-man.co.jp"])
        self.assertEqual(result["unverified"], ["cosme.net"])

    def test_subdomain_citation_title_cannot_borrow_parent_domain_result(self):
        # titleドメインが異なれば(親/子ドメインでも)共有しない。
        cits = [_cit(1, "ya-man.co.jp", _ok("www.ya-man.co.jp", "ヤーマン株式会社")), _cit(2, "shop.ya-man.co.jp")]
        self.assertEqual(classify("ヤーマン", cits)["unverified"], ["shop.ya-man.co.jp"])


class SafetyRegressionTests(unittest.TestCase):

    def test_kakaku_false_positive_does_not_return(self):
        kakaku = dict(_ok("kakaku.com", "価格.com", title_site_name="パナソニック バイタリフト RF EH-SR85 スペック・仕様"),
                      status="confirmed")
        result = classify("パナソニック", [_cit(1, "kakaku.com", kakaku), _cit(2, "kakaku.com"), _cit(3, "kakaku.com")])
        self.assertEqual(result["confirmed"], [])
        self.assertEqual(result["status"], "failed")

    def test_panasonic_katakana_not_matched_to_ascii_identity(self):
        cits = [_cit(1, "panasonic.jp", _ok("panasonic.jp", "Panasonic")), _cit(2, "panasonic.jp")]
        self.assertEqual(classify("パナソニック", cits)["confirmed"], [])
        self.assertEqual(classify("Panasonic", cits)["status"], "confirmed")

    def test_retry_still_targets_only_own_transient_records(self):
        now = datetime(2026, 10, 9, tzinfo=timezone.utc)
        cits = [_cit(1, "ksdenki.com", _transient(checked_at=(now - timedelta(days=2)).isoformat())),
                _cit(2, "ksdenki.com")]  # 同一ドメインの共有分は再取得対象にしない
        calls = []

        def fetch(url):
            calls.append(url)
            return {"status": cv.UNVERIFIABLE, "reason": "timeout", "final_url": ""}
        with patch.object(cv, "fetch_html", side_effect=fetch), patch.object(pipeline, "_utcnow", return_value=now):
            out, _ = pipeline.retry_transient_page_verifications("パナソニック", cits, now=now)
        self.assertEqual(calls, [CIT + "1"])
        self.assertEqual(out[0]["page_verification"]["retry_count"], 1)
        self.assertNotIn("page_verification", out[1])

    def test_attach_still_checks_at_most_five_domains(self):
        cits = [_cit(i, f"site{i}.example.com") for i in range(8)]
        calls = []

        def fetch(url):
            calls.append(url)
            return {"status": cv.OK, "reason": None, "final_url": "", "final_domain": "other.example.com", "html": ""}
        with patch.object(cv, "fetch_html", side_effect=fetch):
            pipeline.attach_citation_page_verification("ヤーマン", cits)
        self.assertEqual(len(calls), pipeline.MAX_PAGE_VERIFICATION_DOMAINS)


if __name__ == "__main__":
    unittest.main()
