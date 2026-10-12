"""Step50.1: 主要成分の補足表記照合と、JANの記載が無い楽天出品の照合のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

JAN = "4902720163934"
BRAND, NAME = "森永ビヒダス（森永乳業）", "生きて届く ビフィズス菌BB536"
STAGE1 = ("*   JANコード\n*   4902720163934（30日分）\n*   ※15日分は「4902720133760」となります。\n"
          "*   機能性関与成分：ビフィズス菌BB536 50億個（1日摂取目安量1粒あたり）\n*   1日1粒\n")
CONTEXT = {"stage1_text": STAGE1, "active_ingredients": ["ビフィズス菌BB536", "ミルクオリゴ糖（ラクチュロース）"]}
OK_TITLE = "【ゆうパケット配送対象】森永 ビヒダス 生きて届く ビフィズス菌BB536 30カプセル(機能性表示食品 サプリ)(森永乳業)"


def _item(code, title, caption=""):
    return {"itemCode": code, "itemName": title, "itemCaption": caption, "itemPrice": 1500,
            "itemUrl": f"https://item.rakuten.co.jp/{code.replace(':', '/')}/", "shopName": "shop",
            "reviewCount": 3, "reviewAverage": 4.0, "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}]}


def check(title, context=CONTEXT, brand=BRAND, name=NAME, category="サプリメント"):
    return pipeline.no_jan_identity_check(_item("x:1", title), brand, name, category, JAN, context)


def resolve(items, context=CONTEXT):
    with patch.object(app, "fetch_rakuten_candidates", return_value=[(150, i) for i in items]):
        return pipeline.resolve_item_code_for_product(BRAND, NAME, "サプリメント", jan_code=JAN, identity_context=context)


class PrimaryNoteTests(unittest.TestCase):

    def tags(self, actives, primary):
        return pipeline.derive_primary_ingredient_tags({"active_ingredients": [{"ingredient": a} for a in actives],
                                                        "category_attributes": {"primary_ingredients": {"value": primary}}})

    def test_supplementary_note_matches(self):
        self.assertEqual(self.tags(["プラズマ乳酸菌（L. lactis strain Plasma）", "イソマルトオリゴ糖"], "プラズマ乳酸菌"),
                         ["probiotics"])

    def test_note_that_changes_or_adds_ingredient_does_not_match(self):
        self.assertEqual(self.tags(["コラーゲン（ペプチド）"], "コラーゲン"), [])
        self.assertEqual(self.tags(["ビタミンE（ビタミンC含有）"], "ビタミンE"), [])

    def test_ambiguous_note_match_is_not_used(self):
        self.assertEqual(self.tags(["乳酸菌（A株）", "乳酸菌（B株）"], "乳酸菌"), [])

    def test_exact_match_unchanged(self):
        self.assertEqual(self.tags(["ビタミンC"], "ビタミンC"), ["vitamin_c"])


class OfficialVariantSpecTests(unittest.TestCase):

    def test_specs_of_our_jan_with_daily_conversion(self):
        self.assertEqual(pipeline.official_variant_specs(STAGE1, JAN), {("days", 30.0), ("count", 30.0)})
        self.assertEqual(pipeline.official_variant_specs("1日2粒。JAN 4902720163934", JAN), set())


class NoJanIdentityTests(unittest.TestCase):

    def test_matching_listing_is_accepted(self):
        self.assertEqual(check(OK_TITLE), (True, None))

    def test_other_variant_size_is_rejected(self):
        self.assertEqual(check("森永乳業 ビヒダス 生きて届くビフィズス菌 15日分 ビフィズス菌 BB536")[1], "spec_conflict")

    def test_different_series_or_extra_ingredient_is_rejected(self):
        self.assertTrue(check("森永ビヒダス 生きて届く ビフィズス菌BB536 プレミアム 30カプセル")[1].startswith("series_word"))
        self.assertTrue(check("森永ビヒダス 生きて届く ビフィズス菌BB536 ＋ビタミンC 30カプセル")[1].startswith("extra_ingredient"))

    def test_missing_information_is_held(self):
        self.assertEqual(check("森永ビヒダス 生きて届く ビフィズス菌BB536 お徳用")[1], "listing_spec_missing")
        self.assertEqual(check(OK_TITLE, context={"stage1_text": "1日1粒", "active_ingredients": []})[1],
                         "official_variant_spec_unknown")
        self.assertEqual(check(OK_TITLE, context=None)[1], "no_official_context")

    def test_brand_and_name_are_required(self):
        self.assertEqual(check("生きて届く ビフィズス菌BB536 30カプセル")[1], "brand_not_in_title")
        self.assertEqual(check("森永ビヒダス ビフィズス菌BB536 30カプセル")[1], "name_parts_not_in_title")

    def test_device_without_model_number_is_held(self):
        self.assertEqual(pipeline.no_jan_identity_check(_item("d:1", "ヤーマン WAVY mini 美顔器"), "ヤーマン", "WAVY mini",
                                                        "美容機器", JAN, CONTEXT), (False, "no_model_number"))


class ResolverTests(unittest.TestCase):

    def test_no_jan_listing_is_confirmed(self):
        result = resolve([_item("ok:1", OK_TITLE, caption="機能性表示食品")])
        self.assertEqual((result["status"], result["disambiguated_by"]), ("confirmed", "name_spec_without_jan"))

    def test_jan_match_is_preferred(self):
        result = resolve([_item("nj:1", OK_TITLE, caption="説明"), _item("j:1", OK_TITLE, caption=f"JAN {JAN}")])
        self.assertEqual((result["item"]["itemCode"], result["disambiguated_by"]), ("j:1", "jan_code"))

    def test_explicit_other_jan_is_rejected(self):
        for caption in ("JAN 4902720133760", "JANコード：4902720999999"):  # 正当な別JAN / 明記された別コード
            with self.subTest(caption=caption):
                self.assertEqual(resolve([_item("m:1", OK_TITLE, caption=caption)])["reason"], "jan_mismatch")

    def test_held_when_information_is_insufficient(self):
        undeterminable = _item("u:1", OK_TITLE)
        undeterminable["itemCaption"] = None
        self.assertEqual(resolve([undeterminable])["reason"], "jan_absent_identity_unconfirmed")
        self.assertEqual(resolve([_item("n:1", OK_TITLE)], context=None)["reason"], "jan_absent_identity_unconfirmed")

    def test_other_size_listing_alone_is_not_confirmed(self):
        result = resolve([_item("v:1", "森永乳業 ビヒダス 生きて届くビフィズス菌 15日分 ビフィズス菌 BB536")])
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["no_jan_rejections"], {"spec_conflict": 1})

    def test_post_reflect_verification_accepts_only_no_jan_absent(self):
        with patch.object(app, "fetch_rakuten_item_by_item_code",
                          return_value={"ok": True, "item": {"itemPrice": 1500, "itemUrl": "https://item.rakuten.co.jp/x/1/"}}), \
             patch.object(app, "update_product_master_item_code_fields", return_value=True):
            ok = pipeline.verify_and_resolve_item_code(
                1, BRAND, NAME, "サプリメント", jan_code=JAN,
                resolution={"status": "confirmed", "item": _item("ok:1", OK_TITLE), "disambiguated_by": "name_spec_without_jan"})
            bad = pipeline.verify_and_resolve_item_code(
                1, BRAND, NAME, "サプリメント", jan_code=JAN,
                resolution={"status": "confirmed", "item": _item("m:1", OK_TITLE, caption="JAN 4902720133760"),
                            "disambiguated_by": "name_spec_without_jan"})
        self.assertNotEqual(ok.get("reason"), "jan_mismatch")
        self.assertEqual(bad["reason"], "jan_mismatch")


if __name__ == "__main__":
    unittest.main()
