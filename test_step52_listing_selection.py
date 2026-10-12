"""Step52: 販売ページの選び方(同じ商品の容量違いを受け入れ、レビュー件数を優先して選ぶ)と、
診断画面の正式名表示のテスト。公式本文・楽天タイトルは実データの事例を使う。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

# 実データ(staging 145 / 147 / 102 / 125 / 107)の公式本文のJANの行
DHC_HYALURONIC = ("*   20日分（40粒）：4511413403273\n*   30日分（60粒）：4511413614839\n*   60日分（120粒）：4511413403310\n"
                  "*   4511413403273：40粒（20日分・総重量13.2g）\n")
DEAR_ZINC = ("*   ディアナチュラ 亜鉛 60日分（ボトル）：4946842635511\n"
             "*   ディアナチュラスタイル 亜鉛 20日分（パウチ）：4946842636570\n"
             "*   ディアナチュラスタイル 亜鉛 60日分（パウチ）：4946842637577\n")
NATUREMADE = ("*   4987035691914\n*   ※市場には過去の製品仕様（JANコード：4987035513919）が掲載されているサイトも存在しますが、"
              "現在の主力製品は上記のコードで広く流通しています。\n")
HYALMOIST = ("*   ヒアルモイストW タブレット：4902105954973\n"
             "*   ヒアルモイストW（ドリンクタイプ）：4902105800355（リニューアル版）\n")
DHC_BMIX = ("*   30日分：4511413614860\n*   60日分：4511413404164\n*   90日分：4511413404003\n*   60日分：4511413407660\n")


def _item(code, title, caption="説明", reviews=3, average=4.0, shop="shop", image=True, price=1500):
    return {"itemCode": code, "itemName": title, "itemCaption": caption, "itemPrice": price,
            "itemUrl": f"https://item.rakuten.co.jp/{code.replace(':', '/')}/", "shopName": shop,
            "reviewCount": reviews, "reviewAverage": average,
            "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}] if image else []}


def resolve(brand, name, items, jan=None, stage1="", actives=()):
    context = {"stage1_text": stage1, "active_ingredients": list(actives)}
    with patch.object(app, "fetch_rakuten_candidates", return_value=[(150, i) for i in items]), \
            contextlib.redirect_stdout(io.StringIO()):
        return pipeline.resolve_item_code_for_product(brand, name, "サプリメント", jan_code=jan, identity_context=context)


class OfficialVariantTests(unittest.TestCase):

    def test_size_only_lines_are_variants(self):
        self.assertEqual(pipeline.official_size_variant_jans(DHC_HYALURONIC, "4511413403310"),
                         {"4511413403273", "4511413614839"})

    def test_other_series_old_spec_and_other_form_are_not_variants(self):
        self.assertEqual(pipeline.official_size_variant_jans(DEAR_ZINC, "4946842635511"), set())
        self.assertEqual(pipeline.official_size_variant_jans(NATUREMADE, "4987035691914"), set())
        self.assertEqual(pipeline.official_size_variant_jans(HYALMOIST, "4902105954973"), set())

    def test_same_size_with_two_jans_is_ambiguous(self):
        self.assertEqual(pipeline.official_size_variant_jans(DHC_BMIX, ""), {"4511413614860", "4511413404003"})

    def test_sibling_words_exclude_own_description(self):
        self.assertEqual(pipeline.official_sibling_words(DEAR_ZINC, "ディアナチュラ 亜鉛", "ディアナチュラ", "4946842635511"),
                         {"ディアナチュラスタイル", "パウチ"})  # この商品はボトル
        self.assertIn("ドリンクタイプ", pipeline.official_sibling_words(HYALMOIST, "ヒアルモイストW", "ヒアルモイスト", "4902105954973"))


class SameProductSizeTests(unittest.TestCase):

    def test_official_variant_jan_listing_is_accepted(self):
        result = resolve("DHC", "ヒアルロン酸", [_item("v:1", "DHC ヒアルロン酸 20日分 40粒", caption="JAN 4511413403273")],
                         jan="4511413403310", stage1=DHC_HYALURONIC, actives=["ヒアルロン酸"])
        self.assertEqual((result["status"], result["identity_basis"]), ("confirmed", "official_variant_jan"))

    def test_size_not_in_official_text_is_accepted_for_same_product(self):
        result = resolve("DHC", "ヒアルロン酸", [_item("n:1", "DHC ヒアルロン酸 90日分 180粒")],
                         jan="4511413403310", stage1=DHC_HYALURONIC, actives=["ヒアルロン酸"])
        self.assertEqual((result["status"], result["identity_basis"]), ("confirmed", "name_without_jan"))

    def test_product_without_jan_accepts_other_sizes(self):
        result = resolve("DHC", "ビタミンD 30日分", [_item("d:1", "DHC ビタミンD 60日分 60粒")], stage1="1日1粒", actives=["ビタミンD"])
        self.assertEqual((result["status"], result["identity_basis"]), ("confirmed", "product_without_jan"))

    def test_other_series_is_rejected(self):
        result = resolve("ディアナチュラ", "ディアナチュラ 亜鉛",
                         [_item("s:1", "アサヒ ディアナチュラスタイル 亜鉛 60日分 パウチ")],
                         jan="4946842635511", stage1=DEAR_ZINC, actives=["亜鉛"])
        self.assertEqual(result["status"], "not_found")
        result = resolve("ディアナチュラ", "ディアナチュラ 亜鉛",
                         [_item("s:2", "ディアナチュラスタイル 亜鉛 60日分", caption="JAN 4946842637577")],
                         jan="4946842635511", stage1=DEAR_ZINC, actives=["亜鉛"])
        self.assertEqual(result["status"], "not_found")

    def test_pre_renewal_and_other_form_are_rejected(self):
        for title, caption in (("ネイチャーメイド スーパーフィッシュオイル 90粒", "JAN 4987035513919"),     # 過去の製品仕様のJAN
                               ("ネイチャーメイド スーパーフィッシュオイル 旧パッケージ 90粒", "説明"),
                               ("ネイチャーメイド スーパーフィッシュオイル 持続型 90粒", "説明")):
            with self.subTest(title=title):
                result = resolve("ネイチャーメイド（大塚製薬）", "スーパーフィッシュオイル", [_item("o:1", title, caption=caption)],
                                 jan="4987035691914", stage1=NATUREMADE, actives=["EPA", "DHA"])
                self.assertEqual(result["status"], "not_found")

    def test_other_product_of_same_brand_without_jan_is_rejected(self):
        # 実データ(#111)。容量の照合が無くなった分、タイトル本体(「|」より前)の商品名と、
        # 商品に無い栄養成分名で別商品を見分ける。
        for title in ("【店内P最大18倍以上開催】カルシウム・マグネシウム・ビタミンD増量 【DHC直販】カルシウム／マグ 30日分"
                      "【マグネシウム】 | ビタミンd ミネラル カルシウム",
                      "【店内P最大18倍以上開催】ゆらぎがちな女性の体調をサポート 【DHC直販】【DHCサプリメント】 大豆イソフラボン 吸収型"
                      " 30日分 | 女性 ビタミンd 葉酸"):
            with self.subTest(title=title):
                for jan in (None, "4511413627464"):
                    result = resolve("DHC", "ビタミンD 30日分", [_item("o:1", title)], jan=jan,
                                     stage1="| **JANコード** | 4511413627464 | |", actives=["ビタミンD"])
                    self.assertEqual(result["status"], "not_found")
        ok = "【店内P最大18倍以上開催】【DHC直販】紫外線を避けている方 ビタミンD 30日分 | ビタミンd3 国産 カルシウム"
        result = resolve("DHC", "ビタミンD 30日分", [_item("d:1", ok)], jan="4511413627464",
                         stage1="| **JANコード** | 4511413627464 | |", actives=["ビタミンD"])
        self.assertEqual(result["status"], "confirmed")

    def test_strength_difference_is_still_a_conflict(self):
        self.assertTrue(pipeline.name_title_spec_conflict("ビタミンC 1000mg", "ビタミンC 500mg 120粒"))
        self.assertFalse(pipeline.name_title_spec_conflict("ビタミンD 30日分", "ビタミンD 90日分"))

    def test_sets_are_still_rejected(self):
        result = resolve("DHC", "ヒアルロン酸", [_item("st:1", "DHC ヒアルロン酸 60日分(120粒*2コセット)", caption="JAN 4511413403310")],
                         jan="4511413403310", stage1=DHC_HYALURONIC, actives=["ヒアルロン酸"])
        self.assertEqual(result["reason"], "only_set_items")

    def test_saved_resolution_check_accepts_variant_and_rejects_other_jan(self):
        item = _item("v:1", "DHC ヒアルロン酸 20日分", caption="JAN 4511413403273")
        ok = {"status": "confirmed", "item": item, "identity_basis": "official_variant_jan",
              "official_variant_jans": ["4511413403273", "4511413614839"]}
        self.assertTrue(pipeline._saved_resolution_identity_ok(ok, "4511413403310"))
        bad = dict(ok, item=_item("x:1", "DHC ヒアルロン酸", caption="JAN 4511413999999 4511413403273"))
        self.assertFalse(pipeline._saved_resolution_identity_ok(bad, "4511413403310"))


class ReviewFirstSelectionTests(unittest.TestCase):

    def pick(self, items):
        result = resolve("DHC", "ヒアルロン酸", items, jan="4511413403310", stage1=DHC_HYALURONIC, actives=["ヒアルロン酸"])
        return result["item"]["itemCode"]

    def test_review_count_first_regardless_of_size_and_price(self):
        self.assertEqual(self.pick([_item("cheap:1", "DHC ヒアルロン酸 60日分", caption="JAN 4511413403310", reviews=10, price=900),
                                    _item("big:1", "DHC ヒアルロン酸 20日分 40粒", caption="JAN 4511413403273", reviews=500,
                                          price=4000)]), "big:1")

    def test_tie_break_order(self):
        base = "DHC ヒアルロン酸 60日分"
        self.assertEqual(self.pick([_item("a:1", base, reviews=5, average=4.0), _item("b:1", base, reviews=5, average=4.5)]), "b:1")
        self.assertEqual(self.pick([_item("a:1", base, shop="ドラッグ"), _item("b:1", base, shop="DHC楽天市場店")]), "b:1")
        self.assertEqual(self.pick([_item("a:1", base, image=False), _item("b:1", base)]), "b:1")
        self.assertEqual(self.pick([_item("b:1", base), _item("a:1", base)]), "a:1")

    def test_price_does_not_lower_collection_score(self):
        title, item = "DHC ヒアルロン酸 60日分 120粒", None
        cheap = {"itemName": title, "itemPrice": 1500}
        pricey = {"itemName": title, "itemPrice": 15000}
        token = app._PM_COLLECTION_MODE.set(True)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.score_rakuten_item(cheap, "ヒアルロン酸", brand="DHC", category="サプリメント"),
                                 app.score_rakuten_item(pricey, "ヒアルロン酸", brand="DHC", category="サプリメント"))
        finally:
            app._PM_COLLECTION_MODE.reset(token)
        with contextlib.redirect_stdout(io.StringIO()):  # 診断時の採点は従来どおり
            self.assertGreater(app.score_rakuten_item(cheap, "ヒアルロン酸", brand="DHC", category="サプリメント"),
                               app.score_rakuten_item(pricey, "ヒアルロン酸", brand="DHC", category="サプリメント"))


class OfficialDisplayNameTests(unittest.TestCase):

    def test_official_name_without_size(self):
        cases = {("DHC", "亜鉛 30日分（または60日分）"): ("DHC", "亜鉛"),
                 ("iMUSE（イミューズ）", "iMUSE（イミューズ）プラズマ乳酸菌サプリメント"): ("iMUSE", "プラズマ乳酸菌サプリメント"),
                 ("ネイチャーメイド（大塚製薬）", "ネイチャーメイド スーパーフィッシュオイル"): ("ネイチャーメイド", "スーパーフィッシュオイル"),
                 ("ヤーマン（YA-MAN / mysé）", "ミーゼ ディープスキンクリア（MS-43P）"): ("ヤーマン", "ミーゼ ディープスキンクリア（MS-43P）"),
                 ("DHC", "ビタミンC（ハードカプセル）"): ("DHC", "ビタミンC（ハードカプセル）"),
                 ("X", "ビタミンC 500mg 徳用 120粒"): ("X", "ビタミンC 500mg")}
        for (brand, name), expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(app.product_master_display_name(brand, name), expected)

    def test_result_carries_official_name_only_for_product_master(self):
        pm = {"itemName": "【P10倍】キリン iMUSE プラズマ乳酸菌サプリメント 7日分", "itemCode": "s:1", "itemPrice": 799,
              "_source": "product_master", "_pm_brand": "iMUSE（イミューズ）", "_pm_name": "プラズマ乳酸菌サプリメント"}
        result = app._build_rakuten_result(pm, "x")
        self.assertEqual((result["pm_brand"], result["pm_name"]), ("iMUSE（イミューズ）", "プラズマ乳酸菌サプリメント"))
        live = app._build_rakuten_result(dict(pm, _source="live"), "x")
        self.assertEqual((live["pm_brand"], live["pm_name"]), ("", ""))


class ProductMasterNotOverwrittenTests(unittest.TestCase):
    """診断でProduct Master由来の商品を推薦しても、product_masterへの蓄積(上書き・重複作成)をしない。"""

    def run_step(self, rows):
        step = {"category": "サプリメント", "product": "ヒアルロン酸 サプリメント", "brand": "", "supplement_type": "ヒアルロン酸",
                "ingredient_focus": ["hyaluronic_acid"], "purpose": "乾燥"}
        with patch.object(app, "query_product_master_candidates", return_value=rows), \
                patch.object(app, "fetch_rakuten_candidates", return_value=[]), \
                patch.object(app, "refresh_selected_candidate_price", side_effect=lambda p: None), \
                patch.object(app, "infer_brand_from_image", return_value=""), \
                patch.object(app, "accumulate_verified_product") as accumulate, \
                contextlib.redirect_stdout(io.StringIO()):
            result = app.attach_affiliate_links_to_step(step, {}, dict(app._EFFECTIVE_CANDIDATE_NEUTRAL_USER_DATA), 3000)
        return result, accumulate

    def test_product_master_recommendation_is_not_accumulated(self):
        row = {"product_id": 90, "category": "サプリメント", "brand": "DHC", "name": "ヒアルロン酸", "item_code": "kenkocom:11190746",
               "last_known_rakuten_link": "https://item.rakuten.co.jp/kenkocom/x/", "price_ref": 1941,
               "rakuten_title": "DHC ヒアルロン酸 60日分(120粒)【spts15】", "shop_name": "楽天24",
               "active_ingredients": ["ヒアルロン酸"], "active_ingredient_tags": ["hyaluronic_acid"],
               "category_attributes": {"primary_ingredient_tags": ["hyaluronic_acid"], "product_classification": "supplement",
                                       "supplement_eligibility_basis": {"classification": "supplement"}}}
        with patch.object(app, "is_supplement_product_master_eligible", return_value=True), \
                patch.object(app, "is_candidate_relevant_to_target", return_value=True):
            result, accumulate = self.run_step([row])
        self.assertEqual((result["brand"], result["product"]), ("DHC", "ヒアルロン酸"))
        accumulate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
