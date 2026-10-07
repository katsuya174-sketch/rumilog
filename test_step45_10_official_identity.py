"""Step45.10: page verificationの公式identityをog:site_name/トップレベルJSON-LD
(Organization/Corporation/WebSite.name)だけに限定し、<title>を肯定材料に
使わないことのテスト。実HTTPは使わない(fetch_htmlをモック)。
"""

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import citation_verification as cv  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s4510_"


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


def _ok(domain, html):
    return {"status": cv.OK, "reason": None, "final_url": f"https://{domain}/p", "final_domain": domain, "html": html}


def _check(brand, citation_title, fetched):
    citations = [{"uri": REDIRECT + "1", "title": citation_title}]
    with patch.object(cv, "fetch_html", return_value=fetched):
        verified = pipeline.attach_citation_page_verification(brand, citations)
    return pipeline.is_official_source_confirmed(brand, verified), verified[0].get("page_verification")


class KakakuReproductionTests(unittest.TestCase):

    def test_saved_kakaku_record_from_98_is_not_confirmed(self):
        # 本番staging #98に保存された価格.comのpage_verificationそのもの。
        saved = [{"uri": REDIRECT + "k", "title": "kakaku.com", "page_verification": {
            "reason": None, "status": "confirmed", "fetch_status": "ok", "final_domain": "kakaku.com",
            "site_identity": {"jsonld_names": [], "og_site_name": "価格.com",
                              "title_site_name": "パナソニック バイタリフト RF EH-SR85 スペック・仕様"},
            "canonical_domain": "kakaku.com"}}]
        self.assertFalse(pipeline.is_official_source_confirmed("パナソニック", saved))

    def test_kakaku_page_titles_are_not_confirmed(self):
        for title in ("価格.com - パナソニック バイタリフト RF EH-SR85 スペック・仕様",
                      "パナソニック バイタリフト RF EH-SR85 スペック・仕様 | 価格.com",
                      "パナソニック バイタリフト RF EH-SR85 スペック・仕様"):
            with self.subTest(title=title):
                ok, pv = _check("パナソニック", "kakaku.com", _ok("kakaku.com", _html(og="価格.com", title=title)))
                self.assertFalse(ok)
                self.assertEqual(pv["status"], "not_confirmed")


class AllowedIdentityTests(unittest.TestCase):

    def test_yaman_official_sites_confirm_via_og_site_name(self):
        ok, _ = _check("ヤーマン", "ya-man.co.jp", _ok("www.ya-man.co.jp", _html(
            og="ヤーマン株式会社 企業情報サイト", title="新製品発売 | ヤーマン株式会社")))
        self.assertTrue(ok)
        ok, _ = _check("ヤーマン", "ya-man-tokyo-japan.com", _ok("www.ya-man-tokyo-japan.com", _html(
            og="YA-MAN TOKYO JAPAN｜美しさを創造する美容家電ブランド｜ヤーマン株式会社")))
        self.assertTrue(ok)

    def test_top_level_jsonld_site_name_confirms(self):
        ok, _ = _check("ヤーマン", "ya-man.co.jp", _ok("www.ya-man.co.jp", _html(
            jsonld={"@type": "WebSite", "name": "ヤーマン株式会社"})))
        self.assertTrue(ok)

    def test_brand_need_not_appear_in_domain(self):
        ok, _ = _check("ヤーマン", "corp-example.co.jp", _ok("www.corp-example.co.jp", _html(og="ヤーマン株式会社")))
        self.assertTrue(ok)

    def test_panasonic_ascii_site_identity(self):
        # 公式サイト自身の名乗りが英字「Panasonic」のみ: ブランドを「パナソニック」と
        # 表記している限り同一視しない(推測・対応表は使わない、fail closed)。
        page = _ok("panasonic.jp", _html(og="Panasonic", title="EH-SR85 | 美顔器 | Panasonic",
                                         canonical="https://panasonic.jp/face/products/EH-SR85.html",
                                         body="パナソニック株式会社"))
        self.assertFalse(_check("パナソニック", "panasonic.jp", page)[0])
        self.assertTrue(_check("Panasonic", "panasonic.jp", page)[0])


class ForbiddenEvidenceTests(unittest.TestCase):

    def test_title_only_is_not_confirmed(self):
        for title in ("フォトプラスEX | ヤーマン株式会社", "ヤーマン株式会社", "ヤーマン公式ストア - 商品"):
            with self.subTest(title=title):
                ok, pv = _check("ヤーマン", "ya-man.co.jp", _ok("www.ya-man.co.jp", _html(title=title)))
                self.assertFalse(ok)
                self.assertEqual(pv["status"], "not_confirmed")

    def test_saved_title_site_name_alone_is_not_confirmed(self):
        saved = [{"uri": REDIRECT + "t", "title": "ya-man.co.jp", "page_verification": {
            "fetch_status": "ok", "status": "confirmed", "final_domain": "www.ya-man.co.jp", "canonical_domain": None,
            "site_identity": {"og_site_name": None, "jsonld_names": [], "title_site_name": "ヤーマン株式会社"}}}]
        self.assertFalse(pipeline.is_official_source_confirmed("ヤーマン", saved))

    def test_retailer_and_comparison_sites_are_not_confirmed(self):
        cases = [
            ("store.shopping.yahoo.co.jp", "yahoo.co.jp", _html(og="Yahoo!ショッピング",
                                                                title="ヤーマン公式ストア フォトプラスEX - Yahoo!ショッピング")),
            ("www.cosme.net", "cosme.net", _html(title="YA-MAN TOKYO JAPAN(ヤーマン) / フォトPLUS EXの公式商品情報｜アットコスメ",
                                                 jsonld=[{"@type": "Product", "name": "フォトPLUS EX", "brand": "ヤーマン"},
                                                         {"@type": "Brand", "name": "ヤーマン"}])),
            ("shop.tsukumo.co.jp", "tsukumo.co.jp", _html(og="ツクモ公式通販サイト",
                                                          jsonld={"@type": "Brand", "name": "ヤーマン"})),
        ]
        for final_domain, citation_title, html in cases:
            with self.subTest(site=final_domain):
                self.assertFalse(_check("ヤーマン", citation_title, _ok(final_domain, html))[0])

    def test_product_or_brand_jsonld_only_is_not_confirmed(self):
        html = _html(jsonld=[{"@type": "Product", "name": "フォトプラスEX", "brand": {"@type": "Brand", "name": "ヤーマン"},
                              "manufacturer": {"@type": "Organization", "name": "ヤーマン株式会社"}}])
        self.assertFalse(_check("ヤーマン", "ya-man.co.jp", _ok("www.ya-man.co.jp", html))[0])

    def test_domain_and_canonical_mismatch_are_not_confirmed(self):
        ok, pv = _check("ヤーマン", "ya-man.co.jp", _ok("evil.example.com", _html(og="ヤーマン株式会社")))
        self.assertFalse(ok)
        self.assertEqual(pv["reason"], "final_domain_mismatch")
        ok, pv = _check("ヤーマン", "ya-man.co.jp", _ok("www.ya-man.co.jp", _html(
            og="ヤーマン株式会社", canonical="https://mirror.example.com/x")))
        self.assertFalse(ok)
        self.assertEqual(pv["reason"], "canonical_domain_mismatch")


if __name__ == "__main__":
    unittest.main()
