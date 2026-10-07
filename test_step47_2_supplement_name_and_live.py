"""Step47.2: 商品名からのサプリ主要成分補完と、楽天live候補の主目的判定のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

# #75 DHC ビタミンCの実際の楽天タイトル(本番product_master.rakuten_title)。
DHC_TITLE = ("【店内P最大18倍以上開催】サプリメント【DHC直販】 ビタミンC(ハードカプセル) 徳用90日分【ビタミンC・ビタミンB2】 | "
             "ビタミン 美容 ビタミンB2 大容量 スキンケア DHC サプリ インナーケア ビタミン剤 紫外線対策 UV対策 ディーエイチシー 健康食品 サプリメント well")

TARGETS = ["vitamin_c", "l_cysteine", "vitamin_b", "vitamin_d", "omega3", "collagen", "ceramide",
           "hyaluronic_acid", "probiotics", "zinc"]


class NamePrimaryTests(unittest.TestCase):

    def test_75_like_product_has_vitamin_c_primary_only(self):
        tags = app.supplement_primary_tags_from_name("ビタミンC（ハードカプセル）", ["ビタミンC", "ビタミンB2"])
        self.assertEqual(tags, ["vitamin_c"])
        product = {"name": "ビタミンC（ハードカプセル）", "active_ingredients": ["ビタミンC", "ビタミンB2", "vitamin_b", "vitamin_c"],
                   "category_attributes": {"dosage": "2粒"}}  # primary_ingredient_tags未保存(#75と同形)
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "vitamin_c", product))
        self.assertFalse(app.is_candidate_relevant_to_target("サプリメント", "vitamin_b", product))

    def test_name_ingredient_absent_from_actives_is_not_primary(self):
        self.assertEqual(app.supplement_primary_tags_from_name("コラーゲン&ビタミンC", ["ビタミンC"]), ["vitamin_c"])
        self.assertEqual(app.supplement_primary_tags_from_name("ビタミンD", ["ビタミンC"]), [])

    def test_actives_alone_do_not_make_primary(self):
        self.assertEqual(app.supplement_primary_tags_from_name("美容サプリ", ["ビタミンC", "亜鉛"]), [])

    def test_multiple_primaries_when_all_named_and_present(self):
        self.assertEqual(app.supplement_primary_tags_from_name("亜鉛＆ビタミンC", ["亜鉛", "ビタミンC", "ビタミンE"]),
                         ["vitamin_c", "zinc"])

    def test_omega3_names(self):
        self.assertEqual(app.supplement_primary_tags_from_name("DHA&EPA", ["DHA", "EPA"]), ["omega3"])
        self.assertEqual(app.supplement_primary_tags_from_name("オメガ3 フィッシュオイル", ["EPA"]), ["omega3"])

    def test_stage2_primary_takes_precedence_over_name(self):
        payload = {"active_ingredients": [{"ingredient": "ビタミンC"}, {"ingredient": "亜鉛"}],
                   "category_attributes": {"primary_ingredients": {"value": "亜鉛", "confidence": "high", "source_url": "u"}}}
        self.assertEqual(pipeline.supplement_primary_tags_for_payload("ビタミンC＋亜鉛", payload), ["zinc"])
        payload["category_attributes"]["primary_ingredients"]["value"] = "unknown"
        self.assertEqual(pipeline.supplement_primary_tags_for_payload("ビタミンC＋亜鉛", payload), [])  # unknownは補完しない
        del payload["category_attributes"]["primary_ingredients"]
        self.assertEqual(pipeline.supplement_primary_tags_for_payload("ビタミンC＋亜鉛", payload), ["vitamin_c", "zinc"])

    def test_all_ten_targets_work_the_same_way(self):
        for target in TARGETS:
            name = app._supplement_target_names(target)[0]
            with self.subTest(target=target):
                self.assertEqual(app.supplement_primary_tags_from_name(f"{name} サプリ", [name]), [target])
                self.assertEqual(app.live_supplement_title_primary_target(f"テスト {name} サプリメント 30日分"), target)


class LiveTitleTests(unittest.TestCase):

    def test_dhc_title_is_vitamin_c_not_vitamin_b(self):
        self.assertEqual(app.live_supplement_title_primary_target(DHC_TITLE), "vitamin_c")

    def test_vitamin_b_complex_title(self):
        self.assertEqual(app.live_supplement_title_primary_target("ビタミンB群 サプリメント 60日分"), "vitamin_b")

    def test_omega3_titles(self):
        self.assertEqual(app.live_supplement_title_primary_target("DHA EPA オメガ3 サプリ 120粒"), "omega3")
        self.assertIsNone(app.live_supplement_title_primary_target("ブルーベリー ルテイン DHA配合 サプリ"))
        # EPAは補助的な言及(＋EPA)なので主目的はvitamin_d(omega3にはしない)。
        self.assertEqual(app.live_supplement_title_primary_target("ビタミンD ＋EPA サプリ"), "vitamin_d")

    def test_ambiguous_titles_are_not_adopted(self):
        for title in ("マルチビタミン&ミネラル ビタミンC ビタミンB 亜鉛", "ビタミンC 亜鉛 サプリ", "美容サプリ 30日分", ""):
            with self.subTest(title=title):
                self.assertIsNone(app.live_supplement_title_primary_target(title))


class LiveFallbackFilterTests(unittest.TestCase):

    def _attach(self, target_type, items):
        step = {"category": "サプリメント", "product": "テストサプリ", "supplement_type": target_type,
                "ingredient_focus": [app._SUPPLEMENT_DEFAULTS[target_type]["ingredient_focus"][0]],
                "brand": "", "product_source": "ai"}
        with patch.object(app, "query_product_master_candidates", return_value=[]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_candidates", return_value=items):
            return app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=3000)

    def _item(self, code, title):
        return {"itemName": title, "itemCaption": "", "itemPrice": 1000, "itemCode": code,
                "itemUrl": f"https://item.rakuten.co.jp/shop/{code}/", "shopName": "s", "reviewCount": 10,
                "reviewAverage": 4.0, "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}]}

    def test_vitamin_b_step_does_not_take_dhc_vitamin_c(self):
        result = self._attach("ビタミンB群", [(300, self._item("dhc", DHC_TITLE))])
        self.assertNotIn("dhcshop", str(result.get("rakuten_link", "")))
        self.assertFalse(str(result.get("rakuten_link", "")).endswith("/dhc/"))

    def test_vitamin_b_step_takes_b_complex(self):
        result = self._attach("ビタミンB群", [(300, self._item("dhc", DHC_TITLE)),
                                           (100, self._item("bb", "ビタミンB群 サプリメント 60日分"))])
        self.assertTrue(result["rakuten_link"].endswith("/bb/"))

    def test_vitamin_c_step_still_takes_dhc(self):
        result = self._attach("ビタミンC", [(300, self._item("dhc", DHC_TITLE))])
        self.assertTrue(result["rakuten_link"].endswith("/dhc/"))


class OtherCategoriesUnchangedTests(unittest.TestCase):

    def test_device_relevance_unchanged(self):
        self.assertTrue(app.is_candidate_relevant_to_target("美容機器", "RF", {"category_attributes": {"method": "RF"}}))
        self.assertFalse(app.is_candidate_relevant_to_target("美容機器", "RF", {"name": "RF美顔器", "category_attributes": {}}))


if __name__ == "__main__":
    unittest.main()
