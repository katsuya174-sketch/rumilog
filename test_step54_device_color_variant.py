"""Step54: 美容機器の同じ型式の色違いの採用(JANが色ごとに異なっていても、色違いに由来すると確認できれば採用)。
楽天タイトルは実データ(PM77 ソニック RF リフト EH-SR75)の事例を使う。

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
from test_step52_listing_selection import _item  # noqa: E402

PM77 = ("Panasonic", "ソニック RF リフト EH-SR75", "4549980687628")


def resolve_pm77(items):
    brand, name, jan = PM77
    with patch.object(app, "fetch_rakuten_candidates", return_value=[(100, i) for i in items]), \
            contextlib.redirect_stdout(io.StringIO()):
        return pipeline.resolve_item_code_for_product(brand, name, "美容機器", jan_code=jan,
                                                      identity_context={"stage1_text": "", "active_ingredients": []})


class ColorSuffixTests(unittest.TestCase):
    def test_color_suffix_pairs(self):
        self.assertTrue(pipeline._color_suffix_pair("ehsr75", "ehsr75n"))
        self.assertTrue(pipeline._color_suffix_pair("kbe2740k", "kbe2740"))
        self.assertTrue(pipeline._color_suffix_pair("kbe2740k", "kbe2740w"))   # 色記号の違い
        self.assertFalse(pipeline._color_suffix_pair("ehsr75", "ehsr85"))       # 別の型式
        self.assertFalse(pipeline._color_suffix_pair("epm18bb", "epm18bb2"))    # 末尾が数字(別モデル)
        self.assertFalse(pipeline._color_suffix_pair("epm18bb", "epm18bc"))     # 記号の一部が違う(色記号1文字ではない)

    def test_title_with_color_word_matches_by_model(self):
        # 「ゴールド調」をシリーズ語として扱わない(色記号違いの型番で照合する)。
        title = "EH-SR75-N パナソニック Panasonic 美顔器 ソニック RF リフト ゴールド調 送料無料"
        self.assertTrue(pipeline.pm_title_name_match(PM77[1], title, PM77[0], "美容機器"))
        self.assertFalse(pipeline.pm_title_name_match(PM77[1], "Panasonic 美顔器 ソニック RF リフト ゴールド EH-SR85", PM77[0], "美容機器"))


class Pm77ResolveTests(unittest.TestCase):
    def test_color_variant_listings_are_accepted(self):
        result = resolve_pm77([
            _item("all-kakudai:10064704", "EH-SR75-N パナソニック Panasonic 美顔器 ソニック RF リフト ゴールド調 送料無料()", reviews=0),
            _item("mckey:10121410", "パナソニック Panasonic ソニックRFリフト EH-SR75-N 【送料無料】 ［RF美顔器 エイジングケア］",
                  caption="カラー：ゴールド調", reviews=0),
        ])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["accepted_basis_counts"], {"color_variant": 2})
        self.assertEqual(result["item"]["itemCode"], "all-kakudai:10064704")    # レビュー同数 → 画像 → itemCode

    def test_suffix_without_color_evidence_is_not_accepted(self):
        result = resolve_pm77([_item("x:1", "Panasonic ソニックRFリフト EH-SR75-N 美顔器", reviews=9)])
        self.assertEqual(result["status"], "not_found")

    def test_other_model_and_used_are_still_excluded(self):
        result = resolve_pm77([
            _item("other:1", "Panasonic 美顔器 バイタリフト RF EH-SR85-N ゴールド調", reviews=50),
            _item("used:1", "【中古】Panasonic 美顔器 ソニック RF リフト ゴールド調 EH-SR75-N", reviews=40),
        ])
        self.assertEqual(result["status"], "not_found")


class ColorEvidenceFromSameSearchTests(unittest.TestCase):
    """色記号付きの型番が色を表すことは、同じ検索結果で同じ型番を書いた出品の色の記載でも確認できる。"""

    def test_collection_uses_other_listing_as_color_evidence(self):
        bare = _item("bare:1", "Panasonic ソニックRFリフト EH-SR75-N 美顔器", reviews=9)
        colored = _item("gold:1", "Panasonic 美顔器 ソニック RF リフト ゴールド調 EH-SR75-N", reviews=1)
        result = resolve_pm77([bare, colored])
        self.assertEqual(result["accepted_basis_counts"], {"color_variant": 2})
        self.assertEqual(result["item"]["itemCode"], "bare:1")

    def test_evidence_must_be_for_the_same_suffixed_model(self):
        bare = _item("bare:1", "Panasonic ソニックRFリフト EH-SR75-N 美顔器", reviews=9)
        other = _item("p:1", "Panasonic 美顔器 ソニック RF リフト EH-SR75-P ピンク", reviews=1)
        result = resolve_pm77([bare, other])
        self.assertEqual(result["accepted_basis_counts"], {"color_variant": 1})
        self.assertEqual(result["item"]["itemCode"], "p:1")


class ClearModelColorMappingTests(unittest.TestCase):
    """色記号付きの型番と色の対応が明確な場合に限り、色違いと認める。"""

    def check(self, title, refs=()):
        item = {"itemName": title, "itemCaption": ""}
        return app.device_model_check("ソニック RF リフト EH-SR75", item, [{"itemName": t} for t in refs])

    def test_consistent_single_color_is_clear(self):
        self.assertEqual(self.check("EH-SR75-N", ["EH-SR75-N ゴールド調", "パナソニック EH-SR75-N ゴールド"]), (True, "color_variant"))

    def test_conflicting_colors_for_same_model_are_not_clear(self):
        self.assertEqual(self.check("EH-SR75-N", ["EH-SR75-N ゴールド調", "EH-SR75-N ピンク"])[1], "color_variant_unconfirmed")

    def test_title_with_several_colors_is_not_clear(self):
        self.assertEqual(self.check("EH-SR75-N ゴールド/ピンク")[1], "color_variant_unconfirmed")
        self.assertEqual(self.check("EH-SR75-N", ["EH-SR75-N ゴールド ピンク 選べる"])[1], "color_variant_unconfirmed")

    def test_color_of_other_model_does_not_count(self):
        self.assertEqual(self.check("EH-SR75-N", ["EH-SR75-P ピンク"])[1], "color_variant_unconfirmed")

    def test_caption_color_alone_is_not_used(self):
        item = {"itemName": "EH-SR75-N", "itemCaption": "カラー：ゴールド調"}
        self.assertEqual(app.device_model_check("ソニック RF リフト EH-SR75", item)[1], "color_variant_unconfirmed")


class BrandAliasTests(unittest.TestCase):
    def test_approved_alias_is_added(self):
        forms = app.brand_match_forms("Panasonic")
        self.assertIn("パナソニック", forms)
        self.assertEqual(app.brand_match_forms("コイズミ（KOIZUMI）"), app.explicit_brand_forms("コイズミ（KOIZUMI）"))

    def test_katakana_only_title_passes_device_brand_filter(self):
        item = {"itemName": "パナソニック 美顔器 ソニック RF リフトケア ゴールド調 EH-SR75-N", "itemCode": "a:1", "itemPrice": 40000,
                "itemCaption": "", "reviewCount": 0, "reviewAverage": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNotNone(app._score_rakuten_search_item(item, "ソニック RF リフト EH-SR75", "美容機器", "Panasonic"))


class DiagnosisModelFilterTests(unittest.TestCase):
    """診断時の美容機器の候補の型番判定(収集時と同じ規則)。"""

    def filtered(self, name, titles):
        items = [(10, {"itemName": t, "itemCode": f"s:{i}", "itemCaption": ""}) for i, t in enumerate(titles)]
        with contextlib.redirect_stdout(io.StringIO()):
            return [i["itemName"] for _, i in app.filter_device_candidates_by_model(items, name)]

    def test_other_model_is_excluded_and_color_variant_kept(self):
        kept = self.filtered("バイタリフト RF EH-SR85", [
            "パナソニック バイタリフト RF EH-SR85",
            "パナソニック バイタリフト RF EH-SR85-K ブラック",
            "パナソニック バイタリフト RF EX EH-SR86",
            "パナソニック 美顔器 リフトケア",                      # 型番の記載なし(同じ型式と確認できない)
        ])
        self.assertEqual(kept, ["パナソニック バイタリフト RF EH-SR85", "パナソニック バイタリフト RF EH-SR85-K ブラック"])

    def test_color_variant_needs_evidence_in_same_search(self):
        self.assertEqual(self.filtered("バイタリフト RF EX EH-SR86", ["パナソニック EH-SR86-T"]), [])
        self.assertEqual(len(self.filtered("バイタリフト RF EX EH-SR86", ["パナソニック EH-SR86-T", "パナソニック EH-SR86-T ブラウン"])), 2)

    def test_base_model_listing_with_confirmed_color(self):
        kept = self.filtered("ピーリング＆超音波美顔器 KBE-2740/K", [
            "【メーカー公式】コイズミ 超音波美顔器 KBE-2740 ピーリング",
            "コイズミ KBE-2740/K ブラック KBE2740K",
        ])
        self.assertEqual(len(kept), 2)

    def test_name_without_model_is_not_filtered(self):
        self.assertEqual(len(self.filtered("リファカラットレイ", ["リファ ReFa CARAT RAY RF-PC2019B", "リファ カラット"])), 2)

    def test_same_model_rule_as_collection(self):
        for name in ("ソニック RF リフト EH-SR75", "ピーリング＆超音波美顔器 KBE-2740/K", "ミーゼ ディープスキンクリア（MS-43P）",
                     "メディリフト プラス EPM-18BB", "リフトケア美顔器 バイタリフト RF EX EH-SR86"):
            self.assertEqual(app._pm_full_models(name), pipeline.full_model_numbers(name), name)


if __name__ == "__main__":
    unittest.main()
