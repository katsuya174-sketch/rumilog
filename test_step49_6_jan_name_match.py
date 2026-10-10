"""Step49.6: Product Master収集経路での、検証済みJANがある場合の語順不問の名称照合のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

JAN = "4987072014325"
BRAND, NAME = "小林製薬", "亜鉛（小林製薬の栄養補助食品）"


def _item(code, title, caption=""):
    return {"itemCode": code, "itemName": title, "itemCaption": caption, "itemPrice": 980,
            "itemUrl": f"https://item.rakuten.co.jp/{code.replace(':', '/')}/", "shopName": "shop",
            "reviewCount": 5, "reviewAverage": 4.0, "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}]}


def resolve(items, jan=JAN, name=NAME, brand=BRAND):
    with patch.object(app, "fetch_rakuten_candidates", return_value=[(150, i) for i in items]):
        return pipeline.resolve_item_code_for_product(brand, name, "サプリメント", jan_code=jan)


REVERSED = _item("ok:1", "小林製薬の栄養補助食品 亜鉛 お徳用 約60日分 120粒", caption=f"JANコード：{JAN}")


class JanVerifiedNameMatchTests(unittest.TestCase):

    def test_reversed_word_order_with_jan_is_confirmed(self):
        result = resolve([REVERSED])
        self.assertEqual((result["status"], result["item"]["itemCode"]), ("confirmed", "ok:1"))

    def test_without_jan_in_listing_is_rejected(self):
        no_jan = _item("nj:1", REVERSED["itemName"], caption="")
        self.assertEqual(resolve([no_jan])["reason"], "title_mismatch")

    def test_other_jan_is_rejected(self):
        other = _item("oj:1", REVERSED["itemName"], caption="JANコード：4987072099999")
        self.assertEqual(resolve([other])["reason"], "title_mismatch")

    def test_without_verified_jan_old_rule_applies(self):
        self.assertEqual(resolve([REVERSED], jan=None)["reason"], "title_mismatch")

    def test_other_product_is_rejected(self):
        maca = _item("mz:1", "小林製薬の栄養補助食品 マカ・亜鉛 プレミアム 90粒", caption="JAN 4987072099998")
        self.assertEqual(resolve([maca])["reason"], "title_mismatch")

    def test_spec_conflict_is_rejected(self):
        result = resolve([_item("sp:1", "小林製薬の栄養補助食品 亜鉛 約30日分 60粒", caption=f"JAN {JAN}")],
                         name="亜鉛 120粒（小林製薬の栄養補助食品）")
        self.assertEqual(result["reason"], "title_mismatch")

    def test_spec_conflict_rule(self):
        self.assertTrue(pipeline.name_title_spec_conflict("亜鉛 120粒", "亜鉛 60粒 お徳用"))
        self.assertFalse(pipeline.name_title_spec_conflict("亜鉛 120粒", "亜鉛 120粒 約60日分"))
        self.assertFalse(pipeline.name_title_spec_conflict("亜鉛 120粒", "亜鉛 お徳用"))      # 記載なしは矛盾としない
        self.assertFalse(pipeline.name_title_spec_conflict("亜鉛", "亜鉛 60粒"))              # 商品名に規格なし
        self.assertFalse(pipeline.name_title_spec_conflict("ビタミンC ６０日分", "ビタミンC 60日分"))

    def test_brand_missing_is_rejected(self):
        self.assertFalse(pipeline.jan_verified_name_match(
            "亜鉛 サプリ", "小林製薬", _item("b:1", "亜鉛 サプリ 120粒", caption=f"JAN {JAN}"), JAN))

    def test_used_and_set_are_still_rejected(self):
        for title, reason in (("【中古】小林製薬の栄養補助食品 亜鉛 120粒", "only_non_new_sale_listings"),
                              ("小林製薬の栄養補助食品 亜鉛 120粒 3個セット", "only_set_items")):
            with self.subTest(title=title):
                self.assertEqual(resolve([_item("x:1", title, caption=f"JAN {JAN}")])["reason"], reason)

    def test_correct_listing_chosen_among_rejects(self):
        items = [_item("mz:1", "小林製薬の栄養補助食品 マカ・亜鉛 プレミアム", caption="JAN 4987072099998"),
                 _item("nj:1", REVERSED["itemName"]), REVERSED]
        result = resolve(items)
        self.assertEqual((result["status"], result["item"]["itemCode"]), ("confirmed", "ok:1"))

    def test_diagnosis_matcher_is_unchanged(self):
        self.assertFalse(app.is_same_verified_rakuten_product(NAME, REVERSED["itemName"], brand=BRAND))


if __name__ == "__main__":
    unittest.main()
