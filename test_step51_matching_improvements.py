"""Step51: Product Master収集の楽天照合・主要成分の改善(C1〜C6)のテスト。
実データの事例(タイトル)を使い、正しい出品の採用と、正しくない出品の除外の両方を確認する。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import contextlib
import io
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402


def pm_score(title, product, brand, category):
    token = app._PM_COLLECTION_MODE.set(True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return app.score_rakuten_item({"itemName": title}, product, brand=brand, category=category)
    finally:
        app._PM_COLLECTION_MODE.reset(token)


def diag_score(title, product, brand, category):
    with contextlib.redirect_stdout(io.StringIO()):
        return app.score_rakuten_item({"itemName": title}, product, brand=brand, category=category)


class C1NameMatchTests(unittest.TestCase):

    def test_katakana_alternative_and_category_words(self):
        self.assertTrue(pipeline.pm_title_name_match(
            "SHAPE POINTER（シェイプポインター）", "MYTREX シェイプポインター フェイスケア 美顔器 微弱電流", "MYTREX", "美容機器"))
        self.assertTrue(pipeline.pm_title_name_match(
            "iMUSE（イミューズ）プラズマ乳酸菌サプリメント", "キリン イミューズ プラズマ乳酸菌 サプリメント 約7日分（28粒入り）",
            "iMUSE（イミューズ）", "サプリメント", ["プラズマ乳酸菌（L. lactis strain Plasma）"]))
        self.assertTrue(pipeline.pm_title_name_match(
            "ピーリング＆超音波美顔器 KBE-2740/K", "【メーカー公式】コイズミ 超音波美顔器 KBE-2740 ピーリング 保湿", "コイズミ（KOIZUMI）", "美容機器"))

    def test_other_series_or_extra_ingredient_is_rejected(self):
        self.assertFalse(pipeline.pm_title_name_match(
            "亜鉛（小林製薬の栄養補助食品）", "小林製薬の栄養補助食品(サプリメント) マカ・亜鉛 プレミアム 90粒", "小林製薬", "サプリメント", ["亜鉛"]))
        self.assertFalse(pipeline.pm_title_name_match(
            "ビタミンC", "DHC ビタミンC ＋ 亜鉛 60粒", "DHC", "サプリメント", ["ビタミンC"]))

    def test_series_word_kana_and_english_are_the_same(self):
        self.assertIsNone(pipeline.series_word_conflict("フォトプラス EX", "YA-MAN Photo PLUS EX 美顔器"))
        self.assertEqual(pipeline.series_word_conflict("フォトプラス", "フォトプラス プレステージ PRO"), "pro")

    def test_brand_is_required(self):
        self.assertFalse(pipeline.pm_title_name_match("シェイプポインター", "シェイプポインター 互換品", "MYTREX", "美容機器"))


class C2ScoreContextTests(unittest.TestCase):

    def test_shipping_and_promo_words_do_not_reject_in_collection(self):
        title = "【メール便1便で合計4個までOK】DHC 亜鉛 60日分 60粒 DHC［サプリ/サプリメント］ 亜鉛"
        self.assertLessEqual(diag_score(title, "DHC 亜鉛", "DHC", "サプリメント"), -9000)   # 診断時は従来どおり
        self.assertGreater(pm_score(title, "DHC 亜鉛", "DHC", "サプリメント"), -9000)       # 収集時は配送条件の語を無視
        promo = "【正規品取扱店】【レビューで「美容液サンプルボトル」プレゼント】YA-MAN ヤーマン ミーゼ ディープスキンクリア MS43P 超音波 美顔器"
        self.assertGreater(pm_score(promo, "ミーゼ ディープスキンクリア", "ヤーマン", "美容機器"), -9000)

    def test_device_function_description_is_not_skincare(self):
        title = "★YA-MAN(ヤーマン) 超音波美顔器 myse(ミーゼ) ディープスキンクリア 毛穴 角栓 ディープクレンジング MS-43P"
        self.assertGreater(pm_score(title, "ミーゼ ディープスキンクリア", "ヤーマン", "美容機器"), -9000)

    def test_real_skincare_and_bundles_are_still_rejected(self):
        self.assertLessEqual(pm_score("ヤーマン メディリフト 専用 美容液 50ml", "メディリフト", "ヤーマン", "美容機器"), -9000)
        self.assertLessEqual(pm_score("DHC 亜鉛 60日分 3個セット", "DHC 亜鉛", "DHC", "サプリメント"), -9000)
        self.assertLessEqual(pm_score("【中古】ヤーマン ミーゼ ディープスキンクリア 美顔器", "ミーゼ", "ヤーマン", "美容機器"), -9000)


class C3C6ModelTests(unittest.TestCase):

    def identity(self, name, title, jan_in_title=False):
        jan = "4549980667286"
        item = {"itemName": title, "itemCaption": f"JAN {jan}" if jan_in_title else "説明"}
        return pipeline.pm_device_identity(name, title, "B", item, jan)

    def test_full_model_numbers_keep_suffix(self):
        self.assertEqual(pipeline.full_model_numbers("メディリフト プラス EPM-18BB"), {"epm18bb"})
        self.assertEqual(pipeline.full_model_numbers("セラムセット EPM18BB2"), {"epm18bb2"})
        self.assertEqual(pipeline.full_model_numbers("ピーリング＆超音波美顔器 KBE-2740/K"), {"kbe2740k"})

    def test_different_suffix_is_a_different_model(self):
        self.assertFalse(self.identity("メディリフト プラス EPM-18BB", "メディリフト プラス セラムセット EPM18BB2")[0])

    def test_color_variant_requires_jan(self):
        self.assertEqual(self.identity("バイタリフト RF EH-SR85", "バイタリフト RF ブラック EH-SR85-K"), (False, "color_variant_without_jan"))
        self.assertTrue(self.identity("バイタリフト RF EH-SR85", "バイタリフト RF ブラック EH-SR85-K", jan_in_title=True)[0])

    def test_product_without_model_needs_jan_when_title_has_model(self):
        self.assertFalse(self.identity("リファカラットレイ", "リファ ReFa CARAT RAY リファカラットレイ RF-PC2019B")[0])
        self.assertTrue(self.identity("リファカラットレイ", "リファ ReFa CARAT RAY リファカラットレイ RF-PC2019B", jan_in_title=True)[0])

    def test_own_color_letter_is_not_a_conflict(self):
        self.assertTrue(self.identity("ピーリング＆超音波美顔器 KBE-2740/K", "コイズミ KBE-2740/K ブラック KBE2740K")[0])


class C4SetTests(unittest.TestCase):

    def test_count_and_accessory_sets(self):
        for title in ("DHC ヒアルロン酸 60日分(120粒*2コセット)", "ビヒダス 30日分 3袋 ビフィズス菌 BB536 森永",
                      "キリン iMUSE プラズマ乳酸菌サプリメント 7日分(28粒入)×30個セット", "メディリフト プラス セラムセット EPM18BB2",
                      "1-3セット メディリフト プラス セラムセット", "DHC ヒアルロン酸 40粒入り 2袋入"):
            with self.subTest(title=title):
                self.assertTrue(app.pm_is_set_item(title))

    def test_singles_and_shipping_notes_are_not_sets(self):
        # Step53(ユーザー判断): 1個から買えることが書かれた出品(数量を選べる出品)はセットとしない。
        for title in ("DHC ヒアルロン酸 60日分(120粒)", "【メール便1便で合計4個までOK】DHC 亜鉛 60日分 60粒",
                      "1-3セット DHC ヒアルロン酸 60日分 120粒", "DHC 亜鉛 60日分 単品/3個セット 選べる",
                      "森永 ビヒダス 30粒入 1袋", "DHC ヒアルロン酸 120粒入"):
            with self.subTest(title=title):
                self.assertFalse(app.pm_is_set_item(title))


class C5PrimaryTests(unittest.TestCase):

    def payload(self, primary, actives):
        return {"active_ingredients": [{"ingredient": a} for a in actives],
                "category_attributes": {"primary_ingredients": {"value": primary}}}

    def test_secondary_ingredients_are_dropped(self):
        self.assertEqual(pipeline.supplement_primary_tags_for_payload(
            "ディープチャージ コラーゲン", self.payload("コラーゲンペプチド、ビタミンC", ["コラーゲンペプチド", "ビタミンC"])), ["collagen"])
        self.assertEqual(pipeline.supplement_primary_tags_for_payload(
            "ヒアルロン酸", self.payload("ヒアルロン酸、ビタミンB2", ["ヒアルロン酸", "ビタミンB2"])), ["hyaluronic_acid"])

    def test_functional_ingredient_counts_without_name(self):
        payload = self.payload("EPA、DHA", ["EPA", "DHA"])
        text = "*   **機能性関与成分**\n    *   EPA：190mg\n    *   DHA：80mg"
        self.assertEqual(pipeline.supplement_primary_tags_for_payload("フィッシュオイル", payload, text), ["omega3"])
        self.assertEqual(pipeline.supplement_primary_tags_for_payload("ラクフィット", self.payload("EPA", ["EPA"]), ""), [])

    def test_multiple_named_ingredients_are_kept(self):
        payload = self.payload("ビタミンC、亜鉛", ["ビタミンC", "亜鉛"])
        self.assertEqual(pipeline.supplement_primary_tags_for_payload("ビタミンC＋亜鉛", payload), ["vitamin_c", "zinc"])


class DiagnosisUnchangedTests(unittest.TestCase):

    def test_flags_only_apply_during_collection(self):
        self.assertFalse(app._PM_COLLECTION_MODE.get())
        self.assertFalse(app.pm_is_set_item("") )
        self.assertEqual(app._is_rakuten_set_item("DHC ヒアルロン酸 60日分(120粒*2コセット)"), False)  # 診断側の判定は不変


if __name__ == "__main__":
    unittest.main()
