"""Step47.4: 公式情報源照合でのブランド末尾括弧注記の扱いのテスト。

実HTTP・実Gemini・実楽天・本番DBは使わない。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s474_"


def _site(domain, og, canonical=None):
    return {"uri": CIT + domain, "title": domain, "page_verification": {
        "fetch_status": "ok", "status": "not_confirmed", "final_domain": domain,
        "canonical_domain": canonical or domain,
        "site_identity": {"og_site_name": og, "jsonld_names": [], "title_site_name": None}}}


def official(brand, *citations):
    return pipeline.is_official_source_confirmed(brand, list(citations))


class BrandFormsTests(unittest.TestCase):

    def test_forms(self):
        self.assertEqual(pipeline.official_brand_forms("ネイチャーメイド（大塚製薬）"),
                         ["ネイチャーメイド(大塚製薬)", "ネイチャーメイド"])
        self.assertEqual(pipeline.official_brand_forms("Nature Made (Otsuka)"), ["Nature Made (Otsuka)", "Nature Made"])
        self.assertEqual(pipeline.official_brand_forms("ヤーマン"), ["ヤーマン"])

    def test_not_trailing_is_not_removed(self):
        self.assertEqual(pipeline.official_brand_forms("ブランド(X) シリーズ"), ["ブランド(X) シリーズ"])

    def test_multiple_parentheses_are_not_stripped_recursively(self):
        self.assertEqual(pipeline.official_brand_forms("ブランド (A) (B)"), ["ブランド (A) (B)", "ブランド (A)"])

    def test_too_short_base_is_not_used(self):
        self.assertEqual(pipeline.official_brand_forms("X (大塚製薬)"), ["X (大塚製薬)"])
        self.assertEqual(pipeline.official_brand_forms("(大塚製薬)"), ["(大塚製薬)"])


class OfficialMatchTests(unittest.TestCase):

    def test_nature_made_japanese_with_annotation(self):
        self.assertTrue(official("ネイチャーメイド（大塚製薬）", _site("otsuka.co.jp", "ネイチャーメイド公式サイト")))

    def test_nature_made_english_with_annotation(self):
        self.assertTrue(official("Nature Made (Otsuka)", _site("naturemade.example.com", "Nature Made")))

    def test_annotation_alone_does_not_confirm(self):
        self.assertFalse(official("ネイチャーメイド（大塚製薬）", _site("otsuka.co.jp", "大塚製薬")))
        self.assertFalse(official("Nature Made (Otsuka)", _site("otsuka.example.com", "Otsuka")))
        # 括弧内の英字だけでドメインラベルに一致させない(title rule)。
        self.assertFalse(official("Nature Made (Otsuka)", {"uri": CIT + "o", "title": "otsuka.co.jp"}))

    def test_unrelated_brand_with_same_annotation_is_false(self):
        self.assertFalse(official("別ブランド（大塚製薬）", _site("otsuka.co.jp", "大塚製薬")))
        self.assertFalse(official("別ブランド（大塚製薬）", _site("otsuka.co.jp", "ネイチャーメイド公式サイト")))

    def test_ascii_brand_with_annotation_still_matches_domain(self):
        self.assertTrue(official("DHC (ディーエイチシー)", {"uri": CIT + "d", "title": "dhc.co.jp"}))


class SafetyRegressionTests(unittest.TestCase):

    def test_panasonic_not_conflated(self):
        page = _site("panasonic.jp", "Panasonic")
        self.assertFalse(official("パナソニック", page))
        self.assertFalse(official("パナソニック（Panasonic）", {"uri": CIT + "p", "title": "cosme.net"}))
        self.assertTrue(official("Panasonic", page))

    def test_kakaku_false_positive_does_not_return(self):
        kakaku = _site("kakaku.com", "価格.com")
        kakaku["page_verification"]["site_identity"]["title_site_name"] = "パナソニック バイタリフト RF EH-SR85 スペック・仕様"
        self.assertFalse(official("パナソニック", kakaku))
        self.assertFalse(official("パナソニック（価格.com）", kakaku))

    def test_yaman_unchanged(self):
        self.assertTrue(official("ヤーマン", _site("ya-man-tokyo-japan.com",
                                                 "YA-MAN TOKYO JAPAN｜美しさを創造する美容家電ブランド｜ヤーマン株式会社")))

    def test_domain_and_canonical_conditions_kept(self):
        mismatch = _site("otsuka.co.jp", "ネイチャーメイド公式サイト")
        mismatch["page_verification"]["final_domain"] = "evil.example.com"
        self.assertFalse(official("ネイチャーメイド（大塚製薬）", mismatch))
        canonical = _site("otsuka.co.jp", "ネイチャーメイド公式サイト", canonical="mirror.example.com")
        self.assertFalse(official("ネイチャーメイド（大塚製薬）", canonical))

    def test_product_master_identity_is_unchanged(self):
        # 照合用の候補はidentity_keyに影響しない。
        self.assertNotEqual(app._normalize_product_master_identity_key("ネイチャーメイド（大塚製薬）", "フィッシュオイル", "サプリメント"),
                            app._normalize_product_master_identity_key("ネイチャーメイド", "フィッシュオイル", "サプリメント"))


if __name__ == "__main__":
    unittest.main()
