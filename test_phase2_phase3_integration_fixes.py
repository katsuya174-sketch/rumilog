"""
Phase 2+3統合検証(12パターン分析)で判明した構造的不整合の修正に対する回帰テスト。

A. 成分正規化:
   - Phase 3(ai_precollected)のproduct_masterは、Gemini抽出の生の原文成分名
     (例: "ツボクサエキス")をそのまま保存しており、既存診断ロジックが
     ingredient_focus一致判定に使う normalize_ingredient_tag() の統制タグ
     (例: "centella_extract")と文字列が一致しないため、score_product()の
     ingredient_focus_active_match等が一切発火しない構造的不整合があった。
   - 修正: app.compute_ingredient_tags()(既存のnormalize_ingredient_tag()を
     再利用、新しい正規化ロジックは作らない)で統制タグ集合を算出し、
     product_masterの新列 active_ingredient_tags に保存。読み出し時
     (_product_master_row_to_product)にactive_ingredientsへ合流させることで、
     既存のスコアリング関数側は一切変更せずに機能するようにした。
   - 原文のactive_ingredientsは変更せず保持する。正規化できない成分は
     無理にタグ化しない(タグ集合から単純に除外される)。

B. non-cosmetic誤判定:
   - is_non_cosmetic()が「カプセル」という語の単独一致で、外用化粧品
     (例: カプセル配合美容液)を誤って非化粧品と判定していた。
   - 修正: 「カプセル」については、商品名に化粧品の製品種別語(美容液/
     セラム等)が明示されている場合は除外しないようにした。経口サプリ等、
     他のキーワード(サプリ/サプリメント/錠剤等)に一致する商品は従来通り
     除外する。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


class ComputeIngredientTagsTests(unittest.TestCase):
    """app.compute_ingredient_tags()は既存のnormalize_ingredient_tag()を
    そのまま再利用して統制タグ集合を返す(別の正規化ロジックは作らない)。"""

    def test_recognizes_raw_japanese_ingredient_name(self):
        tags = app.compute_ingredient_tags(["ツボクサエキス"])
        self.assertEqual(tags, [app.normalize_ingredient_tag("ツボクサエキス")])
        self.assertIn("centella_extract", tags)

    def test_already_normalized_tag_is_idempotent(self):
        # 既存の診断時ライブ候補は既にASCIIタグを保持しているため、
        # 再計算しても同じ値になること(副作用が無いこと)を確認する。
        tags = app.compute_ingredient_tags(["niacinamide", "vitamin_c"])
        self.assertEqual(sorted(tags), ["niacinamide", "vitamin_c"])

    def test_unrecognized_ingredient_is_dropped_not_forced(self):
        # 正規化不能な成分を無理にタグ化しない(合意事項)。
        tags = app.compute_ingredient_tags(["ダマスクバラ花エキス", "ツボクサエキス"])
        self.assertIn("centella_extract", tags)
        self.assertEqual(len(tags), 1)  # ダマスクバラ花エキスはタグ化されず落ちる

    def test_empty_or_none_input_returns_empty_list(self):
        self.assertEqual(app.compute_ingredient_tags([]), [])
        self.assertEqual(app.compute_ingredient_tags(None), [])

    def test_duplicate_tags_are_deduplicated(self):
        tags = app.compute_ingredient_tags(["ツボクサエキス", "ツボクサ抽出液"])
        self.assertEqual(tags.count("centella_extract"), 1)


class AminoAcidExactMatchNormalizationTests(unittest.TestCase):
    """P2 Step9: normalize_ingredient_tag()に追加した個別アミノ酸名
    (グルタミン酸/アルギニン/ロイシン)の完全一致判定の回帰テスト。
    部分一致は禁止(既存の無関係な成分名との誤検出を防ぐため)。"""

    def test_glutamic_acid_matches_exactly(self):
        self.assertEqual(app.normalize_ingredient_tag("グルタミン酸"), "amino_acid")

    def test_arginine_matches_exactly(self):
        self.assertEqual(app.normalize_ingredient_tag("アルギニン"), "amino_acid")

    def test_leucine_matches_exactly(self):
        self.assertEqual(app.normalize_ingredient_tag("ロイシン"), "amino_acid")

    def test_generic_amino_acid_phrase_still_matches(self):
        # 既存の「アミノ酸」部分一致判定は維持されていること。
        self.assertEqual(app.normalize_ingredient_tag("アミノ酸"), "amino_acid")
        self.assertEqual(
            app.normalize_ingredient_tag("5種のアミノ酸(アラニン、アルギニン、グルタミン酸Na、セリン、プロリン)"),
            "amino_acid",
        )

    def test_polyglutamic_acid_is_not_misdetected(self):
        # 「ポリグルタミン酸」は既存の別タグ(polyglutamic_acid)のままで、
        # 誤ってamino_acidにならないこと。
        self.assertEqual(app.normalize_ingredient_tag("ポリグルタミン酸"), "polyglutamic_acid")

    def test_partial_match_false_positives_are_not_detected(self):
        # 部分一致ではないため、既存の無関係な成分名(グリシン/セリン/
        # プロリンを含む別成分)は誤ってamino_acidにならないこと。
        self.assertIsNone(app.normalize_ingredient_tag("アゼロイルジグリシンK"))
        self.assertNotEqual(
            app.normalize_ingredient_tag("ケラトMF複合成分（アミジノプロリン、コハク酸ジグリコールグアニジン、メチルセリン）"),
            "amino_acid",
        )

    def test_salt_form_variants_not_in_scope_remain_unmatched(self):
        # 今回はグルタミン酸/アルギニン/ロイシンの3成分のみが対象であり、
        # 塩形態(グルタミン酸Na等)や他の個別アミノ酸20種への拡張は
        # 今回は行っていないことを確認する(スコープの明示的な固定)。
        self.assertIsNone(app.normalize_ingredient_tag("グルタミン酸Na"))
        self.assertIsNone(app.normalize_ingredient_tag("バリン"))

    def test_compute_ingredient_tags_picks_up_newly_recognized_amino_acids(self):
        tags = app.compute_ingredient_tags(["グルタミン酸", "アルギニン", "ロイシン", "セラミド3"])
        self.assertIn("amino_acid", tags)
        self.assertIn("ceramide", tags)


class ProductMasterRowToProductTagMergeTests(unittest.TestCase):
    """_product_master_row_to_product()が、DB列のactive_ingredient_tagsを
    active_ingredients(原文は保持したまま)へ合流させることを確認する
    (実DB不要、タプル変換ロジックのみの検証)。"""

    def _build_row(self, active_ingredients, active_ingredient_tags):
        values = {
            "product_id": 1, "brand": "テストブランド", "name": "テスト商品",
            "category": "美容液", "price_ref": None,
            "active_ingredients": active_ingredients, "support_ingredients": [],
            "signature_ingredients": [], "concerns": [], "skin_types": [],
            "sensitive_ok": "unknown", "retinol_level": 0, "main_functions": [],
            "ingredient_focus": [], "ingredient_strength": {}, "formulation": [],
            "technology": [], "texture": "", "contraindications": [], "uv_level": {},
            "availability_japan": [], "last_known_image": "", "last_known_rakuten_link": "",
            "rakuten_title": "", "item_code": "", "shop_name": "",
            "data_source": "ai_precollected", "verified_at": None,
            "active_ingredient_tags": active_ingredient_tags,
        }
        return tuple(values[col] for col in app._PRODUCT_MASTER_ROW_COLUMNS)

    def test_raw_ingredient_name_is_preserved_and_tag_is_appended(self):
        row = self._build_row(["ツボクサエキス"], ["centella_extract"])
        product = app._product_master_row_to_product(row)
        self.assertIn("ツボクサエキス", product["active_ingredients"])
        self.assertIn("centella_extract", product["active_ingredients"])

    def test_no_tags_leaves_active_ingredients_unchanged(self):
        row = self._build_row(["ダマスクバラ花エキス"], [])
        product = app._product_master_row_to_product(row)
        self.assertEqual(product["active_ingredients"], ["ダマスクバラ花エキス"])

    def test_null_tags_column_does_not_error(self):
        row = self._build_row(["ドクダミエキス"], None)
        product = app._product_master_row_to_product(row)
        self.assertEqual(product["active_ingredients"], ["ドクダミエキス"])

    def test_no_duplicate_when_tag_already_equals_raw_value(self):
        row = self._build_row(["niacinamide"], ["niacinamide"])
        product = app._product_master_row_to_product(row)
        self.assertEqual(product["active_ingredients"].count("niacinamide"), 1)


class ScoreProductIngredientFocusMatchUsesTagsTests(unittest.TestCase):
    """score_product()自体は変更していないが、product_master読み出し側の
    合流処理により、生の原文成分名しか持たない候補でもingredient_focus一致
    (ingredient_focus_active_match)が発火することをエンドツーエンドで確認する。"""

    def _master_style_product(self, raw_ingredient, tag):
        return {
            "brand": "テストブランド", "name": "テスト商品美容液",
            "category": "美容液", "price_ref": 0, "price": 0,
            "active_ingredients": [raw_ingredient, tag],  # 合流後の形
            "support_ingredients": [], "signature_ingredients": [],
            "concerns": [], "skin_types": [], "sensitive_ok": "unknown",
            "retinol_level": 0, "main_functions": [], "ingredient_focus": [],
            "ingredient_strength": {}, "formulation": [], "technology": [],
            "texture": "", "contraindications": [], "uv_level": {},
            "availability_japan": [], "image": "", "rakuten_link": "",
        }

    def test_raw_ingredient_alone_does_not_match_before_merge(self):
        product = self._master_style_product("ツボクサエキス", "centella_extract")
        product["active_ingredients"] = ["ツボクサエキス"]  # 合流前(修正対象の状態)
        step = {"category": "美容液", "purpose": "鎮静ケア", "ingredient_focus": "ツボクサ"}
        reasons = []
        app.score_product(product, step, {"oil": "normal", "sens": "low"}, 3000, reasons=reasons)
        rule_names = {r.get("rule") for r in reasons}
        self.assertNotIn("ingredient_focus_active_match", rule_names)

    def test_merged_tag_enables_ingredient_focus_active_match(self):
        product = self._master_style_product("ツボクサエキス", "centella_extract")
        step = {"category": "美容液", "purpose": "鎮静ケア", "ingredient_focus": "ツボクサ"}
        reasons = []
        app.score_product(product, step, {"oil": "normal", "sens": "low"}, 3000, reasons=reasons)
        rule_names = {r.get("rule") for r in reasons}
        self.assertIn("ingredient_focus_active_match", rule_names)


class IsNonCosmeticCapsuleOverrideTests(unittest.TestCase):
    """is_non_cosmetic()の「カプセル」単独一致による外用化粧品の誤除外を修正。"""

    def test_topical_capsule_serum_is_not_excluded(self):
        self.assertFalse(app.is_non_cosmetic({
            "name": "Anua PDRNヒアルロン酸カプセル100セラム", "category": "美容液",
        }))

    def test_bare_capsule_without_cosmetic_type_word_still_excluded(self):
        # 化粧品の製品種別語が無い場合は、従来通り「カプセル」一致で除外する
        # (経口サプリの安全側フォールバックは維持する)。
        self.assertTrue(app.is_non_cosmetic({
            "name": "マルチビタミンカプセル", "category": "",
        }))

    def test_real_supplement_name_with_capsule_is_still_excluded(self):
        self.assertTrue(app.is_non_cosmetic({
            "name": "亜鉛マルチビタミンサプリメント カプセルタイプ 30日分", "category": "",
        }))

    def test_other_oral_keywords_unaffected_by_capsule_change(self):
        self.assertTrue(app.is_non_cosmetic({"name": "美容ドリンク コラーゲン配合", "category": ""}))
        self.assertTrue(app.is_non_cosmetic({"name": "健康食品 マルチタブレット", "category": ""}))

    def test_score_product_no_longer_hard_excludes_anua_pdrn_capsule_serum(self):
        product = {
            "brand": "Anua", "name": "Anua PDRNヒアルロン酸カプセル100セラム",
            "category": "美容液", "price_ref": 0, "price": 0,
            "active_ingredients": [], "support_ingredients": [], "signature_ingredients": [],
            "concerns": [], "skin_types": [], "sensitive_ok": "unknown", "retinol_level": 0,
            "main_functions": [], "ingredient_focus": [], "ingredient_strength": {},
            "formulation": [], "technology": [], "texture": "", "contraindications": [],
            "uv_level": {}, "availability_japan": [], "image": "", "rakuten_link": "",
        }
        step = {"category": "美容液", "purpose": "保湿", "ingredient_focus": ""}
        score = app.score_product(product, step, {"oil": "normal", "sens": "low"}, 3000)
        self.assertGreater(score, -9000)


if __name__ == "__main__":
    unittest.main()
