"""Step56: Stage2の主要成分がunknownの場合、商品名と出典付きの有効成分の両方で確認できる成分だけで補完する。
実データ(staging 123 POLA インナーリフティア コラーゲン コア フォルム)の形式を使う。実API・本番DBは使わない。"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import product_collection_pipeline as pipeline  # noqa: E402

SRC = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s56"
UNKNOWN = {"category_attributes": {"primary_ingredients": {"value": "unknown", "confidence": "unknown", "source_url": "unknown"}}}


def payload(actives, primary=UNKNOWN):
    return dict(primary, active_ingredients=[{"ingredient": n, "source_url": u, "concentration": "1mg"} for n, u in actives])


class PrimaryUnknownFallbackTests(unittest.TestCase):
    NAME = "インナーリフティア コラーゲン コア フォルム"

    def test_name_and_sourced_ingredient_agree(self):
        p = payload([("コラーゲンペプチド", SRC), ("鉄", SRC), ("ヒアルロン酸", SRC), ("フィッシュエラスチン末", SRC)])
        self.assertEqual(pipeline.supplement_primary_tags_for_payload(self.NAME, p), ["collagen"])   # 副成分は加えない

    def test_name_only_is_not_enough(self):
        p = payload([("鉄", SRC), ("ヒアルロン酸", SRC)])
        self.assertEqual(pipeline.supplement_primary_tags_for_payload(self.NAME, p), [])

    def test_ingredient_without_source_is_not_used(self):
        p = payload([("コラーゲンペプチド", "unknown"), ("鉄", SRC)])
        self.assertEqual(pipeline.supplement_primary_tags_for_payload(self.NAME, p), [])

    def test_known_primary_is_unchanged(self):
        known = {"category_attributes": {"primary_ingredients": {"value": "鉄", "confidence": "high", "source_url": SRC}}}
        p = payload([("コラーゲンペプチド", SRC), ("鉄", SRC)], known)
        self.assertEqual(pipeline.supplement_primary_tags_for_payload(self.NAME, p), [])          # 根拠ありの値は従来どおり(商品名に無い鉄は除く)


if __name__ == "__main__":
    unittest.main()
