"""Step51.1: 型番の完全一致または商品のJAN記載がある美容機器の出品は、収集時に限り
カテゴリ語・顔の語が無いことによる減点をしないことのテスト(診断時の採点は不変)。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import contextlib
import io
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402

JAN = "4549980667286"


def score(title, product, caption="", collection=True, jan=JAN):
    item = {"itemName": title, "itemCaption": caption}
    tokens = []
    if collection:
        tokens = [(app._PM_COLLECTION_MODE, app._PM_COLLECTION_MODE.set(True)), (app._PM_COLLECTION_JAN, app._PM_COLLECTION_JAN.set(jan))]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return app.score_rakuten_item(item, product, brand="パナソニック", category="美容機器")
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


class ExemptionTests(unittest.TestCase):

    SHORT = "バイタリフト RF EH-SR85/パナソニック"   # 「美顔器」「顔」の語が無い短いタイトル

    def test_model_match_removes_penalty_in_collection_only(self):
        self.assertGreater(score(self.SHORT, "バイタリフト RF EH-SR85"), score(self.SHORT, "バイタリフト RF EH-SR85", collection=False))

    def test_jan_in_caption_removes_penalty(self):
        title = "パナソニック バイタリフト RF ブラック"
        self.assertGreater(score(title, "バイタリフト RF", caption=f"JAN {JAN}"), score(title, "バイタリフト RF", caption="説明"))

    def test_without_identity_penalty_remains(self):
        other_model = "バイタリフト RF EH-SR75/パナソニック"
        self.assertEqual(score(other_model, "バイタリフト RF EH-SR85"),
                         score(other_model, "バイタリフト RF EH-SR85", collection=False))

    def test_diagnosis_scoring_unchanged(self):
        self.assertFalse(app._PM_COLLECTION_MODE.get())
        self.assertEqual(app._PM_COLLECTION_JAN.get(), "")


class ModelMatchNameTests(unittest.TestCase):

    def test_exact_model_is_enough_for_device_name(self):
        import product_collection_pipeline as pipeline
        # 説明語(リフトケア)が無い・カナ/英字の表記違い(ミーゼ/myse)でも、型番の完全一致で同一とみなす。
        self.assertTrue(pipeline.pm_title_name_match("リフトケア美顔器 バイタリフト RF EH-SR85",
                                                     "バイタリフト RF EH-SR85/パナソニック", "Panasonic（パナソニック）", "美容機器"))
        self.assertTrue(pipeline.pm_title_name_match("ミーゼ ディープスキンクリア（MS-43P）",
                                                     "ヤーマン ディープスキンクリア MS-43P YA-MAN myse [MS43P]", "ヤーマン（YA-MAN / mysé）", "美容機器"))

    def test_model_match_does_not_bypass_brand_model_or_set_checks(self):
        import product_collection_pipeline as pipeline
        self.assertFalse(pipeline.pm_title_name_match("バイタリフト RF EH-SR85", "互換品 EH-SR85 対応", "パナソニック", "美容機器"))
        self.assertFalse(pipeline.pm_title_name_match("バイタリフト RF EH-SR85", "パナソニック バイタリフト EH-SR75", "パナソニック", "美容機器"))
        self.assertTrue(app.pm_is_set_item("【ヤーマン公式】《メディリフト プラス セラムセット EPM18BB》"))
        item = {"itemName": "パナソニック バイタリフト RF ブラック EH-SR85-K", "itemCaption": "説明"}
        self.assertFalse(pipeline.pm_device_identity("バイタリフト RF EH-SR85", item["itemName"], "パナソニック", item, JAN)[0])

    def test_supplements_still_need_name_parts(self):
        import product_collection_pipeline as pipeline
        self.assertFalse(pipeline.pm_title_name_match("ビタミンC 1000", "DHC ビタミンD 1000", "DHC", "サプリメント"))


if __name__ == "__main__":
    unittest.main()
