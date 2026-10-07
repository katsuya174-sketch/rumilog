"""Step45.7: 楽天item_code保存時の販売形態除外・美容機器モデル一致のテスト。

実楽天APIは呼ばない(app.fetch_rakuten_candidatesをモックする)。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
from test_product_collection_pipeline import _rakuten_item  # noqa: E402

# Step45.6の事前確認で実際に返ってきた#99の候補(楽天APIは再実行しない)。
MONOCARI_RENTAL_TITLE = (
    "【美容家電レンタル】YA-MAN(ヤーマン) 美顔器 RFボーテ フォトプラスEX RF Beaute Photo PLUS EX "
    "2週間レンタル / 格安レンタル YA-MAN ヤーマン 4540790163234"
)


class SaleConditionTests(unittest.TestCase):

    def test_non_new_sale_listings_are_detected(self):
        cases = {
            MONOCARI_RENTAL_TITLE: "rental",
            "美顔器 1ヶ月 貸出プラン": "rental",
            "【中古】ヤーマン 美顔器": "used",
            "USED品 美顔器": "used",
            "Beauty device used good condition": "used",
            "リユース品 美顔器": "used",
            "展示品 美顔器 箱なし": "display",
            "デモ機 美顔器": "display",
            "訳あり中古 美顔器": "used",
            "ＵＳＥＤ 美顔器": "used",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(app.classify_rakuten_sale_condition(title), expected)

    def test_ambiguous_promotional_words_alone_are_not_excluded(self):
        for title in ("お試しサイズ 美容液 10mL", "アウトレット 美顔器 新品", "訳あり 美容液 パッケージ不良",
                      "unused 未開封 美顔器", "Focused care serum", "お試しキャンペーン 美顔器"):
            with self.subTest(title=title):
                self.assertEqual(app.classify_rakuten_sale_condition(title), "new")

    def test_trial_combined_with_rental_is_rental(self):
        self.assertEqual(app.classify_rakuten_sale_condition("お試しレンタル 美顔器 7日間"), "rental")


class DeviceIdentityTests(unittest.TestCase):

    def assertMatch(self, product, title, expected, brand="ヤーマン"):
        matched, reason = pipeline.device_identity_matches(product, title, brand)
        self.assertEqual(matched, expected, reason)

    def test_model_name_cases(self):
        self.assertMatch("フォトプラス EX", "YA-MAN RFボーテ フォトプラスEX 美顔器", True)
        self.assertMatch("フォトプラス EX", MONOCARI_RENTAL_TITLE, True)  # 同一モデル(販売形態は別軸で除外)
        self.assertMatch("フォトプラス EX", "ヤーマン フォトプラス シャイニー 美顔器", False)
        self.assertMatch("フォトプラス EX", "ヤーマン フォトプラスEX スムースS", False)
        self.assertMatch("フォトプラス EX", "ヤーマン 美顔器 RF", False)

    def test_model_number_cases(self):
        brand = "パナソニック"
        self.assertMatch("バイタリフト RF EH-SR85", "パナソニック 美顔器 EH-SR85", True, brand)
        self.assertMatch("バイタリフト RF EH-SR85", "Panasonic EH-SR85-K ブラック", True, brand)
        self.assertMatch("バイタリフト RF EH-SR85", "パナソニック 美顔器 EH-SR75", False, brand)
        self.assertMatch("バイタリフト RF EH-SR85", "EH-SR850 美顔器", False, brand)
        self.assertMatch("バイタリフト RF EH-SR85", "EH-SR85 と EH-SR75 比較セット", False, brand)

    def test_notation_variants(self):
        brand = "パナソニック"
        self.assertMatch("ソニック RF リフト EH-SR75", "Panasonic ソニックRFリフト ＥＨ－ＳＲ７５", True, brand)
        self.assertMatch("ソニック RF リフト EH-SR75", "パナソニック EH SR75", False, brand)  # 空白で分断された型番は推測しない
        self.assertMatch("ソニック RF リフト EH-SR75", "パナソニック EHSR75 リフト", True, brand)
        self.assertMatch("フォトプラス EX", "ヤーマン　ﾌｫﾄﾌﾟﾗｽ　ＥＸ　美顔器", True)

    def test_generic_only_name_is_undeterminable(self):
        matched, reason = pipeline.device_identity_matches("RF 美顔器", "ヤーマン RF 美顔器 フォトプラス", "ヤーマン")
        self.assertEqual((matched, reason), (False, "identity_undeterminable"))


class ResolverOrderTests(unittest.TestCase):

    def _resolve(self, items, brand="ヤーマン", name="フォトプラス EX", category="美容機器"):
        with patch.object(app, "fetch_rakuten_candidates", return_value=items):
            return pipeline.resolve_item_code_for_product(brand, name, category)

    def test_monocari_rental_only_is_not_saved(self):
        rental = _rakuten_item(item_code="monocari:10000346", item_name=MONOCARI_RENTAL_TITLE, price=3990,
                               shop_name="モノカリ 楽天市場店")
        result = self._resolve([(300, rental)])
        self.assertEqual((result["status"], result["reason"]), ("not_found", "only_non_new_sale_listings"))

    def test_correct_new_listing_is_confirmed_even_if_rental_scores_higher(self):
        rental = _rakuten_item(item_code="monocari:10000346", item_name=MONOCARI_RENTAL_TITLE, price=3990)
        new = _rakuten_item(item_code="yaman:1", item_name="ヤーマン YA-MAN RFボーテ フォトプラスEX 美顔器", price=60000)
        result = self._resolve([(999, rental), (10, new)])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["item"]["itemCode"], "yaman:1")

    def test_other_model_new_listing_is_not_saved(self):
        rental = _rakuten_item(item_code="monocari:10000346", item_name=MONOCARI_RENTAL_TITLE)
        smooth = _rakuten_item(item_code="yaman:2", item_name="ヤーマン フォトプラスEX スムースS 美顔器")
        shiny = _rakuten_item(item_code="yaman:3", item_name="ヤーマン フォトプラス シャイニー 美顔器")
        result = self._resolve([(50, rental), (60, smooth), (70, shiny)])
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["reason"], "only_non_new_sale_listings")
        result = self._resolve([(60, smooth), (70, shiny)])
        self.assertEqual((result["status"], result["reason"]), ("not_found", "device_model_mismatch"))

    def test_device_model_number_mismatch_is_not_saved(self):
        other = _rakuten_item(item_code="pana:75", item_name="パナソニック 美顔器 ソニックRFリフト EH-SR75")
        result = self._resolve([(80, other)], brand="パナソニック", name="バイタリフト RF EH-SR85")
        self.assertEqual((result["status"], result["reason"]), ("not_found", "device_model_mismatch"))

    def test_set_exclusion_still_applies_after_sale_condition(self):
        bundle = _rakuten_item(item_code="yaman:4", item_name="ヤーマン フォトプラスEX 美顔器 ギフトセット")
        result = self._resolve([(80, bundle)])
        self.assertEqual((result["status"], result["reason"]), ("not_found", "only_set_items"))

    def test_cosmetics_used_listing_excluded_and_normal_listing_confirmed(self):
        used = _rakuten_item(item_code="c:1", item_name="TestBrand テスト美容液 30mL 中古")
        new = _rakuten_item(item_code="c:2", item_name="TestBrand テスト美容液 30mL")
        result = self._resolve([(90, used), (10, new)], brand="TestBrand", name="テスト美容液", category="美容液")
        self.assertEqual((result["status"], result["item"]["itemCode"]), ("confirmed", "c:2"))
        result = self._resolve([(90, used)], brand="TestBrand", name="テスト美容液", category="美容液")
        self.assertEqual(result["reason"], "only_non_new_sale_listings")

    def test_supplement_outlet_wording_is_not_excluded(self):
        item = _rakuten_item(item_code="s:1", item_name="DHC ビタミンC ハードカプセル 90日分 アウトレット 訳あり")
        result = self._resolve([(50, item)], brand="DHC", name="ビタミンC（ハードカプセル）", category="サプリメント")
        self.assertEqual(result["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
