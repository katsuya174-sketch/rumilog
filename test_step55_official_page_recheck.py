"""Step55: 公式情報の確認の改善(一時エラーの回数制限付き再試行・仕様の出典ドメインの確認漏れ防止・
他のstaging行の確認結果の再利用・確認結果の記録)。

実HTTP・本番DBは使わない(fetch_htmlは差し替え)。
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import product_collection_pipeline as pipeline  # noqa: E402
from citation_verification import OK, UNVERIFIABLE  # noqa: E402
import citation_verification as cv  # noqa: E402
from test_step45_5_citation_page import _html, _ok  # noqa: E402

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s55_"
TIMEOUT = {"status": UNVERIFIABLE, "reason": "timeout", "final_url": ""}
FORBIDDEN = {"status": UNVERIFIABLE, "reason": "http_403", "final_url": ""}


def _cit(i, domain, verification=None):
    c = {"uri": CIT + str(i), "title": domain}
    if verification is not None:
        c["page_verification"] = verification
    return c


def _fetcher(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        r = responses[url]
        return r.pop(0) if isinstance(r, list) else r
    return fetch, calls


class ImmediateRetryTests(unittest.TestCase):
    def test_timeout_is_retried_then_confirmed(self):
        fetch, calls = _fetcher({CIT + "1": [TIMEOUT, _ok("www.suntory-kenko.com", _html(og="サントリーウエルネス"))]})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            record = pipeline._verify_citation_page("サントリーウエルネス", _cit(1, "suntory-kenko.com"))
        self.assertEqual((record["status"], record["attempts"], len(calls)), ("confirmed", 2, 2))

    def test_retries_are_limited(self):
        fetch, calls = _fetcher({CIT + "1": TIMEOUT})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            record = pipeline._verify_citation_page("B", _cit(1, "b.example.com"))
        self.assertEqual((record["reason"], record["attempts"], len(calls)), ("timeout", 3, 3))

    def test_forbidden_is_not_retried_immediately(self):
        fetch, calls = _fetcher({CIT + "1": FORBIDDEN})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            record = pipeline._verify_citation_page("B", _cit(1, "b.example.com"))
        self.assertEqual((record["reason"], record["attempts"], len(calls)), ("http_403", 1, 1))


class ForbiddenAndRateLimitTests(unittest.TestCase):
    """403・429は、その場で再試行せず、期限(24時間)・回数(2回)の範囲の再確認だけ(Step50の制限を維持)。"""

    def test_429_is_not_retried_immediately(self):
        fetch, calls = _fetcher({CIT + "1": {"status": UNVERIFIABLE, "reason": "http_429", "final_url": ""}})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            record = pipeline._verify_citation_page("B", _cit(1, "b.example.com"))
        self.assertEqual((record["attempts"], len(calls)), (1, 1))

    def test_scheduled_retry_limits_hold_for_403_and_429(self):
        for reason in ("http_403", "http_429"):
            with self.subTest(reason=reason):
                blocked = {"status": UNVERIFIABLE, "reason": reason, "final_url": ""}
                rec = {"fetch_status": UNVERIFIABLE, "status": UNVERIFIABLE, "reason": reason,
                       "checked_at": (NOW - timedelta(days=3)).isoformat()}
                cits = [_cit(1, "b.example.com", rec)]
                fetch, calls = _fetcher({CIT + "1": blocked})
                with patch.object(cv, "fetch_html", side_effect=fetch):
                    out, _, _ = pipeline.refresh_page_verifications("B", cits, {}, cache={}, now=NOW)
                    out, _, _ = pipeline.refresh_page_verifications("B", out, {}, cache={}, now=NOW + timedelta(hours=1))
                    out, _, _ = pipeline.refresh_page_verifications("B", out, {}, cache={}, now=NOW + timedelta(hours=25))
                    out, _, _ = pipeline.refresh_page_verifications("B", out, {}, cache={}, now=NOW + timedelta(days=5))
                self.assertEqual(len(calls), 2)   # 24時間未満の再実行では取得しない・2回で打ち切り
                self.assertTrue(out[0]["page_verification"]["transient_exhausted"])

    def test_reused_forbidden_record_does_not_trigger_access(self):
        rec = {"fetch_status": UNVERIFIABLE, "status": UNVERIFIABLE, "reason": "http_403",
               "checked_at": (NOW - timedelta(hours=1)).isoformat()}
        cache = pipeline.build_page_verification_cache([(1, [_cit(1, "asahi-gf.co.jp", rec)])])
        with patch.object(cv, "fetch_html", side_effect=AssertionError("403の直後に再アクセスしない")):
            out, _, log = pipeline.refresh_page_verifications("X", [_cit(2, "asahi-gf.co.jp")], {}, cache=cache, now=NOW)
        self.assertEqual(log[-1]["action"], "reused")


class SpecSourceDomainTests(unittest.TestCase):
    def test_spec_source_domain_is_checked_beyond_domain_limit(self):
        done = {"fetch_status": OK, "status": "not_confirmed", "final_domain": "x", "checked_at": NOW.isoformat()}
        cits = [_cit(i, f"shop{i}.example.com", dict(done, final_domain=f"shop{i}.example.com")) for i in range(5)]
        cits += [_cit(9, "maker-a.example.jp"), _cit(10, "other.example.com")]
        payload = {"active_ingredients": [{"ingredient": "亜鉛", "source_url": CIT + "9"}]}
        fetch, calls = _fetcher({CIT + "9": _ok("www.maker-a.example.jp", _html(og="ブランドエー"))})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            out, changed, log = pipeline.refresh_page_verifications("ブランドエー", cits, payload, cache={}, now=NOW)
        self.assertTrue(changed)
        self.assertEqual(calls, [CIT + "9"])                       # 上限(5)に達していても仕様の出典ドメインは確認
        self.assertTrue(pipeline.official_confirmation("ブランドエー", out, payload)["confirmed"])
        self.assertNotIn("page_verification", out[6])             # 公式確認できたので残りは確認しない

    def test_unchecked_domains_are_checked_on_recheck(self):
        done = {"fetch_status": OK, "status": "not_confirmed", "final_domain": "kakaku.com", "checked_at": NOW.isoformat()}
        cits = [_cit(1, "kakaku.com", done), _cit(2, "e-polation-supreme.jp")]
        fetch, calls = _fetcher({CIT + "2": _ok("e-polation-supreme.jp", _html(og="株式会社ユニッシュ"))})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            out, _, log = pipeline.refresh_page_verifications("株式会社ユニッシュ", cits, {}, cache={}, now=NOW)
        self.assertEqual(calls, [CIT + "2"])
        self.assertEqual(log[-1]["action"], "fetched")

    def test_other_domains_still_limited(self):
        cits = [_cit(i, f"site{i}.example.com") for i in range(8)]
        responses = {c["uri"]: _ok(c["title"], _html(og="Other")) for c in cits}
        fetch, calls = _fetcher(responses)
        with patch.object(cv, "fetch_html", side_effect=fetch):
            _, _, log = pipeline.refresh_page_verifications("ヤーマン", cits, {}, cache={}, now=NOW)
        self.assertEqual(len(calls), pipeline.MAX_PAGE_VERIFICATION_DOMAINS)
        self.assertEqual(sum(1 for e in log if e.get("action") == "skipped_domain_limit"), 3)


class ReuseTests(unittest.TestCase):
    def ok_record(self, domain, og, checked):
        return {"fetch_status": OK, "status": "confirmed", "final_domain": f"www.{domain}", "canonical_domain": None,
                "site_identity": {"og_site_name": og, "jsonld_names": []}, "checked_at": checked.isoformat()}

    def test_ok_result_from_other_row_is_reused_and_judged_for_this_brand(self):
        cache = pipeline.build_page_verification_cache([(200, [_cit(1, "suntory-kenko.com",
                                                                  self.ok_record("suntory-kenko.com", "サントリーウエルネス", NOW - timedelta(days=3)))])])
        cits = [_cit(5, "suntory-kenko.com")]
        with patch.object(cv, "fetch_html", side_effect=AssertionError("再利用できるのでHTTP不要")):
            out, _, log = pipeline.refresh_page_verifications("サントリーウエルネス", cits, {}, cache=cache, now=NOW)
            other, _, _ = pipeline.refresh_page_verifications("DHC", [_cit(6, "suntory-kenko.com")], {}, cache=cache, now=NOW)
        self.assertEqual((log[-1]["action"], out[0]["page_verification"]["reused_from_staging"]), ("reused", 200))
        self.assertTrue(pipeline.official_confirmation("サントリーウエルネス", out, {})["confirmed"])
        self.assertFalse(pipeline.official_confirmation("DHC", other, {})["confirmed"])   # 他ブランドでは肯定しない

    def test_old_or_mismatched_ok_result_is_not_reused(self):
        old = self.ok_record("a.example.com", "A", NOW - timedelta(days=40))
        moved = dict(self.ok_record("b.example.com", "B", NOW), final_domain="other.example.net")
        cache = pipeline.build_page_verification_cache([(1, [_cit(1, "a.example.com", old), _cit(2, "b.example.com", moved)])])
        self.assertIsNone(pipeline.cached_page_verification(cache, "a.example.com", NOW))
        self.assertIsNone(pipeline.cached_page_verification(cache, "b.example.com", NOW))

    def test_domain_with_differing_site_identities_is_not_reused(self):
        # 実データ: yahoo.co.jp は行によって「Yahoo!ショッピング」「LOHACO by ASKUL」(サブドメインが違う)
        shop = dict(self.ok_record("yahoo.co.jp", "Yahoo!ショッピング", NOW - timedelta(days=1)), final_domain="shopping.yahoo.co.jp")
        lohaco = dict(self.ok_record("yahoo.co.jp", "LOHACO by ASKUL", NOW - timedelta(days=2)), final_domain="lohaco.yahoo.co.jp")
        cache = pipeline.build_page_verification_cache([(1, [_cit(1, "yahoo.co.jp", shop)]), (2, [_cit(2, "yahoo.co.jp", lohaco)])])
        self.assertIsNone(pipeline.cached_page_verification(cache, "yahoo.co.jp", NOW))
        same = pipeline.build_page_verification_cache([(1, [_cit(1, "yahoo.co.jp", shop)]), (3, [_cit(3, "yahoo.co.jp", dict(shop))])])
        self.assertEqual(pipeline.cached_page_verification(same, "yahoo.co.jp", NOW)["reused_from_staging"], 1)

    def test_subdomain_title_is_not_shared_with_parent(self):
        cache = pipeline.build_page_verification_cache([(1, [_cit(1, "brand.example.com", self.ok_record("brand.example.com", "B", NOW))])])
        self.assertIsNone(pipeline.cached_page_verification(cache, "example.com", NOW))

    def test_recent_forbidden_is_reused_without_access(self):
        recent = {"fetch_status": UNVERIFIABLE, "status": UNVERIFIABLE, "reason": "http_403", "checked_at": (NOW - timedelta(hours=2)).isoformat()}
        old = dict(recent, checked_at=(NOW - timedelta(days=2)).isoformat())
        self.assertEqual(pipeline.cached_page_verification(
            pipeline.build_page_verification_cache([(1, [_cit(1, "asahi-gf.co.jp", recent)])]), "asahi-gf.co.jp", NOW)["reason"], "http_403")
        self.assertIsNone(pipeline.cached_page_verification(
            pipeline.build_page_verification_cache([(1, [_cit(1, "asahi-gf.co.jp", old)])]), "asahi-gf.co.jp", NOW))

    def test_reused_records_are_not_reused_again(self):
        reused = dict(self.ok_record("a.example.com", "A", NOW), reused_from_staging=1)
        self.assertEqual(pipeline.build_page_verification_cache([(2, [_cit(1, "a.example.com", reused)])]), {})


# 実データ(staging 139)のStage1本文の行
UNISH_TEXT = ("*   ブランド名：ユニッシュ (UNISH / UNISH lx.)\n*   株式会社ユニッシュ E-Polation Supreme製品ページ\n"
              "*   BiYou イーポレーション・シュプリーム製品詳細\n*   MISUMI浜田山 店舗ブログ\n*   Yahoo!ショッピング 商品情報\n")


class RelevantDomainTests(unittest.TestCase):
    def cits(self):
        return [_cit(i, d) for i, d in enumerate(["unish.co.jp", "e-polation-supreme.jp", "bi-you.jp", "misumis.jp",
                                                   "yahoo.co.jp", "unish-proshop.jp", "beautygarage.jp"])]

    def test_product_site_and_brand_site_are_relevant(self):
        found = pipeline.relevant_domains("株式会社ユニッシュ", "イーポレーション・シュプリーム", UNISH_TEXT, self.cits())
        self.assertEqual(found, {"unish.co.jp", "e-polation-supreme.jp"})   # 小売・ブログ・ブランド名を含まない行の語は使わない

    def test_relevant_domain_is_checked_before_others_and_beyond_limit(self):
        done = {"fetch_status": OK, "status": "not_confirmed", "checked_at": NOW.isoformat()}
        cits = [_cit(i, f"shop{i}.example.com", dict(done, final_domain=f"shop{i}.example.com")) for i in range(5)]
        cits += [_cit(8, "beautygarage.jp"), _cit(9, "e-polation-supreme.jp")]
        fetch, calls = _fetcher({CIT + "9": _ok("e-polation-supreme.jp", _html(og="Other")), CIT + "8": _ok("beautygarage.jp", _html(og="X"))})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            _, _, log = pipeline.refresh_page_verifications("株式会社ユニッシュ", cits, {}, cache={}, now=NOW,
                                                            product_name="イーポレーション・シュプリーム", stage1_text=UNISH_TEXT)
        self.assertEqual(calls, [CIT + "9"])                                  # 関連ドメインは上限に関係なく確認
        self.assertEqual([e["action"] for e in log if e.get("domain") == "beautygarage.jp"], ["skipped_domain_limit"])

    def test_relevance_does_not_make_a_site_official(self):
        cits = [_cit(9, "e-polation-supreme.jp")]
        fetch, _ = _fetcher({CIT + "9": _ok("e-polation-supreme.jp", _html(og="イーポレーション公式"))})
        with patch.object(cv, "fetch_html", side_effect=fetch):
            out, _, _ = pipeline.refresh_page_verifications("株式会社ユニッシュ", cits, {}, cache={}, now=NOW,
                                                            product_name="イーポレーション・シュプリーム", stage1_text=UNISH_TEXT)
        self.assertFalse(pipeline.official_confirmation("株式会社ユニッシュ", out, {})["confirmed"])


class CorporateDesignatorTests(unittest.TestCase):
    def test_company_name_brand_matches_same_company_identity(self):
        # 実データ(staging 140 drsele.co.jp)のサイトの名乗り
        record = {"fetch_status": OK, "final_domain": "www.drsele.co.jp", "canonical_domain": None,
                  "site_identity": {"og_site_name": "株式会社ドクターセレクト Dr.Select - エステ化粧品や美容機器の卸販売",
                                    "jsonld_names": ["ドクターセレクト", "株式会社ドクターセレクト"]}}
        cit = _cit(1, "drsele.co.jp", record)
        self.assertTrue(pipeline._page_identity_confirms_brand("株式会社ドクターセレクト", cit))
        self.assertFalse(pipeline._page_identity_confirms_brand("株式会社ユニッシュ", cit))


if __name__ == "__main__":
    unittest.main()
